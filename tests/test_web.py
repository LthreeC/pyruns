import json
import ast
import http.cookiejar
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import psutil
from fastapi.testclient import TestClient
from starlette.websockets import WebSocket, WebSocketDisconnect

from pyruns import __version__
from pyruns._config import (
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_TASK_SUMMARY_SEARCH_TEXT_CHARS,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
    ENV_KEY_ROOT,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    TASK_INFO_FILENAME,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KIND_SHELL,
)
from pyruns.core.executor import _build_command, _resolve_python_runtime
from pyruns.core.task_manager import TaskManager, active_task_run_index
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import load_task_info, save_task_info, update_task_info
from pyruns.web.app import create_app as _create_app
from pyruns.web.runtime import (
    PyrunsRuntime,
    TaskEnvConflictError,
    TaskNotesConflictError,
    parse_global_env_text,
)

WEB_APP = Path(__file__).resolve().parents[1] / "pyruns" / "web" / "app.py"
WEB_RUNTIME = Path(__file__).resolve().parents[1] / "pyruns" / "web" / "runtime.py"


class _TestClientScope:
    """Normalize the client address omitted by older Starlette TestClients."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] in {"http", "websocket"} and scope.get("client") is None:
            scope = {**scope, "client": ("testclient", 50000)}
        await self.app(scope, receive, send)


def create_app(runtime=None, **kwargs):
    """Create an app with the ASGI test-client bypass enabled explicitly."""

    kwargs.setdefault("allow_test_client_bypass", True)
    app = _create_app(runtime, **kwargs)
    app.add_middleware(_TestClientScope)
    return app


def test_pyruns_runtime_declares_single_constructor():
    module = ast.parse(WEB_RUNTIME.read_text(encoding="utf-8"))
    runtime_classes = [
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "PyrunsRuntime"
    ]

    assert len(runtime_classes) == 1
    constructors = [
        node
        for node in runtime_classes[0].body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    ]
    assert len(constructors) == 1


def test_web_package_lazy_exports_public_api():
    import pyruns.web as web
    from pyruns.web.app import create_app, main
    from pyruns.web.runtime import PyrunsRuntime

    assert web.PyrunsRuntime is PyrunsRuntime
    assert web.create_app is create_app
    assert web.main is main
    with pytest.raises(AttributeError):
        web.not_a_public_export


def test_web_app_version_matches_package_version():
    app = create_app(_RouteRuntime())

    assert app.version == __version__


def test_system_info_exposes_instance_and_disables_updates_without_coordinator(monkeypatch):
    from pyruns.web import self_update

    monkeypatch.delenv(self_update.UI_UPDATE_RESULT_ENV, raising=False)
    client = TestClient(create_app(_RouteRuntime()))

    info = client.get("/api/system/info")
    check = client.get("/api/system/update/check")
    update = client.post("/api/system/update")
    restart = client.post("/api/system/restart")

    assert info.status_code == 200
    assert info.json()["version"] == __version__
    assert info.json()["instance_id"]
    assert info.json()["update_supported"] is False
    assert info.json()["update_state"] == "unavailable"
    assert info.json()["installed_version"] == __version__
    assert info.json()["restart_required"] is False
    assert info.json()["last_update"] is None
    assert check.status_code == 503
    assert update.status_code == 503
    assert restart.status_code == 503


def test_system_update_check_reports_latest_pypi_version(monkeypatch):
    from pyruns.web.self_update import UiUpdateCoordinator

    monkeypatch.setattr(
        "pyruns.web.app.check_latest_version",
        lambda current: {
            "current_version": current,
            "latest_version": "0.4.0",
            "update_available": True,
        },
    )
    coordinator = UiUpdateCoordinator(lambda: None)
    client = TestClient(create_app(_RouteRuntime(), update_coordinator=coordinator))

    response = client.get("/api/system/update/check")

    assert response.status_code == 200
    assert response.json() == {
        "current_version": __version__,
        "latest_version": "0.4.0",
        "update_available": True,
    }

    def fail_check(_current):
        from pyruns.web.self_update import LatestVersionCheckError

        raise LatestVersionCheckError("PyPI unavailable")

    monkeypatch.setattr("pyruns.web.app.check_latest_version", fail_check)
    failed = client.get("/api/system/update/check")
    assert failed.status_code == 503
    assert failed.json()["detail"] == "PyPI unavailable"


def test_system_update_requires_idle_runtime_then_gates_task_starts():
    from pyruns.web.self_update import UiUpdateCoordinator

    shutdowns = []
    runtime = _RouteRuntime(
        {
            "active_task_count": 0,
            "start_task": {"name": "alpha", "status": "running"},
        }
    )
    coordinator = UiUpdateCoordinator(lambda: shutdowns.append("shutdown"))
    client = TestClient(create_app(runtime, update_coordinator=coordinator))

    response = client.post(
        "/api/system/update",
        json={"target_version": "0.4.0"},
    )
    start = client.post("/api/tasks/alpha/run")
    info = client.get("/api/system/info")

    assert response.status_code == 202
    assert response.json()["state"] == "restarting"
    assert shutdowns == ["shutdown"]
    assert start.status_code == 503
    assert "new tasks are disabled" in start.json()["detail"]
    assert info.json()["update_supported"] is True
    assert info.json()["update_state"] == "restarting"
    assert coordinator.handoff()["target_version"] == "0.4.0"


def test_system_restart_reports_external_version_and_requires_manual_request(monkeypatch):
    from pyruns.web.self_update import UiUpdateCoordinator

    shutdowns = []
    runtime = _RouteRuntime({"active_task_count": 0})
    coordinator = UiUpdateCoordinator(
        lambda: shutdowns.append("shutdown"),
        current_version="0.3.0",
    )
    monkeypatch.setattr(coordinator, "_installed_version", lambda _fallback: "0.4.0")
    client = TestClient(create_app(runtime, update_coordinator=coordinator))

    info = client.get("/api/system/info")

    assert info.status_code == 200
    assert info.json()["version"] == __version__
    assert info.json()["installed_version"] == "0.4.0"
    assert info.json()["restart_required"] is True
    assert info.json()["update_state"] == "restart_required"
    assert coordinator.requested is False
    assert shutdowns == []

    response = client.post("/api/system/restart")

    assert response.status_code == 202
    assert response.json()["state"] == "restarting"
    assert coordinator.requested is True
    assert shutdowns == ["shutdown"]


def test_system_update_gates_generator_task_creation():
    from pyruns.web.self_update import UiUpdateCoordinator

    runtime = _RouteRuntime({"active_task_count": 0})
    coordinator = UiUpdateCoordinator(lambda: None)
    client = TestClient(create_app(runtime, update_coordinator=coordinator))

    assert client.post("/api/system/update").status_code == 202
    response = client.post(
        "/api/generator/create",
        json={"name_prefix": "blocked", "mode": "yaml", "yaml_text": "x: 1"},
    )

    assert response.status_code == 503
    assert "new tasks are disabled" in response.json()["detail"]


def test_system_update_refuses_active_tasks_without_stopping_server():
    from pyruns.web.self_update import UiUpdateCoordinator

    shutdowns = []
    runtime = _RouteRuntime({"active_task_count": 2})
    coordinator = UiUpdateCoordinator(lambda: shutdowns.append("shutdown"))
    client = TestClient(create_app(runtime, update_coordinator=coordinator))

    response = client.post("/api/system/update")

    assert response.status_code == 409
    assert "2 queued or running tasks" in response.json()["detail"]
    assert coordinator.requested is False
    assert shutdowns == []


def test_runtime_active_task_count_refreshes_all_owned_managers(tmp_path):
    refreshes = []

    class CountingManager:
        def __init__(self):
            self.tasks = [
                {"name": "running", "status": "running"},
                {"name": "pending", "status": "pending"},
            ]
            self.is_processing = False
            self.callback = None

        def refresh_from_disk(self, **kwargs):
            refreshes.append(kwargs)

        def list_tasks(self, *, summary=False):
            return [dict(task) for task in self.tasks]

        def on_change(self, callback):
            self.callback = callback

        def off_change(self, callback):
            assert callback is self.callback
            self.callback = None

        def shutdown(self):
            pass

    manager = CountingManager()
    runtime = PyrunsRuntime(
        root_dir=str(tmp_path),
        task_manager_factory=lambda _tasks_dir: manager,
    )
    try:
        assert runtime.active_task_count() == 1
        assert any(
            call
            == {
                "force_all": False,
                "check_all": True,
                "discover": True,
                "raise_on_error": False,
            }
            for call in refreshes
        )
        assert runtime.strict_active_task_count() == 1
        assert any(
            call
            == {
                "force_all": True,
                "check_all": False,
                "discover": True,
                "raise_on_error": True,
            }
            for call in refreshes
        )

        manager.tasks[0]["status"] = "completed"
        assert runtime.active_task_count() == 0

        manager.is_processing = True
        assert runtime.active_task_count() == 1
    finally:
        runtime.shutdown()


def test_web_app_does_not_launch_server_when_imported_as_multiprocessing_main():
    """Windows process-spawn imports use __mp_main__ and must not start uvicorn."""

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import runpy; runpy.run_module('pyruns.web.app', run_name='__mp_main__')",
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert "RuntimeWarning" not in output


def _make_workspace(root: Path, name: str) -> Path:
    workspace = root / "_pyruns_" / name
    workspace.mkdir(parents=True, exist_ok=True)
    script_path = root / f"{name}.py"
    script_path.write_text("print('hello')\n", encoding="utf-8")
    (workspace / "script_info.json").write_text(
        json.dumps(
            {
                "script_name": name,
                "script_path": str(script_path),
                "workspace_kind": WORKSPACE_KIND_SCRIPT,
            }
        ),
        encoding="utf-8",
    )
    (workspace / "config_default.yaml").write_text("lr: 0.01\n", encoding="utf-8")
    (workspace / TASKS_DIR).mkdir(exist_ok=True)
    return workspace


def _add_task(workspace: Path, name: str, status: str = "pending", log_text: str = "") -> None:
    task_dir = workspace / TASKS_DIR / name
    task_dir.mkdir(parents=True, exist_ok=True)
    start_times = ["2026-03-17_12-00-00"] if status == "running" else []
    pids = [__import__("os").getpid()] if status == "running" else []
    save_task_info(
        str(task_dir),
        {
            "name": name,
            "status": status,
            "progress": 1.0 if status == "completed" else 0.0,
            "created_at": "2026-03-17_12-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "start_times": start_times,
            "finish_times": [],
            "pids": pids,
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01, "model": "tiny"})
    log_dir = task_dir / "run_logs"
    log_dir.mkdir(exist_ok=True)
    if log_text:
        (log_dir / "run1.log").write_text(log_text, encoding="utf-8")


def _build_runtime(
    workspace: Path,
    *,
    owns_task_lifecycle: bool = True,
) -> PyrunsRuntime:
    def mark_existing_running_tasks_owned(tasks_dir: str, manager: TaskManager) -> None:
        for task_dir in Path(tasks_dir).iterdir():
            if not task_dir.is_dir():
                continue
            info_path = task_dir / TASK_INFO_FILENAME
            if not info_path.exists():
                continue
            info = json.loads(info_path.read_text(encoding="utf-8"))
            if info.get("status") != "running":
                continue

            def _apply(task_info, manager=manager):
                task_info["runner_id"] = manager.runner_id
                task_info["runner_host"] = manager.runner_host
                task_info["lease_heartbeat"] = time.time()
                task_info["lease_until"] = time.time() + 60

            update_task_info(str(task_dir), _apply)

    def make_task_manager(tasks_dir: str) -> TaskManager:
        with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
            manager = TaskManager(
                tasks_dir=tasks_dir,
                lazy_scan=None,
                owns_task_lifecycle=owns_task_lifecycle,
            )
            if owns_task_lifecycle:
                mark_existing_running_tasks_owned(tasks_dir, manager)
            manager.scan_disk()
            return manager

    return PyrunsRuntime(root_dir=str(workspace), task_manager_factory=make_task_manager)


class _RouteRuntime:
    def __init__(self, results=None):
        self.results = results or {}
        self.settings = {"ui_port": 8099}

    def __getattr__(self, name):
        def call(*args, **kwargs):
            result = self.results.get(name, {"ok": True})
            if isinstance(result, BaseException):
                raise result
            if callable(result):
                return result(*args, **kwargs)
            return result

        return call


def test_local_server_rejects_cross_origin_and_dns_rebinding_requests():
    client = TestClient(create_app(_RouteRuntime()))

    allowed = client.get(
        "/api/workspace",
        headers={"Origin": "http://testserver", "Host": "testserver"},
    )
    cross_origin = client.post(
        "/api/workspace/shell",
        headers={"Origin": "https://attacker.example", "Host": "testserver"},
    )
    rebound = client.get(
        "/api/workspace",
        headers={"Origin": "http://attacker.example", "Host": "attacker.example"},
    )
    malformed_host = client.get(
        "/api/workspace",
        headers={"Host": "127.0.0.1/attacker"},
    )
    preflight = client.options(
        "/api/workspace/shell",
        headers={
            "Origin": "https://attacker.example",
            "Host": "testserver",
            "Access-Control-Request-Method": "POST",
        },
    )

    assert allowed.status_code == 200
    assert cross_origin.status_code == 403
    assert rebound.status_code == 403
    assert malformed_host.status_code == 403
    assert preflight.status_code == 403
    assert "access-control-allow-origin" not in cross_origin.headers
    for response in (allowed, cross_origin, rebound, malformed_host, preflight):
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"


def test_local_api_requires_random_session_token_outside_test_bypass():
    app = create_app(
        _RouteRuntime(),
        access_token="correct-horse-battery-staple",
        allow_test_client_bypass=False,
    )
    client = TestClient(app)

    unauthenticated = client.get("/api/workspace")
    wrong_token = client.get("/launcher?token=wrong", follow_redirects=False)
    bootstrap = client.get(
        "/launcher?token=correct-horse-battery-staple",
        follow_redirects=False,
    )
    authenticated = client.get("/api/workspace")

    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["www-authenticate"] == "PyrunsToken"
    assert wrong_token.status_code == 401
    assert bootstrap.status_code == 303
    assert bootstrap.headers["location"] == "/launcher"
    assert "HttpOnly" in bootstrap.headers["set-cookie"]
    assert "SameSite=strict" in bootstrap.headers["set-cookie"]
    assert authenticated.status_code == 200
    assert "token=" not in str(authenticated.url)


def test_session_recovery_keeps_one_browser_session_across_ui_restarts(tmp_path):
    state_path = tmp_path / "session.json"
    nonce = "1" * 32
    original_scope = "host\nport\noriginal-workspace"
    first = _create_app(
        _RouteRuntime({"get_workspace_info": {"instance": "first"}}),
        access_token="first-token",
        session_cookie_nonce=nonce,
        session_state_path=str(state_path),
        session_scope_value=original_scope,
        allow_test_client_bypass=False,
    )
    first_client = TestClient(first, base_url="http://127.0.0.1")
    bootstrap = first_client.get(
        "/?token=first-token",
        follow_redirects=False,
    )
    assert bootstrap.status_code == 303
    persistent_token = first.state.session_recovery.cookie_token
    assert persistent_token
    assert persistent_token != "first-token"
    assert first_client.cookies.get(first.state.session_cookie_name) == persistent_token
    assert "Max-Age=34560000" in bootstrap.headers["set-cookie"]

    second = _create_app(
        _RouteRuntime({"get_workspace_info": {"instance": "second"}}),
        access_token="second-token",
        session_cookie_nonce=nonce,
        session_state_path=str(state_path),
        session_scope_value=original_scope,
        allow_test_client_bypass=False,
    )
    second_client = TestClient(second, base_url="http://127.0.0.1")
    second_client.cookies.update(first_client.cookies)

    recovered = second_client.get("/api/workspace")
    assert recovered.status_code == 200
    assert recovered.json() == {"instance": "second"}
    assert second_client.cookies.get(second.state.session_cookie_name) == persistent_token
    assert "Max-Age=34560000" in recovered.headers["set-cookie"]

    third = _create_app(
        _RouteRuntime({"get_workspace_info": {"instance": "third"}}),
        access_token="third-token",
        session_cookie_nonce=nonce,
        session_state_path=str(state_path),
        session_scope_value=original_scope,
        allow_test_client_bypass=False,
    )
    third_client = TestClient(third, base_url="http://127.0.0.1")
    third_client.cookies.update(first_client.cookies)
    assert third_client.get("/api/workspace").json() == {"instance": "third"}
    assert third_client.cookies.get(third.state.session_cookie_name) == persistent_token
    assert third_client.get(
        "/?token=first-token",
        follow_redirects=False,
    ).status_code == 401

    explicit_recovery = TestClient(second, base_url="http://127.0.0.1")
    explicit_recovery.cookies.update(first_client.cookies)
    recovered = explicit_recovery.post(
        "/session/recover",
        headers={"Origin": "http://127.0.0.1"},
    )
    assert recovered.status_code == 200
    assert recovered.json() == {"ok": True}
    assert explicit_recovery.cookies.get(second.state.session_cookie_name) == persistent_token

    state_path.unlink()
    still_authenticated = second_client.get("/api/workspace")
    assert still_authenticated.status_code == 200
    assert still_authenticated.json() == {"instance": "second"}

    forged = TestClient(second, base_url="http://127.0.0.1")
    forged.cookies.set(second.state.session_cookie_name, "forged-token")
    assert forged.post(
        "/session/recover",
        headers={"Origin": "http://127.0.0.1"},
    ).status_code == 401
    assert forged.post(
        "/session/recover",
        headers={
            "Origin": "https://attacker.example",
            "Sec-Fetch-Site": "cross-site",
        },
    ).status_code == 403


def test_parallel_ui_instances_keep_independent_http_sessions():
    first_token = "first-instance-bootstrap-secret"
    second_token = "second-instance-bootstrap-secret"
    first_app = create_app(
        _RouteRuntime({"get_workspace_info": {"instance": "first"}}),
        access_token=first_token,
        allow_test_client_bypass=False,
    )
    second_app = create_app(
        _RouteRuntime({"get_workspace_info": {"instance": "second"}}),
        access_token=second_token,
        allow_test_client_bypass=False,
    )
    first_name = first_app.state.session_cookie_name
    second_name = second_app.state.session_cookie_name

    assert first_name != second_name
    assert re.fullmatch(r"pyruns_session_[0-9a-f]{32}", first_name)
    assert re.fullmatch(r"pyruns_session_[0-9a-f]{32}", second_name)
    assert first_token not in first_name
    assert second_token not in second_name

    first_client = TestClient(first_app)
    second_client = TestClient(second_app)
    assert first_client.get(f"/?token={first_token}", follow_redirects=False).status_code == 303
    assert second_client.get(f"/?token={second_token}", follow_redirects=False).status_code == 303
    first_cookie = first_client.cookies.get(first_name)
    second_cookie = second_client.cookies.get(second_name)
    shared_cookie = f"{first_name}={first_cookie}; {second_name}={second_cookie}"

    assert first_cookie == first_token
    assert second_cookie == second_token
    assert first_client.get(
        "/api/workspace",
        headers={"Cookie": f"{second_name}={second_cookie}"},
    ).status_code == 401
    assert second_client.get(
        "/api/workspace",
        headers={"Cookie": f"{first_name}={first_cookie}"},
    ).status_code == 401
    assert first_client.get(
        "/api/workspace",
        headers={"Cookie": shared_cookie},
    ).json() == {"instance": "first"}
    assert second_client.get(
        "/api/workspace",
        headers={"Cookie": shared_cookie},
    ).json() == {"instance": "second"}


def test_explicit_ui_instance_ignores_inherited_reload_cookie_nonce(monkeypatch):
    inherited_nonce = "a" * 32
    monkeypatch.setenv("PYRUNS_UI_COOKIE_NONCE", inherited_nonce)

    app = create_app(
        _RouteRuntime(),
        access_token="explicit-token",
        allow_test_client_bypass=False,
    )

    assert app.state.session_cookie_name != f"pyruns_session_{inherited_nonce}"


def test_parallel_ui_instances_keep_independent_websocket_sessions():
    class EventManager:
        def on_change(self, _callback):
            return None

        def off_change(self, _callback):
            return None

    class EventRuntime(_RouteRuntime):
        def __init__(self):
            super().__init__()
            self.event_manager = EventManager()

        def get_task_event_stream_context(self):
            return "workspace", self.event_manager

        def workspace_stream_is_current(self, root, manager):
            return root == "workspace" and manager is self.event_manager

        def release_task_event_stream_context(self, _root, _manager):
            return None

    first_token = "first-websocket-secret"
    second_token = "second-websocket-secret"
    first_app = create_app(
        EventRuntime(),
        access_token=first_token,
        allow_test_client_bypass=False,
    )
    second_app = create_app(
        EventRuntime(),
        access_token=second_token,
        allow_test_client_bypass=False,
    )
    first_name = first_app.state.session_cookie_name
    second_name = second_app.state.session_cookie_name
    first_client = TestClient(first_app)
    second_client = TestClient(second_app)
    assert first_client.get(f"/?token={first_token}", follow_redirects=False).status_code == 303
    assert second_client.get(f"/?token={second_token}", follow_redirects=False).status_code == 303
    first_cookie = first_client.cookies.get(first_name)
    second_cookie = second_client.cookies.get(second_name)
    shared_cookie = f"{first_name}={first_cookie}; {second_name}={second_cookie}"

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with first_client.websocket_connect(
            "/api/tasks/events",
            headers={"Cookie": f"{second_name}={second_cookie}"},
        ):
            pass
    assert exc_info.value.code == 4401

    with first_client.websocket_connect(
        "/api/tasks/events",
        headers={"Cookie": shared_cookie},
    ) as websocket:
        assert websocket.receive_json() == {"type": "ready", "revision": 0}
    with second_client.websocket_connect(
        "/api/tasks/events",
        headers={"Cookie": shared_cookie},
    ) as websocket:
        assert websocket.receive_json() == {"type": "ready", "revision": 0}


def test_unicode_ui_token_is_rejected_without_server_error():
    app = create_app(
        _RouteRuntime(),
        access_token="ascii-token",
        allow_test_client_bypass=False,
    )
    client = TestClient(app)

    rejected = client.get(
        "/launcher",
        params={"token": chr(233)},
        follow_redirects=False,
    )
    accepted = client.get(
        "/launcher?token=ascii-token",
        follow_redirects=False,
    )

    assert rejected.status_code == 401
    assert rejected.json() == {"detail": "Invalid UI access token"}
    assert accepted.status_code == 303


def test_local_websocket_requires_session_token_outside_test_bypass():
    app = create_app(
        _RouteRuntime(),
        access_token="websocket-secret",
        allow_test_client_bypass=False,
    )
    client = TestClient(app)

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect("/api/tasks/events"):
            pass

    assert exc_info.value.code == 4401


def test_forwarded_testclient_headers_cannot_bypass_http_authentication():
    client = TestClient(
        _create_app(_RouteRuntime(), access_token="http-secret")
    )

    response = client.get(
        "/api/workspace",
        headers={
            "Host": "127.0.0.1:8099",
            "X-Forwarded-For": "testclient",
            "X-Forwarded-Host": "testserver",
        },
    )

    assert response.status_code == 401


def test_forwarded_testclient_headers_cannot_bypass_websocket_authentication():
    client = TestClient(
        _create_app(_RouteRuntime(), access_token="websocket-secret")
    )

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/tasks/events",
            headers={
                "Host": "127.0.0.1:8099",
                "X-Forwarded-For": "testclient",
                "X-Forwarded-Host": "testserver",
            },
        ):
            pass

    assert exc_info.value.code == 4401


def test_api_rejects_oversized_and_unbounded_requests(monkeypatch):
    from pyruns.web import app as web_app

    monkeypatch.setattr(web_app, "MAX_API_REQUEST_BYTES", 64)
    client = TestClient(create_app(_RouteRuntime()))

    oversized = client.post(
        "/api/workspace/run-root",
        content=json.dumps({"path": "x" * 100}),
        headers={"Content-Type": "application/json"},
    )

    assert oversized.status_code == 413
    assert "64 bytes" in oversized.json()["detail"]


def test_api_rejects_unknown_fields_and_resource_limit_overrides(monkeypatch):
    from pyruns.web import app as web_app

    monkeypatch.setattr(web_app, "MAX_TASK_BATCH_ITEMS", 1)
    monkeypatch.setattr(web_app, "MAX_ENVIRONMENT_ITEMS", 1)
    client = TestClient(create_app(_RouteRuntime()))

    unknown = client.post("/api/workspace/run-root", json={"path": ".", "typo": True})
    too_many_workers = client.post(
        "/api/tasks/batch/run",
        json={"task_names": ["alpha"], "max_workers": 33},
    )
    unbounded_page = client.get("/api/tasks", params={"limit": 0})
    oversized_batch = client.post(
        "/api/tasks/batch/delete",
        json={"task_names": ["alpha", "beta"]},
    )
    oversized_env = client.patch(
        "/api/tasks/alpha/env",
        json={"env": {"A": "1", "B": "2"}, "expected_env": {}},
    )
    oversized_expected_env = client.patch(
        "/api/tasks/alpha/env",
        json={"env": {}, "expected_env": {"A": "1", "B": "2"}},
    )

    assert unknown.status_code == 422
    assert too_many_workers.status_code == 422
    assert unbounded_page.status_code == 422
    assert oversized_batch.status_code == 400
    assert oversized_env.status_code == 400
    assert oversized_expected_env.status_code == 400


@pytest.mark.parametrize(
    "endpoint",
    ["/api/tasks/alpha/logs/stream", "/api/tasks/events"],
    ids=["task-log", "task-events"],
)
def test_websocket_rejects_cross_origin_browser_clients(endpoint):
    client = TestClient(create_app(_RouteRuntime()))

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            endpoint,
            headers={"Origin": "https://attacker.example", "Host": "testserver"},
        ):
            pass

    assert exc_info.value.code == 4403


def test_task_event_websocket_pushes_invalidations_and_releases_watch(tmp_path):
    workspace = _make_workspace(tmp_path, "events")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    try:
        with client.websocket_connect("/api/tasks/events") as websocket:
            ready = websocket.receive_json()
            assert ready == {"type": "ready", "revision": 0}
            assert runtime.task_manager.has_reactive_watchers() is True

            runtime.task_manager.trigger_update()
            changed = websocket.receive_json()
            assert changed == {"type": "changed", "revision": 1}

        assert runtime.task_manager.has_reactive_watchers() is False
    finally:
        runtime.shutdown()


def test_task_event_websocket_ignores_close_after_client_disconnect(tmp_path):
    workspace = _make_workspace(tmp_path, "events-disconnect")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    try:
        with patch.object(WebSocket, "close", side_effect=WebSocketDisconnect(1006)):
            with client.websocket_connect("/api/tasks/events") as websocket:
                assert websocket.receive_json()["type"] == "ready"
        assert runtime.task_manager.has_reactive_watchers() is False
    finally:
        runtime.shutdown()


def test_task_event_websocket_closes_when_workspace_changes(tmp_path):
    workspace_a = _make_workspace(tmp_path, "event-a")
    workspace_b = _make_workspace(tmp_path, "event-b")
    _add_task(workspace_a, "alpha")
    runtime = _build_runtime(workspace_a)
    client = TestClient(create_app(runtime))

    try:
        with client.websocket_connect("/api/tasks/events") as websocket:
            assert websocket.receive_json()["type"] == "ready"
            old_manager = runtime.task_manager
            runtime.change_run_root(str(workspace_b))
            old_manager.trigger_update()

            with pytest.raises(WebSocketDisconnect) as exc_info:
                websocket.receive_json()
            assert exc_info.value.code == 4409
        assert old_manager.has_reactive_watchers() is False
        assert old_manager not in runtime._task_managers.values()
    finally:
        runtime.shutdown()


def test_reactive_task_watch_discovers_external_task_changes(tmp_path):
    workspace = _make_workspace(tmp_path, "external-events")
    manager = TaskManager(tasks_dir=str(workspace / TASKS_DIR), lazy_scan=False)
    changed = threading.Event()
    manager.on_change(changed.set)
    manager.acquire_reactive_watch()

    try:
        _add_task(workspace, "created-elsewhere")

        assert changed.wait(3)
        assert manager.get_task("created-elsewhere") is not None
    finally:
        manager.release_reactive_watch()
        manager.off_change(changed.set)
        manager.shutdown()


def test_root_uses_fallback_html_when_static_bundle_is_missing(tmp_path, monkeypatch):
    from pyruns.web import app as web_app

    monkeypatch.setattr(web_app, "_frontend_candidates", lambda: [tmp_path / "missing"])
    client = TestClient(web_app.create_app(_RouteRuntime()), base_url="http://127.0.0.1")

    response = client.get("/")

    assert response.status_code == 200
    assert "Pyruns API server is running" in response.text
    assert "pyruns/web/static" in response.text
    assert "frontend/dist" not in response.text


def test_schedule_browser_open_ignores_browser_errors(monkeypatch):
    from pyruns.web import app as web_app

    class ImmediateThread:
        def __init__(self, target, daemon):
            self.target = target
            self.daemon = daemon

        def start(self):
            self.target()

    monkeypatch.setattr(web_app.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(web_app.time, "sleep", lambda delay: None)
    monkeypatch.setattr(web_app.webbrowser, "open", lambda url: (_ for _ in ()).throw(RuntimeError("browser failed")))

    web_app._schedule_browser_open("http://127.0.0.1:8099", delay_seconds=0)


def test_browser_environment_detection_honors_overrides_and_headless_linux(monkeypatch):
    from pyruns.web import app as web_app

    monkeypatch.setenv("PYRUNS_NO_BROWSER", "1")
    assert web_app._can_open_browser_from_environment() is False

    monkeypatch.delenv("PYRUNS_NO_BROWSER", raising=False)
    monkeypatch.setenv("PYRUNS_OPEN_BROWSER", "yes")
    assert web_app._can_open_browser_from_environment() is True

    monkeypatch.setenv("PYRUNS_OPEN_BROWSER", "off")
    assert web_app._can_open_browser_from_environment() is False

    monkeypatch.delenv("PYRUNS_OPEN_BROWSER", raising=False)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(web_app.sys, "platform", "linux")
    assert web_app._can_open_browser_from_environment() is False


def test_find_available_port_handles_invalid_or_exhausted_ranges():
    from pyruns.web import app as web_app

    class BusySocket:
        calls = []

        def setsockopt(self, *_args):
            pass

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def bind(self, address):
            self.calls.append(address)
            raise OSError("busy")

    with patch.object(web_app.socket, "socket", lambda *args, **kwargs: BusySocket()):
        with pytest.raises(RuntimeError):
            web_app.find_available_port("bad", host="127.0.0.1", max_attempts=0)
        assert BusySocket.calls == [("127.0.0.1", web_app.DEFAULT_UI_PORT)]

        BusySocket.calls.clear()
        with pytest.raises(RuntimeError):
            web_app.find_available_port(70000, host="127.0.0.1", max_attempts=0)
        assert BusySocket.calls == [("127.0.0.1", web_app.DEFAULT_UI_PORT)]


@pytest.mark.parametrize(
    ("os_name", "expected_options"),
    [
        ("posix", [(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)]),
        ("nt", []),
    ],
)
def test_find_available_port_matches_platform_listener_reuse(
    monkeypatch,
    os_name,
    expected_options,
):
    from pyruns.web import app as web_app

    class ProbeSocket:
        options = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def setsockopt(self, *option):
            self.options.append(option)

        def bind(self, _address):
            pass

    monkeypatch.setattr(web_app.os, "name", os_name)
    monkeypatch.setattr(web_app.socket, "socket", lambda *_args, **_kwargs: ProbeSocket())

    assert web_app.find_available_port(8123, max_attempts=0) == 8123
    assert ProbeSocket.options == expected_options


def test_parse_main_options_handles_browser_flags_and_invalid_ports(capsys):
    from pyruns.web import app as web_app

    app_web_options = web_app._parse_main_options(["--port", "8123", "--no-browser"])
    assert app_web_options == (8123, False)
    assert web_app._parse_main_options(["--port=8124", "--browser"]) == (8124, True)

    with pytest.raises(SystemExit) as missing_port:
        web_app._parse_main_options(["--port"])
    assert missing_port.value.code == 2
    assert "expected one argument" in capsys.readouterr().err

    with pytest.raises(SystemExit) as invalid_port:
        web_app._parse_main_options(["--port", "not-a-port"])
    assert invalid_port.value.code == 2
    assert "invalid port" in capsys.readouterr().err

    with pytest.raises(SystemExit) as out_of_range:
        web_app._parse_main_options(["--port", "70000"])
    assert out_of_range.value.code == 2
    assert "port must be between" in capsys.readouterr().err

    for removed_or_unknown in ("--open-browser", "--broser"):
        with pytest.raises(SystemExit) as unknown:
            web_app._parse_main_options([removed_or_unknown])
        assert unknown.value.code == 2
        assert "unrecognized arguments" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("method", "path", "json_body", "params", "runtime_results", "expected_status", "detail_part"),
    [
        ("post", "/api/workspace/run-root", {"path": "missing"}, None, {"change_run_root": ValueError("bad root")}, 400, "bad root"),
        ("post", "/api/workspace/shell", None, None, {"open_shell_workspace": ValueError("shell not ready")}, 400, "shell not ready"),
        ("patch", "/api/runtime", {"python_executable": "bad"}, None, {"update_runtime_settings": ValueError("bad python")}, 400, "bad python"),
        ("get", "/api/templates/content", None, {"value": "missing.yaml"}, {"get_template_content": FileNotFoundError("missing template")}, 404, "missing template"),
        ("post", "/api/generator/create", {"name_prefix": "x", "mode": "yaml", "yaml_text": ":"}, None, {"create_tasks_from_template": ValueError("bad yaml")}, 400, "bad yaml"),
        ("post", "/api/generator/preview", {"mode": "yaml", "yaml_text": ":"}, None, {"preview_tasks_from_template": ValueError("bad preview")}, 400, "bad preview"),
        ("post", "/api/generator/pick-shell-file", None, None, {"pick_generator_shell_file": FileNotFoundError("picker unavailable")}, 400, "picker unavailable"),
        ("get", "/api/launcher/configs", None, {"script": "missing.py"}, {"get_launcher_config_info": FileNotFoundError("script missing")}, 400, "script missing"),
        ("get", "/api/launcher/workspaces", None, {"script": "missing.py"}, {"list_launcher_workspaces": FileNotFoundError("script missing")}, 400, "script missing"),
        ("post", "/api/launcher/open", {"script_path": "missing.py"}, None, {"open_launcher_workspace": FileNotFoundError("script missing")}, 400, "script missing"),
        ("post", "/api/launcher/open", {"script_path": "train.py", "config_path": "bad.yaml"}, None, {"open_launcher_workspace": ValueError("bad launcher config")}, 400, "bad launcher config"),
        ("post", "/api/launcher/pick-script", None, None, {"pick_and_open_launcher_workspace": ValueError("cancelled")}, 400, "cancelled"),
        ("post", "/api/launcher/pick-script-path", None, None, {"pick_launcher_script_path": FileNotFoundError("picker unavailable")}, 400, "picker unavailable"),
        ("post", "/api/launcher/pick-config-path", {"script_path": "train.py"}, None, {"pick_launcher_config_path": ValueError("no config")}, 400, "no config"),
        ("post", "/api/launcher/pick-shell-root", None, None, {"pick_and_open_shell_workspace": ValueError("no shell root")}, 400, "no shell root"),
        ("post", "/api/launcher/open-shell-root", {"path": "missing"}, None, {"open_shell_workspace_at": ValueError("missing dir")}, 400, "missing dir"),
        ("post", "/api/tasks/reorder", {"items": [{"name": "ghost"}]}, None, {"reorder_tasks": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/reorder", {"items": []}, None, {"reorder_tasks": ValueError("empty order")}, 400, "empty order"),
        ("post", "/api/tasks/batch/run", {"task_names": ["ghost"]}, None, {"start_tasks_batch": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/batch/run", {"task_names": []}, None, {"start_tasks_batch": ValueError("empty batch")}, 400, "empty batch"),
        ("post", "/api/tasks/batch/delete", {"task_names": ["ghost"]}, None, {"delete_tasks_batch": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/batch/delete", {"task_names": []}, None, {"delete_tasks_batch": ValueError("empty delete")}, 400, "empty delete"),
        ("post", "/api/tasks/export/csv", {"task_names": ["ghost"]}, None, {"export_tasks_csv": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/export/csv", {"task_names": []}, None, {"export_tasks_csv": ValueError("empty export")}, 400, "empty export"),
        ("post", "/api/tasks/ghost/run", None, None, {"start_task": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/ghost/cancel", None, None, {"cancel_task": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/alpha/cancel", None, None, {"cancel_task": ValueError("not running")}, 400, "not running"),
        ("post", "/api/tasks/ghost/pin", {"pinned": True}, None, {"set_task_pin": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/alpha/pin", {"pinned": None}, None, {"set_task_pin": ValueError("pin required")}, 400, "pin required"),
        ("patch", "/api/tasks/ghost/notes", {"notes": "x", "expected_notes": ""}, None, {"update_task_notes": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("patch", "/api/tasks/alpha/notes", {"notes": "x", "expected_notes": ""}, None, {"update_task_notes": ValueError("bad notes")}, 400, "bad notes"),
        ("patch", "/api/tasks/alpha/notes", {"notes": "x", "expected_notes": ""}, None, {"update_task_notes": TaskNotesConflictError("notes changed")}, 409, "notes changed"),
        ("patch", "/api/tasks/ghost/env", {"env": {}, "expected_env": {}}, None, {"update_task_env": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("patch", "/api/tasks/alpha/env", {"env": {"BAD KEY": "x"}, "expected_env": {}}, None, {"update_task_env": ValueError("bad env")}, 400, "bad env"),
        ("patch", "/api/tasks/alpha/env", {"env": {}, "expected_env": {}}, None, {"update_task_env": TaskEnvConflictError("env changed")}, 409, "env changed"),
        ("post", "/api/tasks/ghost/rename", {"new_name": "beta"}, None, {"rename_task": KeyError("ghost")}, 404, "Task 'ghost' not found"),
        ("post", "/api/tasks/alpha/rename", {"new_name": "bad/name"}, None, {"rename_task": ValueError("bad name")}, 400, "bad name"),
        ("get", "/api/tasks/ghost/logs", None, None, {"get_task_logs": KeyError("ghost")}, 404, "Task 'ghost' not found"),
    ],
)
def test_api_routes_translate_runtime_errors_to_http_responses(
    method,
    path,
    json_body,
    params,
    runtime_results,
    expected_status,
    detail_part,
):
    client = TestClient(create_app(_RouteRuntime(runtime_results)))
    request = getattr(client, method)
    kwargs = {"params": params or {}}
    if json_body is not None:
        kwargs["json"] = json_body

    response = request(path, **kwargs)

    assert response.status_code == expected_status
    assert detail_part in response.json()["detail"]


def test_get_task_endpoint_returns_not_found_when_runtime_returns_none():
    client = TestClient(create_app(_RouteRuntime({"get_task": None})))

    response = client.get("/api/tasks/ghost")

    assert response.status_code == 404
    assert "Task 'ghost' not found" in response.json()["detail"]


def test_find_available_port_increments_when_start_port_is_busy():
    from pyruns.web import app as web_app

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as busy_socket:
        busy_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        busy_socket.bind(("127.0.0.1", 0))
        busy_socket.listen(1)
        busy_port = int(busy_socket.getsockname()[1])

        resolved_port = web_app.find_available_port(busy_port, host="127.0.0.1")

    assert resolved_port > busy_port


def test_main_uses_resolved_dynamic_port_for_server_and_browser(monkeypatch):
    from pyruns.web import app as web_app

    captured: dict[str, object] = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(web_app, "find_available_port", lambda port, host="127.0.0.1": 8101)
    monkeypatch.setattr(web_app, "_schedule_browser_open", lambda url: captured.update(browser_url=url))
    monkeypatch.setattr(web_app.uvicorn, "run", lambda app_target, **kwargs: captured.update(kwargs))

    web_app.main(
        open_browser=True,
        start_path="/generator?launcher=1",
        access_token="test-token",
    )

    assert captured["port"] == 8101
    assert captured["proxy_headers"] is False
    assert captured["browser_url"] == (
        "http://127.0.0.1:8101/generator?launcher=1&token=test-token"
    )


def test_main_explicit_port_overrides_workspace_setting(monkeypatch):
    from pyruns.web import app as web_app

    captured: dict[str, object] = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    def fake_find_available_port(port, host="127.0.0.1", max_attempts=100):
        captured["requested_port"] = port
        captured["host"] = host
        captured["max_attempts"] = max_attempts
        return port

    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(web_app, "find_available_port", fake_find_available_port)
    monkeypatch.setattr(web_app, "_schedule_browser_open", lambda url: captured.update(browser_url=url))
    monkeypatch.setattr(web_app.uvicorn, "run", lambda app_target, **kwargs: captured.update(kwargs))

    web_app.main(open_browser=True, port=9022, access_token="test-token")

    assert captured["requested_port"] == 9022
    assert captured["max_attempts"] == 0
    assert captured["port"] == 9022
    assert captured["browser_url"] == "http://127.0.0.1:9022/?token=test-token"


def test_main_flushes_private_url_before_blocking_server_run(monkeypatch):
    from pyruns.web import app as web_app

    printed = []

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )
    monkeypatch.setattr(
        web_app,
        "print",
        lambda *args, **kwargs: printed.append((args, kwargs)),
        raising=False,
    )
    monkeypatch.setattr(web_app.uvicorn, "run", lambda *_args, **_kwargs: None)

    web_app.main(open_browser=False, access_token="test-token")

    assert printed
    assert all(kwargs.get("flush") is True for _args, kwargs in printed)
    assert any("token=test-token" in str(args[0]) for args, _kwargs in printed)


def test_main_keeps_access_token_out_of_environment_without_reload(monkeypatch):
    from pyruns.web import app as web_app

    captured = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.delenv(web_app._UI_TOKEN_ENV, raising=False)
    monkeypatch.delenv(web_app._UI_COOKIE_NONCE_ENV, raising=False)
    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )

    def fake_run(app_target, **_kwargs):
        captured["app_target"] = app_target
        captured["token"] = web_app.os.environ.get(web_app._UI_TOKEN_ENV)
        captured["cookie_nonce"] = web_app.os.environ.get(web_app._UI_COOKIE_NONCE_ENV)
        captured["cookie_name"] = app_target.state.session_cookie_name

    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)

    web_app.main(open_browser=False, access_token="server-secret")

    assert not isinstance(captured["app_target"], str)
    assert captured["token"] is None
    assert captured["cookie_nonce"] is None
    assert captured["cookie_name"] == web_app._session_cookie_name(
        web_app._session_cookie_nonce_for_port(8099)
    )
    assert web_app._UI_TOKEN_ENV not in web_app.os.environ
    assert web_app._UI_COOKIE_NONCE_ENV not in web_app.os.environ


def test_main_limits_access_token_environment_to_reload_supervisor(monkeypatch):
    from pyruns.web import app as web_app

    captured = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.delenv(web_app._UI_TOKEN_ENV, raising=False)
    monkeypatch.delenv(web_app._UI_COOKIE_NONCE_ENV, raising=False)
    monkeypatch.delenv(web_app._UI_SESSION_STATE_ENV, raising=False)
    monkeypatch.delenv(web_app._UI_SESSION_SCOPE_ENV, raising=False)
    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )

    def fake_run(app_target, **_kwargs):
        captured["app_target"] = app_target
        captured["token"] = web_app.os.environ.get(web_app._UI_TOKEN_ENV)
        captured["cookie_nonce"] = web_app.os.environ.get(web_app._UI_COOKIE_NONCE_ENV)
        captured["session_state"] = web_app.os.environ.get(web_app._UI_SESSION_STATE_ENV)
        captured["session_scope"] = web_app.os.environ.get(web_app._UI_SESSION_SCOPE_ENV)

    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)

    web_app.main(reload=True, open_browser=False, access_token="reload-secret")

    assert captured["app_target"] == "pyruns.web.app:create_app"
    assert captured["token"] == "reload-secret"
    assert re.fullmatch(r"[0-9a-f]{32}", captured["cookie_nonce"])
    assert captured["cookie_nonce"] == web_app._session_cookie_nonce_for_port(8099)
    assert captured["session_state"]
    assert captured["session_scope"]
    assert web_app._UI_TOKEN_ENV not in web_app.os.environ
    assert web_app._UI_COOKIE_NONCE_ENV not in web_app.os.environ
    assert web_app._UI_SESSION_STATE_ENV not in web_app.os.environ
    assert web_app._UI_SESSION_SCOPE_ENV not in web_app.os.environ


def test_main_explicit_busy_port_fails_instead_of_silently_incrementing(monkeypatch):
    from pyruns.web import app as web_app

    events = []

    class DummyRuntime:
        settings = {"ui_port": 8099}

        def shutdown(self):
            events.append("shutdown")

    def reject_port(port, host="127.0.0.1", max_attempts=100):
        assert port == 9022
        assert host == "127.0.0.1"
        assert max_attempts == 0
        raise RuntimeError("unavailable")

    monkeypatch.setattr(web_app, "PyrunsRuntime", DummyRuntime)
    monkeypatch.setattr(web_app, "find_available_port", reject_port)

    with pytest.raises(RuntimeError, match="already in use; choose another with --port"):
        web_app.main(open_browser=False, port=9022)

    assert events == ["shutdown"]


def test_main_does_not_auto_open_browser_in_tmux(monkeypatch):
    from pyruns.web import app as web_app

    captured: dict[str, object] = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.delenv("PYRUNS_OPEN_BROWSER", raising=False)
    monkeypatch.delenv("PYRUNS_NO_BROWSER", raising=False)
    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )
    monkeypatch.setattr(web_app, "_schedule_browser_open", lambda url: captured.update(browser_url=url))
    monkeypatch.setattr(web_app.uvicorn, "run", lambda app_target, **kwargs: captured.update(kwargs))

    web_app.main()

    assert captured["port"] == 8099
    assert "browser_url" not in captured


def test_main_explicit_browser_overrides_tmux_default(monkeypatch):
    from pyruns.web import app as web_app

    captured: dict[str, object] = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(web_app, "find_available_port", lambda port, host="127.0.0.1": port)
    monkeypatch.setattr(web_app, "_schedule_browser_open", lambda url: captured.update(browser_url=url))
    monkeypatch.setattr(web_app.uvicorn, "run", lambda app_target, **kwargs: captured.update(kwargs))

    web_app.main(open_browser=True, access_token="test-token")

    assert captured["browser_url"] == "http://127.0.0.1:8099/?token=test-token"


def test_workspace_endpoint_returns_metadata(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with patch("pyruns.web.runtime.get_shell_runtime_for_workspace", return_value={
        "mode": "follow",
        "source": "follow_terminal",
        "terminal_kind": "powershell",
        "display_name": "PowerShell",
        "executable": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "available": True,
    }):
        response = client.get("/api/workspace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["run_root"].endswith("_pyruns_/main")
    assert payload["working_root"] == str(tmp_path).replace("\\", "/")
    assert payload["script_name"] == "main"
    assert payload["workspace_kind"] == WORKSPACE_KIND_SCRIPT
    assert payload["settings"]["shell_mode"] == "follow"
    assert payload["settings"]["monitor_sidebar_width_pct"] == 15
    assert payload["shell_runtime"]["mode"] == "follow"
    assert payload["shell_runtime"]["display_name"] == "PowerShell"
    assert payload["templates"]
    assert payload["workspace_ready"] is True


def test_workspace_endpoint_ignores_non_finite_settings_from_manual_yaml(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "monitor_line_height: .nan\nunknown_nested:\n  value: .inf\n",
        encoding="utf-8",
    )
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/workspace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["settings"]["monitor_line_height"] == 1.0
    assert "unknown_nested" not in payload["settings"]


def test_workspace_endpoint_reports_uninitialized_default_root_as_not_ready(tmp_path):
    workspace = tmp_path / "_pyruns_"
    (workspace / TASKS_DIR).mkdir(parents=True)
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/workspace")

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_kind"] == WORKSPACE_KIND_SCRIPT
    assert payload["workspace_ready"] is False
    assert payload["script_name"] == ""
    assert payload["script_path"] == ""
    assert payload["working_root"] == ""


def test_workspace_endpoint_reports_native_picker_capability(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with patch("pyruns.web.runtime.native_picker_available", return_value=False):
        response = client.get("/api/workspace")

    assert response.status_code == 200
    assert response.json()["native_file_picker"] is False


def test_runtime_endpoint_lists_conda_envs(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    class Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(command, **kwargs):
        if command[1:3] == ["info", "--json"]:
            return Result(json.dumps({"root_prefix": "/opt/conda"}))
        if command[1:4] == ["env", "list", "--json"]:
            return Result(json.dumps({"envs": ["/opt/conda", "/opt/conda/envs/eval"]}))
        raise AssertionError(command)

    monkeypatch.setattr("pyruns.web.runtime.shutil.which", lambda value: "/opt/conda/bin/conda" if value == "conda" else "")
    monkeypatch.setattr("pyruns.web.runtime.subprocess.run", fake_run)

    response = client.get("/api/runtime")

    assert response.status_code == 200
    payload = response.json()
    assert payload["conda"]["available"] is True
    assert [item["name"] for item in payload["conda"]["envs"]] == ["base", "eval"]


def test_runtime_endpoint_uses_conda_exe_from_process_env(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    fake_conda = tmp_path / "conda"
    fake_conda.write_text("", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    class Result:
        def __init__(self, stdout: str):
            self.returncode = 0
            self.stdout = stdout
            self.stderr = ""

    def fake_run(command, **kwargs):
        assert command[0] == str(fake_conda.resolve())
        if command[1:3] == ["info", "--json"]:
            return Result(json.dumps({"root_prefix": "/opt/conda"}))
        if command[1:4] == ["env", "list", "--json"]:
            return Result(json.dumps({"envs": ["/opt/conda/envs/py310"]}))
        raise AssertionError(command)

    monkeypatch.setenv("CONDA_EXE", str(fake_conda))
    monkeypatch.setattr("pyruns.web.runtime.subprocess.run", fake_run)

    response = client.get("/api/runtime")

    assert response.status_code == 200
    payload = response.json()
    assert payload["conda"]["available"] is True
    assert payload["conda"]["executable"] == str(fake_conda.resolve())
    assert [item["name"] for item in payload["conda"]["envs"]] == ["py310"]


def test_runtime_update_persists_runtime_and_global_env(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    fake_conda = tmp_path / "conda.exe"
    fake_conda.write_text("", encoding="utf-8")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": str(fake_conda.resolve()),
        "envs": [],
        "error": "",
    })
    monkeypatch.delenv(ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={
            "conda_env": "eval",
            "conda_executable": str(fake_conda),
            "python_executable": "",
            "global_env": {"CUDA_VISIBLE_DEVICES": "0", "TOKENIZERS_PARALLELISM": "false"},
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["conda_env"] == "eval"
    assert payload["global_env"]["CUDA_VISIBLE_DEVICES"] == "0"
    settings_text = (workspace.parent / "_pyruns_settings.yaml").read_text(encoding="utf-8")
    assert "conda_env: eval" in settings_text
    assert "CUDA_VISIBLE_DEVICES" in settings_text

    task_dir = workspace / TASKS_DIR / "eval_task"
    task_dir.mkdir(parents=True)
    python_runtime = _resolve_python_runtime(str(task_dir))
    assert python_runtime == {
        "mode": "conda",
        "source": "workspace_settings",
        "conda_env": "eval",
        "conda_executable": str(fake_conda.resolve()),
    }

    script_path = tmp_path / "main.py"
    with patch("pyruns.utils.parse_utils.detect_config_source_fast", return_value=("pyruns_load", None)):
        command, _, _ = _build_command(
            None,
            str(script_path),
            None,
            {},
            task_dir=str(task_dir),
            python_runtime=python_runtime,
        )

    assert command[:6] == [
        str(fake_conda.resolve()),
        "run",
        "-n",
        "eval",
        "--no-capture-output",
        "python",
    ]


@pytest.mark.parametrize("global_env", [{"BAD=KEY": "x"}, {"GOOD": "bad\x00value"}])
def test_runtime_update_rejects_invalid_global_environment(tmp_path, global_env):
    workspace = _make_workspace(tmp_path, "main")
    client = TestClient(create_app(_build_runtime(workspace)))

    response = client.patch("/api/runtime", json={"global_env": global_env})

    assert response.status_code == 400


def test_runtime_update_persists_gpu_scheduler_settings(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={
            "gpu_scheduler": {
                "enabled": True,
                "task_mode": "multi",
                "selection_mode": "specified",
                "gpus_per_task": 2,
                "device_ids": "0,1",
                "memory_used_pct": 75,
                "min_free_memory_gb": 8,
                "compute_used_pct": 30,
                "stable_seconds": 6,
                "max_wait_seconds": 86400,
                "max_tasks_per_gpu": 1,
                "respect_cuda_visible_devices": True,
                "require_same_gpu_model": True,
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()["gpu_scheduler"]
    assert payload["enabled"] is True
    assert payload["task_mode"] == "multi"
    assert payload["selection_mode"] == "specified"
    assert payload["gpus_per_task"] == 2
    assert payload["device_ids"] == [0, 1]
    assert payload["max_wait_seconds"] == 86400.0
    assert payload["require_same_gpu_model"] is True
    settings_text = (workspace.parent / "_pyruns_settings.yaml").read_text(encoding="utf-8")
    assert "gpu_scheduler_enabled: true" in settings_text
    assert "gpu_scheduler_task_mode: multi" in settings_text
    assert "gpu_scheduler_selection_mode: specified" in settings_text
    assert "gpu_scheduler_require_same_gpu_model: true" in settings_text
    assert "- 0" in settings_text


def test_runtime_update_multi_gpu_scheduler_allows_one_gpu_limit(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={
            "gpu_scheduler": {
                "enabled": True,
                "task_mode": "multi",
                "gpus_per_task": 1,
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()["gpu_scheduler"]
    assert payload["task_mode"] == "multi"
    assert payload["gpus_per_task"] == 1
    settings_text = (workspace.parent / "_pyruns_settings.yaml").read_text(encoding="utf-8")
    assert "gpu_scheduler_gpus_per_task: 1" in settings_text


def test_runtime_update_skips_provider_refresh_by_default(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    calls = []

    def fake_list_conda_envs(*, refresh=True):
        calls.append(refresh)
        if refresh:
            raise AssertionError("runtime update should not refresh conda providers by default")
        return {
            "available": False,
            "executable": "conda",
            "envs": [],
            "error": "",
        }

    monkeypatch.setattr(runtime, "list_conda_envs", fake_list_conda_envs)
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={"gpu_scheduler": {"enabled": True}},
    )

    assert response.status_code == 200
    assert response.json()["gpu_scheduler"]["enabled"] is True
    assert calls == [False]


def test_runtime_update_can_refresh_providers_when_requested(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    calls = []

    def fake_list_conda_envs(*, refresh=True):
        calls.append(refresh)
        return {
            "available": True,
            "executable": "conda",
            "envs": [{"name": "eval", "path": "/envs/eval", "python_executable": "/envs/eval/bin/python"}],
            "error": "",
        }

    monkeypatch.setattr(runtime, "list_conda_envs", fake_list_conda_envs)
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime?refresh_providers=true",
        json={"conda_env": "eval"},
    )

    assert response.status_code == 200
    assert response.json()["conda"]["available"] is True
    assert calls == [True]


def test_runtime_update_gpu_scheduler_sanitizes_limits_with_scheduler_defaults(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={
            "gpu_scheduler": {
                "selection_mode": "bad",
                "device_ids": "0,0,2",
                "memory_used_pct": 250,
                "compute_used_pct": -5,
                "min_free_memory_gb": "bad",
                "stable_seconds": "bad",
                "max_wait_seconds": "bad",
                "max_tasks_per_gpu": "bad",
                "require_same_gpu_model": "yes",
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()["gpu_scheduler"]
    assert payload["selection_mode"] == "auto"
    assert payload["device_ids"] == [0, 2]
    assert payload["memory_used_pct"] == 100.0
    assert payload["compute_used_pct"] == 0.0
    assert payload["min_free_memory_gb"] == 40.0
    assert payload["stable_seconds"] == 15.0
    assert payload["max_wait_seconds"] == 172800.0
    assert payload["max_tasks_per_gpu"] == 1
    assert payload["require_same_gpu_model"] is True
    assert "sample_interval_seconds" not in payload
    settings_text = (workspace.parent / "_pyruns_settings.yaml").read_text(encoding="utf-8")
    assert "gpu_scheduler_sample_interval_seconds" not in settings_text


def test_runtime_update_gpu_scheduler_clamps_stable_seconds_minimum(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={"gpu_scheduler": {"stable_seconds": 0}},
    )

    assert response.status_code == 200
    assert response.json()["gpu_scheduler"]["stable_seconds"] == 1.0
    settings_text = (workspace.parent / "_pyruns_settings.yaml").read_text(encoding="utf-8")
    assert "gpu_scheduler_stable_seconds: 1.0" in settings_text


def test_runtime_update_gpu_scheduler_rejects_non_finite_numeric_values(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={
            "gpu_scheduler": {
                "gpus_per_task": "Infinity",
                "memory_used_pct": "NaN",
                "min_free_memory_gb": "Infinity",
                "compute_used_pct": "-Infinity",
                "stable_seconds": "Infinity",
                "max_wait_seconds": "NaN",
                "max_tasks_per_gpu": "Infinity",
            }
        },
    )

    assert response.status_code == 200
    payload = response.json()["gpu_scheduler"]
    assert payload["gpus_per_task"] == 1
    assert payload["memory_used_pct"] == 40.0
    assert payload["min_free_memory_gb"] == 40.0
    assert payload["compute_used_pct"] == 30.0
    assert payload["stable_seconds"] == 15.0
    assert payload["max_wait_seconds"] == 172800.0
    assert payload["max_tasks_per_gpu"] == 1


def test_runtime_get_task_logs_prefers_queue_log_for_queued_tasks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "gpu_wait", status="queued", log_text="completed run\n")
    task_dir = workspace / TASKS_DIR / "gpu_wait"
    update_task_info(
        str(task_dir),
        lambda info: info.update({
            "status": "queued",
            "run_index": 1,
        }),
    )
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("[PYRUNS] GPU WAIT\nwaiting for GPU resources\n", encoding="utf-8")
    runtime = _build_runtime(workspace)

    payload = runtime.get_task_logs("gpu_wait", tail_lines=20)

    assert payload["selected_log"] == "queue.log"
    assert payload["available_logs"][0] == "queue.log"
    assert "run1.log" in payload["available_logs"]
    assert "run2.log" not in payload["available_logs"]
    assert "waiting for GPU resources" in payload["content"]


def test_runtime_get_task_logs_prefers_latest_run_when_gpu_task_completed(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "gpu_done", status="completed", log_text="run output\n")
    task_dir = workspace / TASKS_DIR / "gpu_done"
    update_task_info(
        str(task_dir),
        lambda info: info.update({
            "status": "completed",
            "run_index": 1,
        }),
    )
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("[PYRUNS] queue summary\n", encoding="utf-8")
    runtime = _build_runtime(workspace)

    payload = runtime.get_task_logs("gpu_done", tail_lines=20)

    assert payload["selected_log"] == "run1.log"
    assert payload["available_logs"][0] == "queue.log"
    assert "run output" in payload["content"]
    assert "queue summary" not in payload["content"]

    queue_payload = runtime.get_task_logs("gpu_done", log_file_name="queue.log", tail_lines=20)

    assert queue_payload["selected_log"] == "queue.log"
    assert "queue summary" in queue_payload["content"]


def test_runtime_get_task_logs_returns_queue_log_without_presentation_rewrite(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "gpu_wait", status="queued", log_text="completed run\n")
    task_dir = workspace / TASKS_DIR / "gpu_wait"
    update_task_info(
        str(task_dir),
        lambda info: info.update({
            "status": "queued",
            "run_index": 1,
        }),
    )
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_bytes(
        (
            "[PYRUNS] ================= GPU WAIT =================\n"
            "[PYRUNS] Updated at 2026-06-09_21-17-30\n"
            "[PYRUNS] waiting\n"
            "[PYRUNS] ============================================\n"
            "[PYRUNS] Last status at 2026-06-09_21-17-30: waiting\r"
            "[PYRUNS] -------------------- RUN #2 --------------------\n\n"
            "[PYRUNS] ================= GPU ASSIGNED =================\n"
            "[PYRUNS] Updated at 2026-06-09_21-17-45\n"
        ).encode("utf-8")
    )
    runtime = _build_runtime(workspace)

    payload = runtime.get_task_logs("gpu_wait", tail_lines=20)

    content = payload["content"]
    assert "[PYRUNS] ================= GPU WAIT =================" in content
    assert "[PYRUNS] Last status at 2026-06-09_21-17-30: waiting" in content
    assert "[PYRUNS] -------------------- RUN #2 --------------------" in content
    assert "[PYRUNS] ================= GPU ASSIGNED =================" in content
    assert "[PYRUNS] [GPU WAIT]" not in content


def test_runtime_get_task_logs_includes_missing_active_run_log_for_running_task(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "running_gpu", status="running", log_text="first run\n")
    task_dir = workspace / TASKS_DIR / "running_gpu"
    update_task_info(str(task_dir), lambda info: info.update({"status": "running", "run_index": 2}))
    runtime = _build_runtime(workspace, owns_task_lifecycle=False)

    payload = runtime.get_task_logs("running_gpu", log_file_name="run2.log", tail_lines=20)

    assert payload["selected_log"] == "run2.log"
    assert "run1.log" in payload["available_logs"]
    assert "run2.log" in payload["available_logs"]
    assert payload["content"] == ""


def test_runtime_get_task_logs_does_not_invent_missing_non_active_run_log(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "running_gpu", status="running", log_text="first run\n")
    task_dir = workspace / TASKS_DIR / "running_gpu"
    update_task_info(str(task_dir), lambda info: info.update({"status": "running", "run_index": 2}))
    runtime = _build_runtime(workspace, owns_task_lifecycle=False)

    payload = runtime.get_task_logs("running_gpu", log_file_name="run3.log", tail_lines=20)

    assert payload["selected_log"] == "run3.log"
    assert "run1.log" in payload["available_logs"]
    assert "run2.log" not in payload["available_logs"]
    assert "run3.log" not in payload["available_logs"]
    assert payload["content"] == ""


def test_runtime_update_parses_shell_like_global_env_text(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={
            "global_env_text": "\n".join([
                "# workspace env",
                "CUDA_VISIBLE_DEVICES=0",
                "export TOKENIZERS_PARALLELISM=false",
                "HF_HOME='/data/hf cache'",
                'RUN_NAME="smoke run"',
                "EMPTY_VALUE=",
                "LITERAL_HASH=a#b",
                "COMMENTED=value # ignored",
            ]),
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["global_env"] == {
        "CUDA_VISIBLE_DEVICES": "0",
        "TOKENIZERS_PARALLELISM": "false",
        "HF_HOME": "/data/hf cache",
        "RUN_NAME": "smoke run",
        "EMPTY_VALUE": "",
        "LITERAL_HASH": "a#b",
        "COMMENTED": "value",
    }


def test_runtime_update_rejects_invalid_global_env_text(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))

    response = client.patch(
        "/api/runtime",
        json={"global_env_text": "BAD LINE WITHOUT EQUALS"},
    )

    assert response.status_code == 400
    assert "expected KEY=value" in response.json()["detail"]


def test_runtime_update_validates_whole_payload_before_atomic_save(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime, "list_conda_envs", lambda refresh=True: {
        "available": False,
        "executable": "conda",
        "envs": [],
        "error": "",
    })
    client = TestClient(create_app(runtime))
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    before = settings_path.read_bytes()

    response = client.patch(
        "/api/runtime",
        json={
            "python_executable": "python-before-validation",
            "global_env_text": "BAD LINE WITHOUT EQUALS",
        },
    )

    assert response.status_code == 400
    assert settings_path.read_bytes() == before


def test_parse_global_env_text_handles_shell_assignment_edges():
    env = parse_global_env_text(
        "\n".join([
            'QUOTED_HASH="a # b"',
            "SINGLE_HASH='x # y'",
            "HAS_EQUALS=a=b=c",
            r"WINDOWS_PATH=C:\Users\me\data",
            r"ESCAPED_HASH=a\#b",
            r"ESCAPED_SPACE=a\ b",
            "INLINE_COMMENT=value # dropped",
        ])
    )

    assert env == {
        "QUOTED_HASH": "a # b",
        "SINGLE_HASH": "x # y",
        "HAS_EQUALS": "a=b=c",
        "WINDOWS_PATH": r"C:\Users\me\data",
        "ESCAPED_HASH": "a#b",
        "ESCAPED_SPACE": "a b",
        "INLINE_COMMENT": "value",
    }


def test_parse_global_env_text_rejects_unsafe_or_ambiguous_lines():
    invalid_texts = [
        "1BAD=value",
        "HAS SPACE=value",
        "UNQUOTED_SPACE=a b",
        'UNCLOSED="value',
    ]

    for text in invalid_texts:
        try:
            parse_global_env_text(text)
        except ValueError:
            pass
        else:
            raise AssertionError(f"Expected ValueError for {text!r}")


def test_root_serves_react_frontend_shell(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Pyruns</title>" in response.text
    assert '<div id="root"></div>' in response.text
    assert "assets/" in response.text


def test_unknown_api_get_returns_json_404(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/does-not-exist")

    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["detail"] == "Not Found"


def test_frontend_fallback_handles_windows_drive_path(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/C:/Windows/win.ini")

    assert response.status_code == 200
    assert "<title>Pyruns</title>" in response.text


def test_dashboard_endpoint_returns_summary_and_recent_tasks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    _add_task(workspace, "beta", status="failed")
    _add_task(workspace, "gamma", status="cancelled")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 3
    assert payload["summary"]["running"] == 1
    assert payload["summary"]["failed"] == 1
    assert payload["summary"]["cancelled"] == 1
    assert payload["recent_tasks"][0]["name"] in {"alpha", "beta", "gamma"}


def test_tasks_endpoint_can_return_lightweight_summaries(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="completed")
    task_dir = workspace / TASKS_DIR / "alpha"
    long_value = "x" * (DEFAULT_TASK_SUMMARY_SEARCH_TEXT_CHARS + 128)
    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "records": [{"loss": index} for index in range(20)],
                "tracks": [{"loss": list(range(20))}],
                "launch_command": "python train.py --lr 0.01",
                "launch_workdir": "/workspace",
                "launch_started_at": 1773748800.0,
                "launch_run_index": 1,
            }
        ),
    )
    (task_dir / CONFIG_FILENAME).write_text(
        f"payload: {long_value}\ntail_key: tail-value\n",
        encoding="utf-8",
    )
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    full = client.get("/api/tasks", params={"limit": 1, "refresh": True})
    summary = client.get("/api/tasks", params={"limit": 1, "refresh": True, "summary": True})
    matched = client.get(
        "/api/tasks",
        params={"limit": 1, "refresh": True, "summary": True, "query": "tail_key: tail-value"},
    )
    compact = client.get(
        "/api/tasks",
        params={
            "limit": 1,
            "refresh": False,
            "summary": True,
            "compact": True,
            "query": "tail_key: tail-value",
        },
    )

    assert full.status_code == 200
    assert summary.status_code == 200
    assert matched.status_code == 200
    assert compact.status_code == 200
    full_item = full.json()["items"][0]
    summary_item = summary.json()["items"][0]
    assert full_item["config"]
    assert full_item["records"]
    assert full_item["tracks"]
    assert full_item["launch_command"] == "python train.py --lr 0.01"
    assert full_item["launch_workdir"] == "/workspace"
    assert full_item["launch_started_at"] == 1773748800.0
    assert full_item["launch_run_index"] == 1
    assert summary_item["config"] == {}
    assert summary_item["config_text"] == ""
    assert summary_item["records"] == []
    assert summary_item["tracks"] == []
    assert summary_item["preview_text"]
    assert len(summary_item["search_text"]) <= DEFAULT_TASK_SUMMARY_SEARCH_TEXT_CHARS
    assert summary_item["search_text"].startswith("alpha")
    assert "tail_key:tail-value" in summary_item["search_text"]
    assert matched.json()["total"] == 1
    assert matched.json()["items"][0]["name"] == "alpha"
    compact_item = compact.json()["items"][0]
    assert compact_item["name"] == "alpha"
    assert compact_item["search_text"] == ""
    assert compact_item["preview_text"] == ""
    assert compact_item["notes"] == ""
    assert compact_item["env"] == {}
    assert compact_item["start_times"] == []
    assert compact_item["launch_command"] is None
    assert compact_item["launch_workdir"] is None
    assert compact_item["launch_started_at"] is None
    assert compact_item["launch_run_index"] is None
    assert len(compact.content) * 5 < len(summary.content)
    assert matched.json()["status_counts"] == {
        "pending": 0,
        "queued": 0,
        "running": 0,
        "completed": 1,
        "failed": 0,
        "cancelled": 0,
    }


def test_tasks_endpoint_status_counts_are_global_before_filters_and_pagination(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="completed")
    _add_task(workspace, "beta", status="failed")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get(
        "/api/tasks",
        params={"query": "alpha", "status": "Completed", "offset": 0, "limit": 1},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert [item["name"] for item in payload["items"]] == ["alpha"]
    assert payload["status_counts"]["completed"] == 1
    assert payload["status_counts"]["failed"] == 1


def test_compact_task_search_returns_field_context_without_scanning_logs(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", log_text="log-only-token\n")
    task_dir = workspace / TASKS_DIR / "alpha"
    update_task_info(
        str(task_dir),
        lambda info: info.update({"notes": "Owner: Research\nNeeds REVIEW before launch"}),
    )
    save_yaml(
        str(task_dir / CONFIG_FILENAME),
        {"model": {"name": "ResNet50"}, "batch_size": 32},
    )
    client = TestClient(create_app(_build_runtime(workspace)))

    notes_response = client.get(
        "/api/tasks",
        params={"query": "review", "summary": True, "compact": True, "limit": 10},
    )
    config_response = client.get(
        "/api/tasks",
        params={"query": "name: resnet50", "summary": True, "compact": True, "limit": 10},
    )
    log_response = client.get(
        "/api/tasks",
        params={"query": "log-only-token", "summary": True, "compact": True, "limit": 10},
    )

    assert notes_response.status_code == 200
    notes_match = notes_response.json()["items"][0]["search_matches"][0]
    assert notes_response.json()["items"][0]["search_match_count"] == 1
    assert notes_match["field"] == "notes"
    assert notes_match["location"] == "Line 2"
    assert notes_match["snippet"][notes_match["match_start"]:notes_match["match_end"]] == "REVIEW"

    assert config_response.status_code == 200
    config_match = config_response.json()["items"][0]["search_matches"][0]
    assert config_response.json()["items"][0]["search_match_count"] == 1
    assert config_match["field"] == "config"
    assert config_match["location"] == "model.name"
    assert "ResNet50" in config_match["snippet"]

    assert log_response.status_code == 200
    assert log_response.json()["total"] == 0
    assert log_response.json()["items"] == []


@pytest.mark.parametrize("summary,compact", [(False, False), (True, False), (True, True)])
def test_task_search_fields_filter_results_previews_and_pagination(tmp_path, summary, compact):
    workspace = _make_workspace(tmp_path, "main")
    names = {"name": "needle-name", "notes": "by-note", "config": "by-config",
             "script": "by-script", "log": "by-log"}
    for field, name in names.items():
        _add_task(workspace, name, status="completed", log_text="needle\n" if field == "log" else "")
    note_dir = workspace / TASKS_DIR / names["notes"]
    update_task_info(str(note_dir), lambda info: info.update({"notes": "NEEDLE\nsecond-line"}))
    save_yaml(str(workspace / TASKS_DIR / names["config"] / CONFIG_FILENAME), {"model": {"tag": "needle"}})
    script_dir = workspace / TASKS_DIR / names["script"]
    (script_dir / SHELL_CONFIG_FILENAME).write_text("echo needle\n", encoding="utf-8")
    update_task_info(str(script_dir), lambda info: info.update({
        "task_kind": TASK_KIND_SHELL, "config_file": SHELL_CONFIG_FILENAME,
    }))
    _add_task(workspace, "needle-mixed", status="completed", log_text="needle\n")
    mixed_dir = workspace / TASKS_DIR / "needle-mixed"
    update_task_info(str(mixed_dir), lambda info: info.update({"notes": "needle"}))
    save_yaml(str(mixed_dir / CONFIG_FILENAME), {"tag": "needle"})
    _add_task(workspace, "unmatched")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    params = {"query": "needle", "summary": summary, "compact": compact,
              "include_logs": True, "sort": "name_asc", "refresh": False}
    with patch.object(runtime._log_search, "search", wraps=runtime._log_search.search) as scan:
        for field, name in names.items():
            before = scan.call_count
            response = client.get("/api/tasks", params={**params, "search_field": field})
            assert response.status_code == 200
            page = response.json()
            expected = sorted([name] + (["needle-mixed"] if field != "script" else []))
            assert page["total"] == len(expected)
            assert [task["name"] for task in page["items"]] == expected
            if not summary:
                own_task = next(task for task in page["items"] if task["name"] == name)
                if field == "config":
                    assert own_task["config"] == {"model": {"tag": "needle"}}
                elif field == "script":
                    assert own_task["config_text"] == (script_dir / SHELL_CONFIG_FILENAME).read_bytes().decode("utf-8")
            if summary or field == "log":
                for item in page["items"]:
                    assert item["search_match_count"] == 1
                    assert {m["field"] for m in item["search_matches"]} == {field}
            if field != "log":
                assert scan.call_count == before
            assert page["status_counts"]["pending"] == 1

        # All is the default and pagination/counts cover hits from every source.
        for offset, name in enumerate(sorted([*names.values(), "needle-mixed"])):
            page = client.get("/api/tasks", params={**params, "offset": offset, "limit": 1}).json()
            assert page["total"] == 6
            assert [task["name"] for task in page["items"]] == [name]
            assert page["has_more"] == (offset < 5)

        # Each query line must match the selected field, never another field.
        mixed = client.get("/api/tasks", params={**params, "query": "by-log\nneedle", "search_field": "log"}).json()
        assert mixed["total"] == 0
        notes = client.get("/api/tasks", params={**params, "query": "needle\nsecond-line", "search_field": "notes"}).json()
        assert notes["total"] == 1
        assert client.get("/api/tasks", params={**params, "status": "pending", "search_field": "notes"}).json()["total"] == 0
        before = scan.call_count
        assert client.get("/api/tasks", params={**params, "query": "", "search_field": "log"}).json()["total"] == 7
        assert scan.call_count == before
    assert client.get("/api/tasks", params={**params, "search_field": "unknown"}).status_code == 422


@pytest.mark.parametrize("field", ["name", "notes", "config", "script", "env", "log"])
def test_search_match_options_combine_with_each_field(tmp_path, field):
    import itertools

    workspace = _make_workspace(tmp_path, "main")
    text = "Token token tokenize token_1 xToken tokenx"
    name = text.replace(" ", "-") if field == "name" else "source"
    _add_task(workspace, name, log_text=text + "\r\n" if field == "log" else "")
    task_dir = workspace / TASKS_DIR / name
    if field == "notes":
        update_task_info(str(task_dir), lambda info: info.update({"notes": text}))
    elif field == "env":
        update_task_info(str(task_dir), lambda info: info.update({"env": {"SEARCH_PAYLOAD": text}}))
    elif field == "config":
        save_yaml(str(task_dir / CONFIG_FILENAME), {"payload": text})
    elif field == "script":
        (task_dir / SHELL_CONFIG_FILENAME).write_text(f"echo {text}\n", encoding="utf-8")
        update_task_info(str(task_dir), lambda info: info.update({"task_kind": TASK_KIND_SHELL, "config_file": SHELL_CONFIG_FILENAME}))
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    for match_case, whole_word, use_regex in itertools.product([False, True], repeat=3):
        params = {"query": "Toke[n]" if use_regex else "Token", "include_logs": True,
                  "search_field": field, "match_case": match_case, "whole_word": whole_word, "use_regex": use_regex}
        response = client.get("/api/tasks", params=params)
        assert response.status_code == 200, response.text
        item = response.json()["items"][0]
        if field == "config":
            assert item["config"] == {"payload": text}
        elif field == "script":
            assert item["config_text"] == (task_dir / SHELL_CONFIG_FILENAME).read_bytes().decode("utf-8")
        expected = (1 if match_case else 2) if whole_word else (2 if match_case else 6)
        assert item["search_match_count"] == expected
        assert {match["field"] for match in item["search_matches"]} == {field}
        assert all(match["snippet"][match["match_start"]:match["match_end"]] in {"token", "Token"} for match in item["search_matches"])
        params["search_field"] = "all"
        assert client.get("/api/tasks", params=params).json()["items"][0]["search_match_count"] == expected
    if field == "env":
        # Env key/value syntax is searchable, including the metadata-only API.
        response = client.get("/api/tasks", params={"query": "SEARCH_PAYLOAD=Token", "search_field": "env", "summary": True})
        assert response.json()["total"] == 1


@pytest.mark.parametrize("field", ["notes", "log"])
def test_invalid_and_slow_regex_report_errors_and_release_search_slots(tmp_path, monkeypatch, field):
    from pyruns.utils import search_query

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "source", log_text="A" * 3000 + "!\n")
    update_task_info(str(workspace / TASKS_DIR / "source"), lambda info: info.update({"notes": "A" * 3000 + "!"}))
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    params = {"search_field": field, "include_logs": True, "use_regex": True}
    invalid = client.get("/api/tasks", params={**params, "query": "("})
    assert invalid.status_code == 422
    assert "Invalid regular expression" in invalid.json()["detail"]
    monkeypatch.setattr(search_query, "REGEX_TIMEOUT_SECONDS", 0.001)
    timed_out = client.get("/api/tasks", params={**params, "query": "(A|AA)+$"})
    assert timed_out.status_code == 422
    assert "too long" in timed_out.json()["detail"]
    assert client.get("/api/tasks", params={**params, "query": "A", "use_regex": False}).json()["total"] == 1
    assert runtime._log_search.slots.acquire(blocking=False)
    assert runtime._log_search.slots.acquire(blocking=False)
    runtime._log_search.slots.release()
    runtime._log_search.slots.release()


@pytest.mark.parametrize("summary", [False, True])
def test_task_search_only_copies_payloads_for_returned_page(tmp_path, summary):
    class UncopiedHistory(list):
        def __deepcopy__(self, memo):
            raise AssertionError("Search must not copy run history outside its result page")

    workspace = _make_workspace(tmp_path, "main")
    for name in ("needle-a", "needle-b", "other"):
        _add_task(workspace, name)
    runtime = _build_runtime(workspace)
    runtime.ensure_tasks_loaded(full_refresh=False)
    manager = runtime.task_manager
    with manager._lock:
        manager._tasks_by_name["needle-a"]["run_environments"] = [{"host": "original"}]
        for name in ("needle-b", "other"):
            manager._tasks_by_name[name]["run_environments"] = UncopiedHistory([{"host": "large-history"}])

    page = runtime.search_tasks(query="needle", search_field="name", sort_mode="name_asc", limit=1,
                                include_logs=False, summary=summary, cancelled=threading.Event())
    assert page.total == 2 and page.has_more
    assert [task["name"] for task in page.items] == ["needle-a"]
    page.items[0]["run_environments"][0]["host"] = "changed"
    assert manager.get_task("needle-a")["run_environments"] == [{"host": "original"}]


@pytest.mark.parametrize("include_logs", [False, True], ids=["metadata", "log-search"])
def test_search_api_honors_refresh_for_externally_created_tasks(tmp_path, include_logs):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "first")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    assert runtime.list_tasks(summary=True).total == 1

    _add_task(workspace, "second")
    with runtime._lock:
        runtime._last_full_refresh_time = time.monotonic() + 60
    params = {"query": "second", "search_field": "name", "include_logs": include_logs, "summary": True}
    assert client.get("/api/tasks", params={**params, "refresh": False}).json()["total"] == 0
    assert client.get("/api/tasks", params={**params, "refresh": True}).json()["total"] == 0
    response = client.get("/api/tasks", params={**params, "force_refresh": True})
    assert response.status_code == 200
    assert [task["name"] for task in response.json()["items"]] == ["second"]


def test_initial_task_load_does_not_parse_metadata_twice(tmp_path):
    import pyruns.core.task_manager as task_manager_module

    workspace = _make_workspace(tmp_path, "main")
    for name in ("first", "second"):
        _add_task(workspace, name)
    runtime = _build_runtime(workspace)
    with patch.object(task_manager_module, "load_task_info", wraps=task_manager_module.load_task_info) as load:
        assert runtime.list_tasks(summary=True).total == 2
    assert load.call_count == 2


def test_task_refresh_interval_ignores_wall_clock_changes(tmp_path):
    import pyruns.web.runtime as runtime_module

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "first")
    runtime = _build_runtime(workspace)
    assert runtime.list_tasks(summary=True).total == 1

    _add_task(workspace, "second")
    clock = MagicMock()
    clock.time.return_value = -1_000_000_000.0
    clock.monotonic.return_value = runtime._last_full_refresh_time + 5.0
    with patch.object(runtime_module, "time", clock):
        assert runtime.list_tasks(summary=True).total == 2


def test_metadata_search_cancels_between_lines_without_holding_task_lock(tmp_path, monkeypatch):
    from concurrent.futures import CancelledError
    from pyruns.utils.search_query import SearchQuery

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "source")
    update_task_info(str(workspace / TASKS_DIR / "source"), lambda info: info.update({"notes": "line\n" * 1000}))
    runtime = _build_runtime(workspace)
    cancelled = threading.Event()
    original = SearchQuery.found
    calls = []

    def cancel_during_search(matcher, text):
        calls.append(text)
        if len(calls) == 1:
            acquired = []

            def check_lock():
                if runtime.task_manager._lock.acquire(timeout=0.5):
                    acquired.append(True)
                    runtime.task_manager._lock.release()

            worker = threading.Thread(target=check_lock)
            worker.start()
            worker.join(timeout=1)
            assert acquired == [True]
        result = original(matcher, text)
        cancelled.set()
        return result

    monkeypatch.setattr(SearchQuery, "found", cancel_during_search)
    with pytest.raises(CancelledError):
        runtime.search_tasks(query="absent", search_field="notes", cancelled=cancelled)
    assert len(calls) == 2


def test_regex_log_context_preserves_anchors_unicode_ansi_and_long_line_offsets(tmp_path, monkeypatch):
    from pyruns.utils import log_search
    from pyruns.utils.search_query import SearchQuery

    path = tmp_path / "run1.log"
    prefix = "x" * 65536 + "\r\n" + "前缀 "
    payload = (prefix + "\x1b[31mToken42\x1b[0m 后缀\r\n").encode("utf-8")
    path.write_bytes(payload)
    matcher = SearchQuery(r"(?<=前缀 )Token\d+(?= 后缀$)", use_regex=True, match_case=True)
    result = log_search.LogSearch._search_file_patterns(str(path), path.name, len(payload), matcher, threading.Event())
    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["line"] == 2
    assert match["snippet"][match["match_start"]:match["match_end"]] == "Token42"
    assert 0 <= payload.index(b"Token42") - match["offset"] <= 256
    assert SearchQuery(r"\S+", use_regex=True).scan(" a B ")["match_count"] == 2
    assert SearchQuery("^", use_regex=True).scan("")["spans"] == [(0, 0)]
    monkeypatch.setattr(log_search, "_MAX_PATTERN_LINE_CHARS", 32)
    with pytest.raises(ValueError, match="line exceeds"):
        log_search.LogSearch._search_file_patterns(str(path), path.name, len(payload), matcher, threading.Event())


def test_full_log_search_finds_history_outside_terminal_tail_and_opens_context(tmp_path):
    from pyruns._config import RUN_LOGS_DIR

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", log_text="early-token\n" + "ordinary output\n" * 350_000 + "late-token\n")
    log_dir = workspace / TASKS_DIR / "alpha" / RUN_LOGS_DIR
    (log_dir / "run2.log").write_text("historical-token\n", encoding="utf-8")
    (log_dir / "error.log").write_text("error-token\n", encoding="utf-8")
    (log_dir / "queue.log").write_text("queue-token\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    for query, filename in [("early-token", "run1.log"), ("late-token", "run1.log"),
                            ("historical-token", "run2.log"), ("error-token", "error.log"),
                            ("queue-token", "queue.log")]:
        response = client.get("/api/tasks", params={"query": query, "include_logs": True, "compact": True})
        assert response.status_code == 200
        payload = response.json()
        assert payload["total"] == 1
        assert payload["search_errors"] == []
        match = payload["items"][0]["search_matches"][0]
        assert match["log_file"] == filename
        assert match["snippet"][match["match_start"]:match["match_end"]] == query
        context = client.get("/api/tasks/alpha/logs", params={
            "log_file_name": filename, "offset": match["offset"],
            "log_identity": match["log_identity"], "chunk_size": 32768,
        }).json()
        assert query in context["content"]
    page = runtime.search_tasks(query="alpha\nhistorical-token", cancelled=threading.Event())
    assert page.total == 1
    assert {match["field"] for match in page.items[0]["search_matches"]} == {"name", "log"}


@pytest.mark.parametrize("prefix,token,suffix,query", [
    ("x" * 16380, "cross-boundary-token", "\r\n", "cross-boundary-token"),
    ("中" * 16383, "😀测试", "\n", "😀测试"),
    ("x" * 16381, "to\x1b[31mken\x1b[0m", "\n", "token"),
    ("x" * 16380, "loss   :   42", "\rnext\n", "loss:42"),
    ("\t", "İstanbul", "\n", "i̇stanbul"),
], ids=["ascii", "unicode", "ansi", "colon-spaces", "unicode-lower"])
@pytest.mark.parametrize("match_case", [False, True])
def test_log_search_chunk_boundaries_unicode_ansi_and_normalization(tmp_path, prefix, token, suffix, query, match_case):
    from pyruns.utils.log_search import LogSearch
    from pyruns.utils.search_query import SearchQuery

    if match_case:
        query = query.replace("i\u0307", "\u0130")

    path = tmp_path / "run1.log"
    path.write_text(prefix + token + suffix, encoding="utf-8", newline="")
    result = LogSearch._search_file(str(path), path.name, path.stat().st_size, [query], threading.Event(), match_case=match_case)
    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["line"] == 1
    assert len(match["snippet"]) <= 180
    assert SearchQuery(query, match_case=match_case).normalize(match["snippet"][match["match_start"]:match["match_end"]]) == query
    with path.open("rb") as handle:
        handle.seek(match["offset"])
        context = handle.read(32768).decode("utf-8", errors="replace")
    assert token in context


@pytest.mark.parametrize("suffix,query", [
    ("B\n", "aς"), ("\u0301B\n", "aς"), ("", "aς"), ("B\n", "aσb"),
    ("\u0888B\n", "aς"), ("\u0888B\n", "aσ"),
])
@pytest.mark.parametrize("fill_previews", [False, True])
def test_log_search_keeps_contextual_unicode_case_across_chunks(tmp_path, suffix, query, fill_previews):
    from pyruns.utils.log_search import LogSearch, _CHUNK_CHARS
    from pyruns.utils.search_query import SearchQuery

    prefix = "aς\n" * 24 if fill_previews else ""
    payload = prefix + "x" * (_CHUNK_CHARS - len(prefix) - 2) + "AΣ" + suffix
    path = tmp_path / "run1.log"
    path.write_text(payload, encoding="utf-8")
    matcher = SearchQuery(query)
    expected = matcher.scan(payload)["match_count"]
    result = LogSearch._search_file(str(path), path.name, path.stat().st_size, matcher.needles, threading.Event())
    assert result["match_count"] == expected


@pytest.mark.parametrize("payload,query", [
    ("x" * 8191 + "ΑΣ " + "x" * 16000, "ς"),
    ("x" * 8191 + "A" + "\u0301" * 8191 + "Σ tail\n", "ς tail"),
    ("x" * 8190 + "A\u0888" + "\u0301" * 8191 + "Σ tail\n", "ς tail"),
])
def test_log_search_preserves_case_context_before_retained_overlap(tmp_path, payload, query):
    from pyruns.utils.log_search import LogSearch
    from pyruns.utils.search_query import SearchQuery

    path = tmp_path / "run1.log"
    path.write_text(payload, encoding="utf-8")
    matcher = SearchQuery(query)
    result = LogSearch._search_file(str(path), path.name, path.stat().st_size, matcher.needles, threading.Event())
    expected = matcher.scan(payload)["match_count"]
    assert result["match_count"] == expected
    assert len(result["matches"]) == expected
    token = "Σ" if query == "ς" else "Σ tail"
    for match in result["matches"]:
        assert match["snippet"][match["match_start"]:match["match_end"]] == token
        assert 0 <= payload.encode().index("Σ".encode()) - match["offset"] <= 256


@pytest.mark.parametrize("query", ["aς", "aσ"])
@pytest.mark.parametrize("pending,suffix", [
    ("", "\u0301" * 40000 + "B"),
    ("", "\x1b[31mB\x1b[0m"),
    ("\x1b", "[31mB\x1b[0m"),
    ("\x1b[", "31mB\x1b[0m"),
    ("\x1b]title", "\x07B"),
    ("\x1b]title", "\x1b\\B"),
    ("", "\x1b[0 0mB"),
])
def test_log_search_case_lookahead_preserves_ansi_and_reader_position(tmp_path, query, pending, suffix):
    from pyruns.utils.log_search import LogSearch, _ANSI, _CHUNK_CHARS
    from pyruns.utils.search_query import SearchQuery

    payload = "x" * (_CHUNK_CHARS - 2 - len(pending)) + "AΣ" + pending + suffix + "\nmarker\n"
    path = tmp_path / "run1.log"
    path.write_text(payload, encoding="utf-8")
    matcher = SearchQuery(query + "\nmarker")
    expected = matcher.scan(_ANSI.sub("", payload))
    result = LogSearch._search_file(str(path), path.name, path.stat().st_size, matcher.needles, threading.Event())
    assert result["match_count"] == expected["match_count"]
    assert result["found"] == expected["found"]
    marker = next(match for match in result["matches"] if match["line"] == 2)
    assert marker["snippet"][marker["match_start"]:marker["match_end"]] == "marker"
    assert 0 <= payload.encode().index(b"marker") - marker["offset"] <= 256


def test_log_search_case_lookahead_cancels_and_restores_reader(tmp_path):
    from concurrent.futures import CancelledError
    from pyruns.utils.log_search import _CHUNK_CHARS, _next_character_is_cased

    path = tmp_path / "run1.log"
    path.write_text("AΣ" + "\u0301" * (_CHUNK_CHARS * 2) + "B", encoding="utf-8")
    cancelled = MagicMock()
    cancelled.is_set.side_effect = [False, True]
    with path.open(encoding="utf-8") as handle:
        handle.read(2)
        position = handle.tell()
        with pytest.raises(CancelledError):
            _next_character_is_cased(handle, path.stat().st_size, cancelled)
        assert handle.tell() == position
        assert handle.read() == "\u0301" * (_CHUNK_CHARS * 2) + "B"


@pytest.mark.parametrize("payload,expected", [("a" * 100_000, 100_000 // 3), ("aaa\n" * 100_000, 100_000)], ids=["long-line", "many-lines"])
def test_log_search_counts_long_lines_without_duplicate_overlap_and_cancels(tmp_path, payload, expected):
    from concurrent.futures import CancelledError
    from pyruns.utils.log_search import LogSearch

    path = tmp_path / "run1.log"
    path.write_text(payload, encoding="utf-8")
    result = LogSearch._search_file(str(path), path.name, path.stat().st_size, ["aaa"], threading.Event())
    assert result["match_count"] == expected
    assert len(result["matches"]) == 24
    cancelled = threading.Event()
    cancelled.set()
    with pytest.raises(CancelledError):
        LogSearch._search_file(str(path), path.name, path.stat().st_size, ["absent"], cancelled)


@pytest.mark.parametrize("encoding", ["gbk", "invalid-utf8"])
@pytest.mark.parametrize("advanced", [False, True])
def test_log_search_preserves_byte_positions_for_locale_and_invalid_bytes(tmp_path, monkeypatch, encoding, advanced):
    from pyruns.utils import log_search

    path = tmp_path / "run1.log"
    if encoding == "gbk":
        monkeypatch.setattr(log_search, "_log_decode_candidates", lambda: ["utf-8", "gbk"])
        token = "测试"
        payload = b"ASCII header " * 10000 + token.encode("gbk")
    else:
        monkeypatch.setattr(log_search, "_log_decode_candidates", lambda: ["utf-8"])
        token = "token"
        payload = b"\xff" * 2000 + token.encode()
    path.write_bytes(payload)
    if advanced:
        from pyruns.utils.search_query import SearchQuery
        result = log_search.LogSearch._search_file_patterns(str(path), path.name, len(payload), SearchQuery(token, use_regex=True), threading.Event())
    else:
        result = log_search.LogSearch._search_file(str(path), path.name, len(payload), [token], threading.Event())
    assert result["match_count"] == 1
    match = result["matches"][0]
    assert match["snippet"][match["match_start"]:match["match_end"]] == token
    assert 0 <= payload.index(token.encode("gbk" if encoding == "gbk" else "utf-8")) - match["offset"] <= 256


def test_log_search_cache_invalidates_for_append_rewrite_and_read_error(tmp_path, monkeypatch):
    from pyruns._config import RUN_LOGS_DIR
    from pyruns.utils.log_search import LogSearch

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", log_text="token\n")
    task_dir = workspace / TASKS_DIR / "alpha"
    path = task_dir / RUN_LOGS_DIR / "run1.log"
    search = LogSearch()
    event = threading.Event()
    with patch.object(search, "_search_file", wraps=search._search_file) as scan:
        assert search.search(str(task_dir), "token", event)["match_count"] == 1
        assert search.search(str(task_dir), "token", event)["match_count"] == 1
        assert scan.call_count == 1
        with path.open("a", encoding="utf-8") as handle:
            handle.write("token\n")
        assert search.search(str(task_dir), "token", event)["match_count"] == 2
        path.write_text("gone\n", encoding="utf-8")
        assert search.search(str(task_dir), "token", event)["match_count"] == 0
        assert scan.call_count == 3
    monkeypatch.setattr(search, "_search_file", MagicMock(side_effect=PermissionError("denied")))
    assert search.search(str(task_dir), "uncached", event)["errors"] == ["Could not read run1.log"]


def test_log_search_cache_reuses_files_beyond_its_capacity(tmp_path, monkeypatch):
    from pyruns._config import RUN_LOGS_DIR
    from pyruns.utils import log_search

    monkeypatch.setattr(log_search, "_CACHE_FILES_PER_QUERY", 4)
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", log_text="needle\n")
    task_dir = workspace / TASKS_DIR / "alpha"
    for run_index in range(2, 6):
        (task_dir / RUN_LOGS_DIR / f"run{run_index}.log").write_text("needle\n", encoding="utf-8")

    search = log_search.LogSearch()
    event = threading.Event()
    with patch.object(search, "_search_file", wraps=search._search_file) as scan:
        assert search.search(str(task_dir), "needle", event)["match_count"] == 5
        assert scan.call_count == 5
        assert search.search(str(task_dir), "needle", event)["match_count"] == 5
        assert scan.call_count == 6
        search.search(str(task_dir), "other", event)
        assert search.search(str(task_dir), "needle", event)["match_count"] == 5
        assert scan.call_count == 12


def test_log_search_caches_negative_results_beyond_preview_capacity(tmp_path, monkeypatch):
    from pyruns._config import RUN_LOGS_DIR
    from pyruns.utils import log_search

    monkeypatch.setattr(log_search, "_CACHE_FILES_PER_QUERY", 4)
    monkeypatch.setattr(log_search, "_CACHE_MISSES_PER_QUERY", 8, raising=False)
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", log_text="other\n")
    task_dir = workspace / TASKS_DIR / "alpha"
    for run_index in range(2, 6):
        (task_dir / RUN_LOGS_DIR / f"run{run_index}.log").write_text("other\n", encoding="utf-8")

    search = log_search.LogSearch()
    event = threading.Event()
    with patch.object(search, "_search_file", wraps=search._search_file) as scan:
        assert search.search(str(task_dir), "needle", event)["match_count"] == 0
        assert scan.call_count == 5
        assert search.search(str(task_dir), "needle", event)["match_count"] == 0
        assert scan.call_count == 5

        (task_dir / RUN_LOGS_DIR / "run5.log").write_text("needle\n", encoding="utf-8")
        assert search.search(str(task_dir), "needle", event)["match_count"] == 1
        assert scan.call_count == 6
        assert search.search(str(task_dir), "needle", event)["match_count"] == 1
        assert scan.call_count == 6


def test_log_search_releases_runtime_lock_and_blank_query_skips_disk_scan(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", log_text="token\n")
    runtime = _build_runtime(workspace)
    entered = threading.Event()
    release = threading.Event()
    scan = runtime._log_search.search

    def slow_scan(*args):
        entered.set()
        assert release.wait(5)
        return scan(*args)

    with patch.object(runtime._log_search, "search", side_effect=slow_scan) as mocked:
        client = TestClient(create_app(runtime))
        assert client.get("/api/tasks", params={"include_logs": True}).status_code == 200
        assert not mocked.called
        with ThreadPoolExecutor(1) as pool:
            pending = pool.submit(runtime.search_tasks, query="token", cancelled=threading.Event())
            try:
                assert entered.wait(5)
                assert runtime._workspace_lock.acquire(timeout=1)
                runtime._workspace_lock.release()
                assert runtime.get_task("alpha", refresh=False)["name"] == "alpha"
            finally:
                release.set()
            assert pending.result(timeout=5).total == 1


def test_disconnect_cancels_full_log_scan(tmp_path):
    import asyncio

    runtime = _build_runtime(_make_workspace(tmp_path, "main"))
    stopped = threading.Event()

    def scan(*, cancelled, **kwargs):
        assert cancelled.wait(5)
        stopped.set()

    class Disconnected:
        async def is_disconnected(self):
            return True

    with patch.object(runtime, "search_tasks", side_effect=scan):
        app = create_app(runtime)
        endpoint = next(route.endpoint for route in app.routes if route.path == "/api/tasks" and "GET" in route.methods)
        response = asyncio.run(endpoint(request=Disconnected(), query="needle", include_logs=True))
        assert response.status_code == 499
        assert stopped.wait(1)


def test_tasks_endpoint_exposes_persisted_structured_gpu_wait_state(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "gpu-wait", status="queued")
    task_dir = workspace / TASKS_DIR / "gpu-wait"
    started_at = time.time() - 5
    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "queued_at": started_at,
                "gpu_wait": {
                    "state": "waiting",
                    "run_index": 1,
                    "started_at": started_at,
                    "deadline_at": started_at + 60,
                    "max_wait_seconds": 60,
                    "requested_gpu_count": 1,
                    "eligible_gpu_count": 0,
                    "total_gpu_count": 1,
                    "reason": "GPU 0 free 23.0 GiB < 40 GiB",
                    "devices": [
                        {
                            "index": 0,
                            "uuid": "GPU-0",
                            "eligible": False,
                            "reason": "GPU 0 free 23.0 GiB < 40 GiB",
                        }
                    ],
                    "updated_at": started_at,
                },
            }
        ),
    )
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks", params={"summary": True, "limit": 1})

    assert response.status_code == 200
    wait = response.json()["items"][0]["gpu_wait"]
    assert wait["waited_seconds"] >= 4
    assert wait["remaining_seconds"] > 0
    assert wait["requested_gpu_count"] == 1
    assert wait["eligible_gpu_count"] == 0
    assert wait["devices"][0]["reason"]


def test_launcher_endpoints_discover_scripts_configs_and_workspaces(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    secondary = tmp_path / "secondary.py"
    secondary.write_text("print('secondary')\n", encoding="utf-8")
    config_path = tmp_path / "secondary.yaml"
    config_path.write_text("epochs: 2\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    monkeypatch.chdir(tmp_path)

    scripts = client.get("/api/launcher/scripts")
    assert scripts.status_code == 200
    script_items = scripts.json()["items"]
    assert any(item["script_name"] == "main" for item in script_items)
    assert any(item["script_name"] == "secondary" for item in script_items)

    configs = client.get("/api/launcher/configs", params={"script": str(secondary)})
    assert configs.status_code == 200
    assert any(item["label"] == "secondary.yaml" for item in configs.json()["items"])

    workspaces = client.get(
        "/api/launcher/workspaces",
        params={"script": str(secondary), "config": str(config_path)},
    )
    assert workspaces.status_code == 200
    workspace_items = workspaces.json()["items"]
    assert workspace_items[0]["script_name"] == "secondary"
    assert workspace_items[0]["config_name"] == "secondary.yaml"


def test_launcher_configs_reports_when_load_script_needs_first_yaml(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "load_train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")

    response = client.get("/api/launcher/configs", params={"script": str(script_path)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["requires_config_template"] is True
    assert payload["config_source"] == "pyruns_load"
    assert not (tmp_path / "_pyruns_" / "load_train").exists()


def test_launcher_open_load_script_with_yaml_import_clears_first_launch_requirement(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "load_train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")
    config_path = tmp_path / "configs" / "base.yaml"
    config_path.parent.mkdir()
    config_path.write_text("lr: 0.01\nepochs: 1\n", encoding="utf-8")

    before = client.get("/api/launcher/configs", params={"script": str(script_path)}).json()
    assert before["requires_config_template"] is True

    response = client.post(
        "/api/launcher/open",
        json={"script_path": str(script_path), "config_path": str(config_path)},
    )

    assert response.status_code == 200
    payload = response.json()
    workspace_root = Path(payload["run_root"])
    assert payload["script_name"] == "load_train"
    assert (workspace_root / "config_default.yaml").read_text(encoding="utf-8") == "lr: 0.01\nepochs: 1\n"
    after = client.get("/api/launcher/configs", params={"script": str(script_path)}).json()
    assert after["requires_config_template"] is False
    assert after["items"][0]["kind"] == "workspace_default"


def test_launcher_open_load_script_replaces_workspace_default_with_selected_yaml(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "load_train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")
    old_config = tmp_path / "configs" / "old.yaml"
    new_config = tmp_path / "configs" / "base.yaml"
    old_config.parent.mkdir()
    old_config.write_text("experiment:\n  name: stale\ntraining:\n  lr: 0.01\n", encoding="utf-8")
    new_config.write_text("experiment:\n  name: nested-smoke\ntraining:\n  lr: 0.001\n", encoding="utf-8")

    first = client.post(
        "/api/launcher/open",
        json={"script_path": str(script_path), "config_path": str(old_config)},
    )
    second = client.post(
        "/api/launcher/open",
        json={"script_path": str(script_path), "config_path": str(new_config)},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    payload = second.json()
    workspace_root = Path(payload["run_root"])
    assert (workspace_root / "config_default.yaml").read_text(encoding="utf-8") == new_config.read_text(
        encoding="utf-8"
    )
    assert payload["config_default_source"] == str(new_config).replace("\\", "/")
    assert payload["config_default_source_name"] == "base.yaml"
    assert any(
        item["label"] == "config_default.yaml (from base.yaml)"
        for item in payload["templates"]
    )


def test_launcher_open_argparse_script_generates_default_config_without_yaml(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "train.py"
    script_path.write_text(
        "\n".join(
            [
                "import argparse",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--epochs', type=int, default=3)",
                "args = parser.parse_args()",
                "",
            ]
        ),
        encoding="utf-8",
    )

    info = client.get("/api/launcher/configs", params={"script": str(script_path)}).json()
    assert info["config_source"] == "argparse"
    assert info["requires_config_template"] is False

    response = client.post("/api/launcher/open", json={"script_path": str(script_path)})

    assert response.status_code == 200
    payload = response.json()
    workspace_root = Path(payload["run_root"])
    assert payload["script_name"] == "train"
    assert (workspace_root / "tasks").is_dir()
    default_text = (workspace_root / "config_default.yaml").read_text(encoding="utf-8")
    assert "epochs: 3" in default_text


def test_launcher_configs_endpoint_rejects_invalid_script_path(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/launcher/configs", params={"script": str(tmp_path / "missing.py")})

    assert response.status_code == 400
    assert "Python script" in response.json()["detail"]


def test_launcher_open_endpoint_activates_selected_workspace(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "alt.py"
    script_path.write_text("print('alt')\n", encoding="utf-8")
    config_path = tmp_path / "alt.yaml"
    config_path.write_text("lr: 0.02\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)

    response = client.post(
        "/api/launcher/open",
        json={"script_path": str(script_path), "config_path": str(config_path)},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["script_name"] == "alt"
    assert payload["run_root"].endswith("_pyruns_/alt")
    assert client.get("/api/workspace").json()["script_name"] == "alt"


def test_launcher_pick_config_path_returns_native_yaml_selection(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")
    config_path = tmp_path / "configs" / "base.yaml"
    config_path.parent.mkdir()
    config_path.write_text("lr: 0.01\n", encoding="utf-8")

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=True),
        patch("pyruns.web.runtime.choose_config_file", return_value=str(config_path)) as choose_config_mock,
    ):
        response = client.post(
            "/api/launcher/pick-config-path",
            json={"script_path": str(script_path)},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["path"] == str(config_path).replace("\\", "/")
    assert payload["label"] == "base.yaml"
    assert payload["kind"] == "manual"
    choose_config_mock.assert_called_once()


def test_launcher_pick_config_path_reports_unavailable_native_picker(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=False),
        patch("pyruns.web.runtime.choose_config_file") as choose_config_mock,
    ):
        response = client.post(
            "/api/launcher/pick-config-path",
            json={"script_path": str(script_path)},
        )

    assert response.status_code == 400
    assert "Enter the path manually" in response.json()["detail"]
    choose_config_mock.assert_not_called()


def test_launcher_pick_config_path_reports_cancelled_yaml_selection(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=True),
        patch("pyruns.web.runtime.choose_config_file", return_value=None),
    ):
        response = client.post(
            "/api/launcher/pick-config-path",
            json={"script_path": str(script_path)},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "No YAML config selected."


@pytest.mark.parametrize("target_kind", ["file", "directory"])
def test_launcher_open_endpoint_rejects_non_python_target(tmp_path, target_kind):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    target = tmp_path / ("notes.txt" if target_kind == "file" else "configs")
    if target_kind == "file":
        target.write_text("not a script\n", encoding="utf-8")
    else:
        target.mkdir()

    response = client.post("/api/launcher/open", json={"script_path": str(target)})

    assert response.status_code == 400
    assert "Python script" in response.json()["detail"]


def test_pick_script_endpoint_reports_missing_load_yaml_as_bad_request(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime), raise_server_exceptions=False)
    script_path = tmp_path / "load_train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=True),
        patch("pyruns.web.runtime.choose_script_file", return_value=str(script_path)),
    ):
        response = client.post("/api/launcher/pick-script")

    assert response.status_code == 400
    assert "needs a YAML template" in response.json()["detail"]


def test_pick_script_path_endpoint_selects_script_without_bootstrapping(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "load_train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=True),
        patch("pyruns.web.runtime.choose_script_file", return_value=str(script_path)),
    ):
        response = client.post("/api/launcher/pick-script-path")

    assert response.status_code == 200
    payload = response.json()
    assert payload["script_name"] == "load_train"
    assert payload["script_path"] == str(script_path).replace("\\", "/")
    assert not (tmp_path / "_pyruns_" / "load_train" / "config_default.yaml").exists()


def test_run_root_switch_endpoint_reloads_workspace(tmp_path):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    _add_task(workspace_b, "task-b")
    runtime = _build_runtime(workspace_a)
    client = TestClient(create_app(runtime))

    response = client.post("/api/workspace/run-root", json={"path": str(workspace_b)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["script_name"] == "alt"
    tasks = client.get("/api/tasks").json()
    assert tasks["items"][0]["name"] == "task-b"


def _assert_workspace_switch_waits(
    runtime,
    workspace: Path,
    operation,
    entered: threading.Event,
    release: threading.Event,
    message: str,
) -> None:
    switched = threading.Event()
    errors: list[BaseException] = []

    def capture(callback, finished=None):
        try:
            callback()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            if finished is not None:
                finished.set()

    operation_thread = threading.Thread(target=capture, args=(operation,))
    switch_thread = threading.Thread(
        target=capture,
        args=(lambda: runtime.change_run_root(str(workspace)), switched),
    )
    try:
        operation_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert not switched.wait(0.5), message
    finally:
        release.set()
        operation_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not operation_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []


def test_workspace_switch_waits_for_in_flight_task_start(tmp_path, monkeypatch):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    _add_task(workspace_a, "same-name")
    _add_task(workspace_b, "same-name")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    started_in: list[str] = []
    original_require_task = runtime.require_task

    def blocked_require_task(task_name, *, refresh=True):
        task = original_require_task(task_name, refresh=refresh)
        entered.set()
        if not release.wait(5):
            raise TimeoutError("task start test did not release")
        return task

    def record_start(manager, task_name):
        started_in.append(str(Path(manager.tasks_dir).parent))
        return True

    monkeypatch.setattr(runtime, "require_task", blocked_require_task)
    monkeypatch.setattr(TaskManager, "start_task_now", record_start)
    _assert_workspace_switch_waits(
        runtime,
        workspace_b,
        lambda: runtime.start_task("same-name"),
        entered,
        release,
        "workspace changed while task start was in flight",
    )
    assert started_in == [str(workspace_a)]


def test_workspace_switch_waits_for_in_flight_runtime_update(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_module

    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    saved_batches: list[tuple[str, dict[str, object]]] = []

    def blocked_save(root, values):
        saved_batches.append((str(Path(root)), dict(values)))
        entered.set()
        if not release.wait(5):
            raise TimeoutError("runtime update test did not release")

    monkeypatch.setattr(runtime_module, "save_settings_for_root", blocked_save)
    monkeypatch.setattr(runtime_module, "load_settings", lambda root: {})
    monkeypatch.setattr(runtime, "get_runtime_info", lambda refresh_providers=False: {"ok": True})
    _assert_workspace_switch_waits(
        runtime,
        workspace_b,
        lambda: runtime.update_runtime_settings({
            "python_executable": "python-a",
            "conda_env": "env-a",
        }),
        entered,
        release,
        "workspace changed while runtime settings were being saved",
    )
    assert saved_batches == [(
        str(workspace_a),
        {"python_executable": "python-a", "conda_env": "env-a"},
    )]


def test_workspace_switch_waits_for_in_flight_task_creation(tmp_path, monkeypatch):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    generator = runtime.task_generator
    entered = threading.Event()
    release = threading.Event()
    added_in: list[str] = []

    def blocked_create(configs, name_prefix, *, task_kind=TASK_KIND_CONFIG):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("task creation test did not release")
        return [{
            "name": name_prefix,
            "dir": str(workspace_a / TASKS_DIR / name_prefix),
            "status": "pending",
            "config": configs[0],
            "task_kind": task_kind,
        }]

    def record_add(manager, tasks):
        added_in.append(str(Path(manager.tasks_dir).parent))

    monkeypatch.setattr(generator, "create_tasks", blocked_create)
    monkeypatch.setattr(TaskManager, "add_tasks", record_add)
    _assert_workspace_switch_waits(
        runtime,
        workspace_b,
        lambda: runtime.create_tasks_from_template(
            name_prefix="created",
            mode="yaml",
            yaml_text="lr: 0.1\n",
            append_timestamp=False,
        ),
        entered,
        release,
        "workspace changed while tasks were being created",
    )
    assert added_in == [str(workspace_a)]


def test_workspace_switch_waits_for_in_flight_workspace_read(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_module

    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    responses: list[dict] = []
    original_load_script_info = runtime_module.load_script_info
    call_count = 0

    def blocked_load_script_info(root):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("workspace read test did not release")
        return original_load_script_info(root)

    monkeypatch.setattr(runtime_module, "load_script_info", blocked_load_script_info)
    _assert_workspace_switch_waits(
        runtime,
        workspace_b,
        lambda: responses.append(runtime.get_workspace_info()),
        entered,
        release,
        "workspace changed while metadata was being assembled",
    )
    assert Path(responses[0]["run_root"]) == workspace_a
    assert responses[0]["script_name"] == "main"


def test_workspace_switch_waits_for_in_flight_log_read(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_module

    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    _add_task(workspace_a, "same-name", status="completed", log_text="workspace-a\n")
    _add_task(workspace_b, "same-name", status="completed", log_text="workspace-b\n")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    responses: list[dict] = []
    original_get_log_options = runtime_module.get_log_options

    def blocked_get_log_options(task_dir):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("log read test did not release")
        return original_get_log_options(task_dir)

    monkeypatch.setattr(runtime_module, "get_log_options", blocked_get_log_options)
    _assert_workspace_switch_waits(
        runtime,
        workspace_b,
        lambda: responses.append(runtime.get_task_logs("same-name", tail_lines=20)),
        entered,
        release,
        "workspace changed while a log response was being assembled",
    )
    assert responses[0]["content"].splitlines() == ["workspace-a"]


def test_runtime_provider_refresh_uses_snapshot_without_blocking_workspace_switch(tmp_path, monkeypatch):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    runtime.settings.update({
        "python_executable": "python-a",
        "conda_executable": "conda-a",
    })
    entered = threading.Event()
    release = threading.Event()
    switched = threading.Event()
    responses: list[dict] = []
    errors: list[BaseException] = []

    def blocked_provider(settings, *, workspace_epoch, refresh, cached):
        assert refresh is True
        entered.set()
        if not release.wait(5):
            raise TimeoutError("provider snapshot test did not release")
        return {
            "available": False,
            "executable": settings.get("conda_executable", "conda"),
            "envs": [],
            "error": "not installed",
        }

    def read_runtime():
        try:
            responses.append(runtime.get_runtime_info(refresh_providers=True))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            switched.set()

    monkeypatch.setattr(runtime, "_list_conda_envs_for_snapshot", blocked_provider)
    read_thread = threading.Thread(target=read_runtime)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        read_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert switched.wait(2), "provider discovery held the workspace lock"
    finally:
        release.set()
        read_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not read_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []
    assert responses[0]["python_executable"] == "python-a"
    assert responses[0]["conda"]["executable"] == "conda-a"


def test_generator_picker_does_not_block_switch_and_rejects_stale_workspace(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_module

    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    shell_file = workspace_a / "seed.sh"
    shell_file.write_text("echo from-a\n", encoding="utf-8")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    switched = threading.Event()
    errors: list[BaseException] = []

    def blocked_picker(initial_dir):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("picker test did not release")
        return str(shell_file)

    def pick_file():
        try:
            runtime.pick_generator_shell_file()
        except BaseException as exc:
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        finally:
            switched.set()

    monkeypatch.setattr(runtime_module, "native_picker_available", lambda: True)
    monkeypatch.setattr(runtime_module, "choose_shell_file", blocked_picker)
    picker_thread = threading.Thread(target=pick_file)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        picker_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert switched.wait(2), "native picker held the workspace lock"
    finally:
        release.set()
        picker_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not picker_thread.is_alive()
    assert not switch_thread.is_alive()
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "Workspace changed" in str(errors[0])


def test_runtime_reload_reclaims_idle_workspace_managers(tmp_path):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    managers = []

    class DummyTaskManager:
        def __init__(self, tasks_dir: str):
            self.tasks_dir = tasks_dir
            self.shutdown_count = 0

        def shutdown(self) -> None:
            self.shutdown_count += 1

    def make_task_manager(tasks_dir: str):
        manager = DummyTaskManager(tasks_dir)
        managers.append(manager)
        return manager

    runtime = PyrunsRuntime(root_dir=str(workspace_a), task_manager_factory=make_task_manager)
    first_manager = runtime.task_manager

    runtime.reload(str(workspace_b))

    assert first_manager.shutdown_count == 1
    assert managers == [first_manager]
    second_manager = runtime.task_manager
    runtime.reload(str(workspace_a))

    assert second_manager.shutdown_count == 1
    replacement_manager = runtime.task_manager
    assert replacement_manager is not first_manager
    runtime.shutdown()
    assert first_manager.shutdown_count == 1
    assert second_manager.shutdown_count == 1
    assert replacement_manager.shutdown_count == 1


def test_runtime_reclaims_a_background_manager_after_its_active_task_finishes(tmp_path):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")

    class DummyTaskManager:
        def __init__(self, tasks_dir: str):
            self.tasks_dir = tasks_dir
            self.tasks = [{"name": "active", "status": "running"}]
            self.is_processing = True
            self.shutdown_count = 0
            self.callbacks = []

        def on_change(self, callback) -> None:
            self.callbacks.append(callback)

        def off_change(self, callback) -> None:
            self.callbacks.remove(callback)

        def shutdown(self) -> None:
            self.shutdown_count += 1

        def finish(self) -> None:
            self.tasks[0]["status"] = "completed"
            self.is_processing = False
            for callback in list(self.callbacks):
                callback()

    runtime = PyrunsRuntime(
        root_dir=str(workspace_a),
        task_manager_factory=DummyTaskManager,
    )
    background_manager = runtime.task_manager

    runtime.reload(str(workspace_b))
    assert background_manager.shutdown_count == 0

    background_manager.finish()

    assert background_manager.shutdown_count == 1
    assert background_manager.callbacks == []
    runtime.shutdown()


def test_workspace_switch_does_not_terminate_running_task(tmp_path):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    (tmp_path / "main.py").write_text(
        "import time\nimport pyruns\npyruns.load()\nprint('started', flush=True)\ntime.sleep(30)\n",
        encoding="utf-8",
    )
    _add_task(workspace_a, "slow")
    runtime = _build_runtime(workspace_a)

    try:
        runtime.start_task("slow")
        task_dir = workspace_a / TASKS_DIR / "slow"
        deadline = time.monotonic() + 10
        pid = None
        while time.monotonic() < deadline:
            info = load_task_info(str(task_dir))
            pids = list(info.get("pids", []) or [])
            if info.get("status") == "running" and pids and pids[-1]:
                pid = int(pids[-1])
                break
            time.sleep(0.05)
        assert pid is not None

        runtime.change_run_root(str(workspace_b))

        assert psutil.pid_exists(pid)
        assert load_task_info(str(task_dir))["status"] == "running"
    finally:
        runtime.shutdown()


def test_web_main_shutdowns_runtime_after_uvicorn_returns(monkeypatch):
    from pyruns.web import app as web_app

    events = []

    class DummyRuntime:
        settings = {"ui_port": 8099}

        def shutdown(self) -> None:
            events.append("shutdown")

    monkeypatch.setattr(web_app, "PyrunsRuntime", DummyRuntime)
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )
    monkeypatch.setattr(web_app.uvicorn, "run", lambda *args, **kwargs: events.append("run"))

    web_app.main(open_browser=False, port=8123)

    assert events == ["run", "shutdown"]


def test_web_main_replaces_idle_server_with_updater_after_shutdown(monkeypatch, tmp_path):
    from pyruns.web import app as web_app

    events = []
    captured = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}
        root_dir = str(tmp_path / "initial-workspace")

        def active_task_count(self) -> int:
            return 0

        def shutdown(self) -> None:
            events.append("runtime-shutdown")

    def fake_run(app_target, **_kwargs):
        events.append("server-run")
        captured["session_state"] = app_target.state.session_recovery.path
        captured["session_scope"] = app_target.state.session_scope
        client = TestClient(app_target, base_url="http://127.0.0.1")
        assert client.get("/?token=private-token", follow_redirects=False).status_code == 303
        response = client.post(
            "/api/system/update",
            json={"target_version": "9999.0.0"},
        )
        assert response.status_code == 202
        app_target.state.runtime.root_dir = str(tmp_path / "switched-workspace")

    def fake_replace(**kwargs):
        captured["handoff_state"] = web_app.os.environ.get(web_app._UI_SESSION_STATE_ENV)
        captured["handoff_scope"] = web_app.os.environ.get(web_app._UI_SESSION_SCOPE_ENV)
        events.append(("replace", kwargs))

    monkeypatch.setattr(web_app, "PyrunsRuntime", DummyRuntime)
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )
    monkeypatch.setattr(web_app, "_request_server_shutdown", lambda: events.append("server-stop"))
    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)
    monkeypatch.setattr(web_app, "replace_process_with_updater", fake_replace)
    monkeypatch.delenv(web_app._UI_SESSION_STATE_ENV, raising=False)
    monkeypatch.delenv(web_app._UI_SESSION_SCOPE_ENV, raising=False)

    web_app.main(open_browser=False, port=8123, access_token="private-token")

    assert events[:3] == ["server-run", "server-stop", "runtime-shutdown"]
    assert events[3][0] == "replace"
    replacement = events[3][1]
    assert replacement["port"] == 8123
    assert replacement["token"] == "private-token"
    assert replacement["previous_version"] == __version__
    assert replacement["request_id"]
    assert replacement["instance_id"]
    assert replacement["state_dir"]
    assert replacement["restart_only"] is False
    assert replacement["target_version"] == "9999.0.0"
    assert replacement["installed_version"] == ""
    assert captured["handoff_state"] == captured["session_state"]
    assert captured["handoff_scope"] == captured["session_scope"]
    assert web_app._UI_SESSION_STATE_ENV not in web_app.os.environ
    assert web_app._UI_SESSION_SCOPE_ENV not in web_app.os.environ


def test_web_main_restarts_idle_server_after_external_package_change(monkeypatch):
    from pyruns.web import app as web_app

    events = []

    class DummyRuntime:
        settings = {"ui_port": 8099}

        def active_task_count(self) -> int:
            return 0

        def shutdown(self) -> None:
            events.append("runtime-shutdown")

    def fake_run(app_target, **_kwargs):
        events.append("server-run")
        client = TestClient(app_target, base_url="http://127.0.0.1")
        assert client.get("/?token=private-token", follow_redirects=False).status_code == 303
        info = client.get("/api/system/info")
        assert info.json()["restart_required"] is True
        response = client.post("/api/system/restart")
        assert response.status_code == 202

    def fake_restart(**kwargs):
        events.append(("restart", kwargs))

    monkeypatch.setattr(web_app, "PyrunsRuntime", DummyRuntime)
    monkeypatch.setattr(
        web_app,
        "find_available_port",
        lambda port, host="127.0.0.1", max_attempts=100: port,
    )
    monkeypatch.setattr(
        web_app.UiUpdateCoordinator,
        "_installed_version",
        staticmethod(lambda _fallback: "0.4.0"),
    )
    monkeypatch.setattr(web_app, "_request_server_shutdown", lambda: events.append("server-stop"))
    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)
    monkeypatch.setattr(web_app, "restart_ui_after_handoff", fake_restart)

    web_app.main(open_browser=False, port=8123, access_token="private-token")

    assert events[:3] == ["server-run", "server-stop", "runtime-shutdown"]
    assert events[3][0] == "restart"
    restart = events[3][1]
    assert restart["owner"] is True
    assert restart["installed_version"] == "0.4.0"
    assert restart["request_id"]
    assert restart["state_dir"]


def test_live_web_server_gracefully_hands_idle_update_to_replacer(tmp_path):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])

    token = "live-update-smoke-token"
    code = (
        "import json; "
        "from pyruns.web import app; "
        "app.replace_process_with_updater = "
        "lambda **kwargs: print('REPLACED=' + json.dumps(kwargs, sort_keys=True), flush=True); "
        f"app.main(open_browser=False, port={port}, access_token={token!r})"
    )
    environment = os.environ.copy()
    environment[ENV_KEY_ROOT] = str(tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client_socket:
                client_socket.settimeout(0.1)
                if client_socket.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        assert process.poll() is None

        opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        opener.open(f"http://127.0.0.1:{port}/?token={token}", timeout=5).read()
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/system/update",
            data=b"",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with opener.open(request, timeout=5) as response:
            assert response.status == 202

        stdout, stderr = process.communicate(timeout=15)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)

    assert process.returncode == 0, stdout + stderr
    replacement = next(
        json.loads(line.removeprefix("REPLACED="))
        for line in stdout.splitlines()
        if line.startswith("REPLACED=")
    )
    assert replacement["port"] == port
    assert replacement["previous_version"] == __version__
    assert replacement["token"] == token
    assert replacement["request_id"]
    assert replacement["instance_id"]
    assert replacement["state_dir"] == os.environ["PYRUNS_UPDATE_STATE_DIR"]
    assert replacement["restart_only"] is False
    assert isinstance(replacement["installed_version"], str)


def test_tasks_and_task_detail_endpoints_return_data(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="completed", log_text="epoch 1\n")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    tasks_response = client.get("/api/tasks", params={"limit": 10})
    detail_response = client.get("/api/tasks/alpha")

    assert tasks_response.status_code == 200
    assert tasks_response.json()["items"][0]["name"] == "alpha"
    assert detail_response.status_code == 200
    assert detail_response.json()["config"]["model"] == "tiny"


def test_run_and_cancel_task_endpoints_delegate_to_runtime(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    def fake_start(task_name: str) -> bool:
        task_dir = workspace / TASKS_DIR / task_name

        def apply(info):
            now = time.time()
            info.update(
                {
                    "status": "running",
                    "runner_id": "other-host:123:test-runner",
                    "runner_host": "other-host",
                    "lease_heartbeat": now,
                    "lease_until": now + 60,
                }
            )

        update_task_info(str(task_dir), apply)
        return True

    def fake_cancel(task_name: str, **_identity: object) -> bool:
        task_dir = workspace / TASKS_DIR / task_name

        def apply(info):
            info["status"] = "cancelled"

        update_task_info(str(task_dir), apply)
        return True

    with patch.object(runtime.task_manager, "start_task_now", side_effect=fake_start):
        run_response = client.post("/api/tasks/alpha/run", json={})
    with patch.object(runtime.task_manager, "request_task_cancel", side_effect=fake_cancel):
        cancel_response = client.post("/api/tasks/alpha/cancel")

    assert run_response.status_code == 200
    assert run_response.json()["task"]["status"] == "running"
    assert cancel_response.status_code == 200
    assert cancel_response.json()["task"]["status"] == "cancelled"


def test_cancel_task_endpoint_requests_foreign_runner_cancellation(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    runtime.ensure_tasks_loaded()
    task_dir = workspace / TASKS_DIR / "alpha"
    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "runner_id": "other-host:123:abcdef",
                "runner_host": "other-host",
                "lease_heartbeat": time.time(),
                "lease_until": time.time() + 60,
                "pids": [987654],
            }
        ),
    )
    runtime.task_manager.refresh_from_disk(task_ids=["alpha"], force_all=True)
    client = TestClient(create_app(runtime))

    response = client.post("/api/tasks/alpha/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "running"
    info = load_task_info(str(task_dir))
    assert info["cancel_requested_at"]
    assert info["runner_id"] == "other-host:123:abcdef"


def test_cancel_task_endpoint_reconciles_expired_foreign_runner(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    runtime.ensure_tasks_loaded()
    task_dir = workspace / TASKS_DIR / "alpha"
    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "runner_id": "other-host:123:expired",
                "runner_host": "other-host",
                "lease_heartbeat": time.time() - 120,
                "lease_until": time.time() - 60,
                "pids": [987654321],
            }
        ),
    )
    client = TestClient(create_app(runtime))

    with patch("pyruns.core.task_manager.kill_process") as kill_process:
        response = client.post("/api/tasks/alpha/cancel")

    assert response.status_code == 200, response.text
    assert response.json()["task"]["status"] == "failed"
    info = load_task_info(str(task_dir))
    assert info["status"] == "failed"
    assert info["cancel_requested_at"]
    assert "runner_id" not in info
    kill_process.assert_not_called()


def test_run_task_endpoint_rejects_unclaimed_start(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with patch.object(runtime.task_manager, "start_task_now", return_value=False):
        response = client.post("/api/tasks/alpha/run", json={})

    assert response.status_code == 400
    assert "could not be started" in response.json()["detail"]

def test_batch_run_rejects_removed_execution_mode_field(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    runtime.task_manager.max_workers = 1
    client = TestClient(create_app(runtime))

    response = client.post(
        "/api/tasks/batch/run",
        json={
            "task_names": ["alpha"],
            "execution_mode": "proces",
            "max_workers": 4,
        },
    )

    assert response.status_code == 422
    assert runtime.task_manager.max_workers == 1
    assert runtime.get_task("alpha", refresh=True)["status"] == "pending"
    info = json.loads((workspace / TASKS_DIR / "alpha" / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["status"] == "pending"


def test_run_task_rejects_removed_execution_mode_field(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.post("/api/tasks/alpha/run", json={"execution_mode": "proces"})

    assert response.status_code == 422
    assert runtime.get_task("alpha", refresh=True)["status"] == "pending"
    info = json.loads((workspace / TASKS_DIR / "alpha" / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["status"] == "pending"


def test_logs_endpoint_returns_history_and_available_logs(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running", log_text="line 1\nline 2\n")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_log"] == "run1.log"
    assert "run1.log" in payload["available_logs"]
    assert "line 1" in payload["content"]


def test_tasks_endpoint_discovers_external_task_dirs_on_refresh(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    assert client.get("/api/tasks", params={"limit": 10_000}).json()["total"] == 1

    _add_task(workspace, "beta")
    runtime.invalidate_cache()
    response = client.get("/api/tasks", params={"limit": 10_000, "refresh": True, "summary": True})

    assert response.status_code == 200
    names = {task["name"] for task in response.json()["items"]}
    assert names == {"alpha", "beta"}


def test_task_endpoint_lazy_loads_external_task_by_name(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    assert client.get("/api/tasks", params={"limit": 10_000}).json()["total"] == 0

    _add_task(workspace, "external")
    response = client.get("/api/tasks/external")

    assert response.status_code == 200
    assert response.json()["name"] == "external"


@pytest.mark.parametrize("status", ["pending", "queued"])
def test_task_endpoint_does_not_return_removed_cached_task(tmp_path, status):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "removed", status=status)
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    assert client.get("/api/tasks/removed").status_code == 200

    (workspace / TASKS_DIR / "removed" / TASK_INFO_FILENAME).unlink()
    assert client.get("/api/tasks/removed", params={"refresh": False}).status_code == 200
    assert client.get("/api/tasks/removed", params={"refresh": True}).status_code == 404
    assert client.get("/api/tasks", params={"force_refresh": True}).json()["total"] == 0
    assert runtime.task_manager.is_processing is False


def test_task_list_force_refresh_discards_missing_metadata(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "removed")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    assert client.get("/api/tasks", params={"summary": True}).json()["total"] == 1

    (workspace / TASKS_DIR / "removed" / TASK_INFO_FILENAME).unlink()
    assert client.get("/api/tasks", params={"refresh": False, "summary": True}).json()["total"] == 1
    assert client.get("/api/tasks", params={"force_refresh": True, "summary": True}).json()["total"] == 0


def test_task_endpoint_refresh_checks_only_selected_pending_payload(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    for name in ("alpha", "beta", "gamma"):
        _add_task(workspace, name)
    runtime = _build_runtime(workspace)
    runtime.ensure_tasks_loaded()
    client = TestClient(create_app(runtime))

    manager = runtime.task_manager
    with patch.object(manager, "_payload_signature", wraps=manager._payload_signature) as signature:
        response = client.get("/api/tasks/alpha")

    assert response.status_code == 200
    assert response.json()["name"] == "alpha"
    assert [Path(call.args[0]).name for call in signature.call_args_list] == ["alpha"]


def test_task_endpoint_refresh_skips_unrelated_active_metadata(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    _add_task(workspace, "beta", status="queued")
    _add_task(workspace, "gamma", status="running")
    runtime = _build_runtime(workspace)
    runtime.ensure_tasks_loaded()
    client = TestClient(create_app(runtime))

    original_stat = os.stat
    metadata_stats = []

    def track_stat(path, *args, **kwargs):
        if Path(path).name == TASK_INFO_FILENAME:
            metadata_stats.append(Path(path).parent.name)
        return original_stat(path, *args, **kwargs)

    with patch("pyruns.core.task_manager.os.stat", side_effect=track_stat):
        response = client.get("/api/tasks/alpha")

    assert response.status_code == 200
    assert metadata_stats and set(metadata_stats) == {"alpha"}


def test_logs_endpoint_prefers_active_run_log_even_before_file_exists(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running", log_text="old run\n")
    task_dir = workspace / TASKS_DIR / "alpha"

    def set_second_run(info):
        info["run_index"] = 2
        info["start_times"] = ["2026-03-17_12-00-00", "2026-03-17_12-10-00"]
        info["finish_times"] = ["2026-03-17_12-05-00", ""]
        info["pids"] = [111, __import__("os").getpid()]

    update_task_info(str(task_dir), set_second_run)
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs")

    assert response.status_code == 200
    payload = response.json()
    assert payload["selected_log"] == "run2.log"
    assert payload["content"] == ""
    assert "run2.log" in payload["available_logs"]
    assert "run1.log" in payload["available_logs"]


def test_logs_endpoint_can_tail_history_by_lines(tmp_path):
    workspace = _make_workspace(
        tmp_path,
        "main",
    )
    _add_task(
        workspace,
        "alpha",
        status="running",
        log_text="".join(f"line {index}\n" for index in range(1, 6)),
    )
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs", params={"tail_lines": 2})

    assert response.status_code == 200
    content = response.json()["content"]
    assert "line 4" in content
    assert "line 5" in content
    assert "line 3" not in content


def test_logs_endpoint_caps_large_initial_tail_by_server_byte_budget(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    log_text = "first\n" + ("x" * 5_000_001) + "\nlast\n"
    _add_task(workspace, "alpha", status="running", log_text=log_text)
    (workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log").write_bytes(log_text.encode("utf-8"))
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs", params={"tail_lines": 100})

    assert response.status_code == 200
    payload = response.json()
    content = payload["content"].replace("\r", "")
    assert payload["tail_truncated"] is True
    assert payload["tail_limit_bytes"] == 4 * 1024 * 1024
    assert len(payload["content"].encode("utf-8")) <= payload["tail_limit_bytes"]
    assert content.endswith("\nlast\n")
    assert not content.startswith("first\n")
    assert payload["offset"] == len(log_text)

    oversized = client.get(
        "/api/tasks/alpha/logs",
        params={"tail_lines": 100, "tail_bytes": 50_000_000},
    )
    assert oversized.status_code == 422
    assert any(
        item["loc"][-1] == "tail_bytes" and item["type"] == "less_than_equal"
        for item in oversized.json()["detail"]
    )


def test_logs_endpoint_only_caps_tail_lines_when_tail_bytes_is_explicit(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    log_text = ("A" * 40 + "\n") + ("B" * 40 + "\n")
    _add_task(workspace, "alpha", status="running", log_text=log_text)
    (workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log").write_bytes(log_text.encode("utf-8"))
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs", params={"tail_lines": 100, "tail_bytes": 16})

    assert response.status_code == 200
    payload = response.json()
    assert payload["content"].replace("\r", "") == "B" * 15 + "\n"
    assert payload["offset"] == len(log_text)
    assert payload["tail_truncated"] is True
    assert payload["tail_limit_bytes"] == 16


def test_logs_endpoint_tails_terminal_rows_without_counting_progress_carriage_returns(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    log_text = "prepare\nprogress 1%\rprogress 50%\rprogress 100%\nfinish\n"
    _add_task(workspace, "alpha", status="running", log_text=log_text)
    (workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log").write_bytes(log_text.encode("utf-8"))
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs", params={"tail_lines": 3})

    assert response.status_code == 200
    payload = response.json()
    assert payload["content"].replace("\r\n", "\n") == log_text
    assert payload["offset"] == len(log_text)


def test_logs_endpoint_caps_incremental_reads_by_chunk_size(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running", log_text="line 1\nline 2\n")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks/alpha/logs", params={"offset": 0, "chunk_size": 8})

    assert response.status_code == 200
    payload = response.json()
    assert payload["content"].replace("\r", "") == "line 1\n"
    assert payload["offset"] == len(payload["content"].encode("utf-8"))


def test_logs_endpoint_preserves_unicode_across_incremental_chunks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running", log_text="ab测试c")
    client = TestClient(create_app(_build_runtime(workspace)))

    offset = 0
    chunks = []
    while offset < len("ab测试c".encode("utf-8")):
        response = client.get("/api/tasks/alpha/logs", params={"offset": offset, "chunk_size": 3})
        assert response.status_code == 200
        payload = response.json()
        assert payload["offset"] > offset
        chunks.append(payload["content"])
        offset = payload["offset"]
    assert "".join(chunks) == "ab测试c"


def test_logs_endpoint_resets_incremental_reader_after_truncate_and_rotation(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running", log_text="old output that is deliberately long\n")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    initial = client.get(
        "/api/tasks/alpha/logs",
        params={"log_file_name": "run1.log", "tail_lines": 20},
    ).json()
    assert initial["log_identity"]

    log_file.write_text("new\n", encoding="utf-8")
    truncated = client.get(
        "/api/tasks/alpha/logs",
        params={
            "log_file_name": "run1.log",
            "offset": initial["offset"],
            "log_identity": initial["log_identity"],
            "chunk_size": 64 * 1024,
        },
    ).json()
    assert truncated["reset"] is True
    assert truncated["content"].replace("\r", "") == "new\n"
    assert truncated["offset"] == log_file.stat().st_size

    rotated_path = log_file.with_suffix(".previous")
    log_file.replace(rotated_path)
    replacement_text = "replacement file is longer than the previous offset\n"
    log_file.write_text(replacement_text, encoding="utf-8")
    rotated = client.get(
        "/api/tasks/alpha/logs",
        params={
            "log_file_name": "run1.log",
            "offset": truncated["offset"],
            "log_identity": truncated["log_identity"],
            "chunk_size": 64 * 1024,
        },
    ).json()
    assert rotated["reset"] is True
    assert rotated["log_identity"] != truncated["log_identity"]
    assert rotated["content"].replace("\r", "") == replacement_text
    assert rotated["offset"] == log_file.stat().st_size


def test_template_content_and_generator_create_endpoints(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    template_response = client.get("/api/templates/content", params={"value": "config_default.yaml"})

    assert template_response.status_code == 200
    assert "lr: 0.01" in template_response.json()["content"]

    create_response = client.post(
        "/api/generator/create",
        json={
            "name_prefix": "demo",
            "mode": "form",
            "yaml_text": "lr: 0.1 | 0.2\nmodel: tiny\n",
            "template_value": "config_default.yaml",
            "append_timestamp": False,
        },
    )

    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["count"] == 2
    assert {item["name"] for item in payload["items"]} == {"demo_1-of-2", "demo_2-of-2"}
    assert payload["recent_tasks"]
    assert payload["recent_tasks"][0]["config"] == {}
    assert payload["recent_tasks"][0]["records"] == []


def test_template_content_rejects_paths_outside_workspace_boundary(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    outside = tmp_path / "secret.yaml"
    outside.write_text("token: do-not-read\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with pytest.raises(ValueError, match="outside the allowed workspace"):
        runtime.get_template_content(str(outside))
    with pytest.raises(ValueError, match="outside the allowed workspace"):
        runtime.get_template_content("../../secret.yaml")
    response = client.get("/api/templates/content", params={"value": str(outside)})
    assert response.status_code == 400
    assert "outside the allowed workspace" in response.json()["detail"]
    preview = client.post(
        "/api/generator/preview",
        json={"mode": "form", "yaml_text": "lr: 1", "template_value": str(outside)},
    )
    assert preview.status_code == 400


def test_template_content_rejects_symlink_escape(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    outside = tmp_path / "secret.yaml"
    outside.write_text("token: do-not-read\n", encoding="utf-8")
    link = workspace / "linked.yaml"
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    runtime = _build_runtime(workspace)
    with pytest.raises(ValueError, match="outside the allowed workspace"):
        runtime.get_template_content("linked.yaml")


def test_shell_template_boundary_does_not_trust_project_root_metadata(tmp_path):
    workspace = _make_workspace(tmp_path, SHELL_WORKSPACE_NAME)
    outside_root = tmp_path.parent
    outside = outside_root / f"pyruns-outside-template-{tmp_path.name}.sh"
    outside.write_text("echo secret\n", encoding="utf-8")
    (workspace / SCRIPT_INFO_FILENAME).write_text(
        json.dumps({"workspace_kind": WORKSPACE_KIND_SHELL, "project_root": str(outside_root)}),
        encoding="utf-8",
    )
    runtime = _build_runtime(workspace)

    try:
        with pytest.raises(ValueError, match="outside the allowed workspace"):
            runtime.get_template_content(str(outside))
    finally:
        outside.unlink(missing_ok=True)


def test_template_and_generator_inputs_have_hard_size_limits(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_mod

    workspace = _make_workspace(tmp_path, "main")
    oversized = workspace / "oversized.yaml"
    oversized.write_text("value: 123456789\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    monkeypatch.setattr(runtime_mod, "MAX_TASK_PAYLOAD_BYTES", 8)

    with pytest.raises(ValueError, match="too large"):
        runtime.get_template_content("oversized.yaml")
    with pytest.raises(ValueError, match="too large"):
        runtime.preview_tasks_from_template(mode="yaml", yaml_text="value: 123456789")


def test_generator_preview_endpoint_returns_expansion_summary(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.post(
        "/api/generator/preview",
        json={
            "mode": "form",
            "yaml_text": "lr: 0.1 | 0.2\nmodel: tiny\n",
            "template_value": "config_default.yaml",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 2
    assert payload["items"][0]["preview"]


def test_yaml_mode_rejects_batch_syntax_without_expanding(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)

    def fail_if_expanded(_config):
        raise AssertionError("YAML mode should reject batch syntax without expansion")

    monkeypatch.setattr("pyruns.web.runtime.generate_batch_configs", fail_if_expanded)

    with pytest.raises(ValueError, match="YAML mode does not support batch syntax"):
        runtime.preview_tasks_from_template(
            mode="yaml",
            yaml_text="epochs: 0:1000000:1\nmodel: tiny\n",
            template_value="config_default.yaml",
        )


def test_generator_range_syntax_survives_yaml_parsing(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.post(
        "/api/generator/create",
        json={
            "name_prefix": "range-demo",
            "mode": "form",
            "yaml_text": "epochs: 30:40:1\nmodel: tiny\n",
            "template_value": "config_default.yaml",
            "append_timestamp": False,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 10
    assert payload["items"][0]["name"] == "range-demo_1-of-10"
    assert payload["items"][-1]["name"] == "range-demo_10-of-10"


def test_shell_workspace_endpoint_and_generator_shell_mode(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    shell_file = tmp_path / "run_smoke.sh"
    shell_file.write_text("echo smoke\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    shell_response = client.post("/api/workspace/shell")

    assert shell_response.status_code == 200
    shell_payload = shell_response.json()
    assert shell_payload["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert shell_payload["script_name"] == "_shell_"
    assert shell_payload["templates"] == []

    template_response = client.get("/api/templates/content", params={"value": str(shell_file)})
    assert template_response.status_code == 200
    template_payload = template_response.json()
    assert template_payload["mode_hint"] == "shell"
    assert template_payload["content"] == "echo smoke\n"

    with patch(
        "pyruns.core.task_generator.get_shell_config_filename_for_workspace",
        return_value=SHELL_CONFIG_FILENAME,
    ):
        create_response = client.post(
            "/api/generator/create",
            json={
                "name_prefix": "shell-demo",
                "mode": "shell",
                "shell_text": "echo hello from shell\n",
                "append_timestamp": False,
            },
        )

    assert create_response.status_code == 200
    payload = create_response.json()
    assert payload["count"] == 1
    assert payload["task_kind"] == TASK_KIND_SHELL
    task = payload["items"][0]
    assert task["task_kind"] == TASK_KIND_SHELL
    task_dir = Path(task["dir"])
    assert (task_dir / SHELL_CONFIG_FILENAME).read_text(encoding="utf-8") == "echo hello from shell\n"


def test_shell_workspace_templates_include_existing_shell_task_payloads_first(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    shell_file = tmp_path / "run_smoke.sh"
    shell_file.write_text("echo smoke\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    runtime.open_shell_workspace()
    task = runtime.task_generator.create_shell_task("shell_seed", "echo from task\n")
    client = TestClient(create_app(runtime))

    response = client.get("/api/templates")

    assert response.status_code == 200
    items = response.json()["items"]
    assert items[0]["label"] == task["name"]
    assert items[0]["value"] == f"tasks/{task['name']}/{task['config_file']}"
    assert {"value": str(shell_file).replace("\\", "/"), "label": "run_smoke.sh"} not in items

    content_response = client.get("/api/templates/content", params={"value": items[0]["value"]})
    assert content_response.status_code == 200
    payload = content_response.json()
    assert payload["mode_hint"] == "shell"
    assert payload["content"] == "echo from task\n"


def test_pick_generator_shell_file_returns_selected_script_content(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    shell_file = tmp_path / "scripts" / "launch.sh"
    shell_file.parent.mkdir()
    shell_file.write_text("bash train.sh\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    runtime.open_shell_workspace()
    client = TestClient(create_app(runtime))

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=True),
        patch("pyruns.web.runtime.choose_shell_file", return_value=str(shell_file)),
    ):
        response = client.post("/api/generator/pick-shell-file")

    assert response.status_code == 200
    payload = response.json()
    assert payload["value"] == str(shell_file).replace("\\", "/")
    assert payload["label"] == "scripts/launch.sh"
    assert payload["mode_hint"] == "shell"
    assert payload["content"] == "bash train.sh\n"


def test_pick_shell_root_endpoint_opens_directory_shell_workspace(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    target_dir = tmp_path / "shell_project"
    target_dir.mkdir(parents=True, exist_ok=True)

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=True),
        patch("pyruns.web.runtime.choose_directory", return_value=str(target_dir)),
    ):
        response = client.post("/api/launcher/pick-shell-root")

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert payload["run_root"].endswith("_pyruns_/_shell_")
    assert Path(payload["run_root"]).parent == target_dir / "_pyruns_"
    assert payload["project_root"] == str(target_dir).replace("\\", "/")
    assert payload["working_root"] == str(target_dir).replace("\\", "/")


def test_pick_shell_root_endpoint_rejects_unavailable_native_picker(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with (
        patch("pyruns.web.runtime.native_picker_available", return_value=False),
        patch("pyruns.web.runtime.choose_directory") as choose_directory_mock,
    ):
        response = client.post("/api/launcher/pick-shell-root")

    assert response.status_code == 400
    assert "Enter the path manually" in response.json()["detail"]
    choose_directory_mock.assert_not_called()


def test_open_shell_root_endpoint_accepts_manual_directory_path(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    target_dir = tmp_path / "manual_shell_project"
    target_dir.mkdir(parents=True, exist_ok=True)

    response = client.post("/api/launcher/open-shell-root", json={"path": str(target_dir)})

    assert response.status_code == 200
    payload = response.json()
    assert payload["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert payload["run_root"].endswith("_pyruns_/_shell_")
    assert Path(payload["run_root"]).parent == target_dir / "_pyruns_"
    assert payload["project_root"] == str(target_dir).replace("\\", "/")
    assert payload["working_root"] == str(target_dir).replace("\\", "/")


def test_open_shell_root_endpoint_rejects_missing_manual_directory(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.post("/api/launcher/open-shell-root", json={"path": str(tmp_path / "missing")})

    assert response.status_code == 400
    assert "Shell folder" in response.json()["detail"]


def test_launcher_validate_path_endpoint_checks_manual_paths(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "train.py"
    script_path.write_text("print('train')\n", encoding="utf-8")
    shell_dir = tmp_path / "shell_project"
    shell_dir.mkdir()

    script_response = client.get(
        "/api/launcher/validate-path",
        params={"kind": "python", "path": str(script_path)},
    )
    shell_response = client.get(
        "/api/launcher/validate-path",
        params={"kind": "shell", "path": str(shell_dir)},
    )
    missing_response = client.get(
        "/api/launcher/validate-path",
        params={"kind": "shell", "path": str(tmp_path / "missing")},
    )

    assert script_response.status_code == 200
    assert script_response.json()["ok"] is True
    assert script_response.json()["normalized_path"] == str(script_path).replace("\\", "/")
    assert shell_response.status_code == 200
    assert shell_response.json()["ok"] is True
    assert shell_response.json()["normalized_path"] == str(shell_dir).replace("\\", "/")
    assert missing_response.status_code == 200
    assert missing_response.json()["ok"] is False
    assert "does not exist" in missing_response.json()["message"]


def test_launcher_validate_config_path_resolves_relative_to_script_dir(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    script_path = tmp_path / "train.py"
    script_path.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")
    config_path = tmp_path / "configs" / "base.yaml"
    config_path.parent.mkdir()
    config_path.write_text("lr: 0.001\n", encoding="utf-8")

    response = client.get(
        "/api/launcher/validate-path",
        params={"kind": "config", "path": "configs/base.yaml", "script": str(script_path)},
    )

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["normalized_path"] == str(config_path).replace("\\", "/")


def test_tasks_endpoint_supports_offset_pagination(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    _add_task(workspace, "beta")
    _add_task(workspace, "gamma")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks", params={"offset": 1, "limit": 1})

    assert response.status_code == 200
    payload = response.json()
    assert payload["limit"] == 1
    assert payload["offset"] == 1
    assert len(payload["items"]) == 1
    assert payload["total"] == 3


def test_batch_run_and_delete_endpoints(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    _add_task(workspace, "beta")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    calls = []

    def fake_start_batch(task_names, max_workers=None):
        calls.append((list(task_names), max_workers))
        for task_name in task_names:
            task_dir = workspace / TASKS_DIR / task_name

            def apply(info):
                info["status"] = "queued"

            update_task_info(str(task_dir), apply)
        return list(task_names)

    with patch.object(runtime.task_manager, "start_batch_tasks", side_effect=fake_start_batch):
        run_response = client.post(
            "/api/tasks/batch/run",
            json={
                "task_names": ["alpha", "beta"],
                "max_workers": 5,
            },
        )

    delete_response = client.post("/api/tasks/batch/delete", json={"task_names": ["alpha"]})

    assert run_response.status_code == 200
    assert calls == [(["alpha", "beta"], 5)]
    assert run_response.json()["count"] == 2
    assert {item["status"] for item in run_response.json()["items"]} == {"queued"}
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] == ["alpha"]


def test_batch_run_reports_only_claimed_tasks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    _add_task(workspace, "beta", status="running")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with patch.object(runtime.task_manager, "start_batch_tasks", return_value=["alpha"]):
        response = client.post(
            "/api/tasks/batch/run",
            json={"task_names": ["alpha", "beta"]},
        )

    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert [item["name"] for item in response.json()["items"]] == ["alpha"]
    assert response.json()["skipped"] == ["beta"]


def test_batch_run_rejects_when_no_task_is_claimed(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with patch.object(runtime.task_manager, "start_batch_tasks", return_value=[]):
        response = client.post(
            "/api/tasks/batch/run",
            json={"task_names": ["alpha"]},
        )

    assert response.status_code == 400
    assert "could be started" in response.json()["detail"]

def test_pin_notes_env_and_rename_endpoints(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    pin_response = client.post("/api/tasks/alpha/pin", json={"pinned": True})
    notes_response = client.patch(
        "/api/tasks/alpha/notes",
        json={"notes": "needs review", "expected_notes": ""},
    )
    env_response = client.patch(
        "/api/tasks/alpha/env",
        json={"env": {"CUDA_VISIBLE_DEVICES": "0"}, "expected_env": {}},
    )
    rename_response = client.post("/api/tasks/alpha/rename", json={"new_name": "alpha-renamed"})

    assert pin_response.status_code == 200
    assert pin_response.json()["task"]["pinned"] is True
    assert notes_response.status_code == 200
    assert notes_response.json()["task"]["notes"] == "needs review"
    assert env_response.status_code == 200
    assert env_response.json()["task"]["env"]["CUDA_VISIBLE_DEVICES"] == "0"
    assert rename_response.status_code == 200
    assert rename_response.json()["task"]["name"] == "alpha-renamed"
    assert client.get("/api/tasks/alpha-renamed").status_code == 200


@pytest.mark.parametrize(
    ("endpoint", "field", "first_value", "stale_value"),
    [
        ("notes", "notes", "first", "stale"),
        ("env", "env", {"FIRST": "1"}, {"STALE": "1"}),
    ],
)
def test_task_metadata_endpoint_rejects_stale_write_without_overwriting_disk(
    tmp_path,
    endpoint,
    field,
    first_value,
    stale_value,
):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    expected_field = f"expected_{field}"
    empty_value = {} if field == "env" else ""

    first = client.patch(
        f"/api/tasks/alpha/{endpoint}",
        json={field: first_value, expected_field: empty_value},
    )
    stale = client.patch(
        f"/api/tasks/alpha/{endpoint}",
        json={field: stale_value, expected_field: empty_value},
    )
    blind = client.patch(
        f"/api/tasks/alpha/{endpoint}",
        json={field: stale_value},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]
    assert blind.status_code == 422
    task_dir = workspace / TASKS_DIR / "alpha"
    assert load_task_info(str(task_dir))[field] == first_value


@pytest.mark.parametrize(
    "env",
    [
        {"BAD=KEY": "x"},
        {"GOOD": "bad\x00value"},
    ],
)
def test_task_env_endpoint_rejects_values_that_subprocess_cannot_use(tmp_path, env):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.patch("/api/tasks/alpha/env", json={"env": env, "expected_env": {}})

    assert response.status_code == 400
    assert load_task_info(str(workspace / TASKS_DIR / "alpha")).get("env", {}) == {}


def test_reorder_tasks_endpoint_persists_manual_order_and_pin_state(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    _add_task(workspace, "beta")
    _add_task(workspace, "gamma")
    created_at_by_task = {
        "alpha": "2026-05-29_12-00-00",
        "beta": "2026-05-31_12-00-00",
        "gamma": "2026-05-30_12-00-00",
    }
    for task_name, created_at in created_at_by_task.items():
        task_dir = workspace / TASKS_DIR / task_name

        def apply(info, value=created_at):
            info["created_at"] = value

        update_task_info(str(task_dir), apply)

    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.post(
        "/api/tasks/reorder",
        json={
            "items": [
                {"name": "gamma", "pinned": True},
                {"name": "alpha", "pinned": False},
                {"name": "beta", "pinned": False},
            ]
        },
    )
    listed = client.get(
        "/api/tasks",
        params={"limit": 10_000, "refresh": True, "sort": "manual"},
    ).json()["items"]
    gamma_info = json.loads(
        (workspace / TASKS_DIR / "gamma" / "task_info.json").read_text(encoding="utf-8")
    )

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["gamma", "alpha", "beta"]
    assert response.json()["items"][0]["pinned"] is True
    assert [item["name"] for item in listed[:3]] == ["gamma", "alpha", "beta"]
    assert gamma_info["pinned"] is True
    assert gamma_info["task_order"] == 0


def test_tasks_endpoint_keeps_active_and_new_tasks_ahead_of_old_manual_order(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "completed-old", status="completed")
    _add_task(workspace, "pending-old")
    _add_task(workspace, "running-old", status="running")
    _add_task(workspace, "new-pending")

    updates = {
        "completed-old": {
            "created_at": "2026-05-28_02-25-46",
            "task_order": 0,
        },
        "pending-old": {
            "created_at": "2026-05-28_02-25-47",
            "task_order": 1,
        },
        "running-old": {
            "created_at": "2026-05-28_02-25-48",
            "start_times": ["2026-05-28_02-25-48"],
            "task_order": 2,
        },
        "new-pending": {
            "created_at": "2026-05-31_22-50-00",
        },
    }
    for task_name, patch_data in updates.items():
        task_dir = workspace / TASKS_DIR / task_name

        def apply(info, data=patch_data):
            info.update(data)

        update_task_info(str(task_dir), apply)

    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/tasks", params={"limit": 10_000, "refresh": True})

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["items"]]
    assert names[:4] == ["running-old", "new-pending", "completed-old", "pending-old"]


def test_tasks_endpoint_applies_selected_sort_before_pagination(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    _add_task(workspace, "beta")
    _add_task(workspace, "gamma")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get(
        "/api/tasks",
        params={"sort": "name_desc", "offset": 1, "limit": 1},
    )
    invalid = client.get("/api/tasks", params={"sort": "unsupported"})

    assert response.status_code == 200
    assert [item["name"] for item in response.json()["items"]] == ["beta"]
    assert invalid.status_code == 422


def test_logs_websocket_streams_live_chunks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    log_file.write_text("existing\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
        assert initialized.wait(2)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write("hello from stream")
        log_emitter.emit(
            "alpha",
            "hello from stream",
            offset=log_file.stat().st_size,
            task_dir=str(workspace / TASKS_DIR / "alpha"),
        )
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["content"] == "hello from stream"
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_closes_when_active_workspace_changes(tmp_path):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    _add_task(workspace_a, "same-name", status="completed", log_text="workspace-a\n")
    _add_task(workspace_b, "same-name", status="completed", log_text="workspace-b\n")
    runtime = _build_runtime(workspace_a)
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/same-name/logs/stream") as websocket:
        assert initialized.wait(2)
        runtime.change_run_root(str(workspace_b))
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_logs_websocket_stream_catches_up_from_client_offset(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    log_file.write_text("initial\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    initial = client.get("/api/tasks/alpha/logs", params={"log_file_name": "run1.log", "tail_lines": 20})
    assert initial.status_code == 200
    offset = initial.json()["offset"]
    with log_file.open("a", encoding="utf-8") as handle:
        handle.write("gap-before-ws\n")

    with client.websocket_connect(f"/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset={offset}") as websocket:
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert "gap-before-ws" in payload["content"]
    assert payload["offset"] == log_file.stat().st_size
    assert payload["log_file_name"] == "run1.log"


def test_logs_websocket_replays_backlog_without_poll_delay(tmp_path):
    import pyruns.web.runtime as runtime_module

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    log_file.write_text("A" * (5 * 1024), encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    original_get_logs = runtime.get_task_logs
    reads_complete = threading.Event()
    read_count = 0

    def tracked_get_logs(*args, **kwargs):
        nonlocal read_count
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("offset") is not None:
            read_count += 1
            if read_count >= 5:
                reads_complete.set()
        return payload

    with (
        patch.object(runtime, "get_task_logs", side_effect=tracked_get_logs),
        patch.object(runtime_module, "get_log_options", wraps=runtime_module.get_log_options) as get_options,
        patch("pyruns.web.app.LOG_STREAM_TAIL_CHUNK_SIZE", 1024),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 1.0),
    ):
        with client.websocket_connect(
            "/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset=0"
        ) as websocket:
            assert reads_complete.wait(1.5)
            chunks = [websocket.receive_json() for _ in range(5)]

    assert "".join(chunk["content"] for chunk in chunks) == "A" * (5 * 1024)
    assert all(chunk["type"] == "chunk" for chunk in chunks)
    assert chunks[-1]["offset"] == log_file.stat().st_size
    assert get_options.call_count == 1


def test_logs_websocket_replay_waits_for_full_send_queue(tmp_path):
    import asyncio

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    log_file.write_text("".join(char * 1024 for char in "ABCD"), encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    original_get_logs = runtime.get_task_logs
    original_send_json = WebSocket.send_json
    reads_complete = threading.Event()
    send_started = threading.Event()
    release_send = threading.Event()
    read_count = 0

    def tracked_get_logs(*args, **kwargs):
        nonlocal read_count
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("offset") is not None:
            read_count += 1
            if read_count >= 4:
                reads_complete.set()
        return payload

    async def blocked_send(self, data, mode="text"):
        if data.get("type") == "chunk" and not send_started.is_set():
            send_started.set()
            assert await asyncio.to_thread(release_send.wait, 2)
        await original_send_json(self, data, mode=mode)

    with (
        patch.object(runtime, "get_task_logs", side_effect=tracked_get_logs),
        patch.object(WebSocket, "send_json", blocked_send),
        patch("pyruns.web.app.LOG_STREAM_TAIL_CHUNK_SIZE", 1024),
        patch("pyruns.web.app.LOG_STREAM_QUEUE_LIMIT", 2),
    ):
        with client.websocket_connect(
            "/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset=0"
        ) as websocket:
            try:
                assert send_started.wait(2)
                assert reads_complete.wait(2)
            finally:
                release_send.set()
            first_three = [websocket.receive_json() for _ in range(3)]
            assert [item["content"] for item in first_three] == [char * 1024 for char in "ABC"]
            fourth = websocket.receive_json()

    assert fourth["content"] == "D" * 1024
    assert fourth["offset"] == log_file.stat().st_size


def test_logs_websocket_replays_live_chunks_when_send_queue_fills(tmp_path):
    import asyncio

    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    log_file = task_dir / "run_logs" / "run1.log"
    log_file.write_text("", encoding="utf-8")
    client = TestClient(create_app(_build_runtime(workspace)))
    original_send_json = WebSocket.send_json
    idle = threading.Event()
    send_started = threading.Event()
    release_send = threading.Event()

    def mark_idle(path):
        asyncio.get_running_loop().call_soon(idle.set)
        return log_file_identity(path)

    async def blocked_send(self, data, mode="text"):
        if data.get("content") == "A" and not send_started.is_set():
            send_started.set()
            assert await asyncio.to_thread(release_send.wait, 3)
        await original_send_json(self, data, mode=mode)

    with (
        patch.object(WebSocket, "send_json", blocked_send),
        patch.object(log_emitter, "subscribe", wraps=log_emitter.subscribe) as subscribe,
        patch("pyruns.web.app.log_file_identity", side_effect=mark_idle),
        patch("pyruns.web.app.LOG_STREAM_QUEUE_LIMIT", 2),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 10.0),
    ):
        with client.websocket_connect(
            "/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset=0"
        ) as websocket:
            try:
                assert idle.wait(2)
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write("A")
                log_emitter.emit(
                    "alpha", "A", offset=1, log_file_name="run1.log", task_dir=str(task_dir),
                )
                assert send_started.wait(2)
                for offset, char in enumerate("BCD", start=2):
                    with log_file.open("a", encoding="utf-8") as handle:
                        handle.write(char)
                    log_emitter.emit(
                        "alpha", char, offset=offset, log_file_name="run1.log", task_dir=str(task_dir),
                    )
                delivered = threading.Event()
                subscribe.call_args.kwargs["loop"].call_soon_threadsafe(delivered.set)
                assert delivered.wait(2)
            finally:
                release_send.set()
            first_three = [websocket.receive_json() for _ in range(3)]
            assert [item["content"] for item in first_three] == ["A", "B", "C"]
            fourth = websocket.receive_json()

    assert fourth["content"] == "D"
    assert fourth["offset"] == 4


def test_logs_websocket_replay_preserves_order_when_emitter_fires(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    log_file = task_dir / "run_logs" / "run1.log"
    log_file.write_text("A" * 2048, encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    original_get_logs = runtime.get_task_logs
    first_read = threading.Event()
    release_read = threading.Event()

    def pause_first_read(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("offset") == 0 and not first_read.is_set():
            first_read.set()
            assert release_read.wait(2)
        return payload

    with (
        patch.object(runtime, "get_task_logs", side_effect=pause_first_read),
        patch.object(log_emitter, "subscribe", wraps=log_emitter.subscribe) as subscribe,
        patch("pyruns.web.app.LOG_STREAM_TAIL_CHUNK_SIZE", 1024),
    ):
        with client.websocket_connect(
            "/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset=0"
        ) as websocket:
            try:
                assert first_read.wait(2)
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write("B" * 1024)
                log_emitter.emit(
                    "alpha", "B" * 1024, offset=log_file.stat().st_size,
                    log_file_name="run1.log", task_dir=str(task_dir),
                )
                delivered = threading.Event()
                subscribe.call_args.kwargs["loop"].call_soon_threadsafe(delivered.set)
                assert delivered.wait(2)
            finally:
                release_read.set()
            chunks = [websocket.receive_json() for _ in range(3)]

    assert "".join(chunk["content"] for chunk in chunks) == "A" * 2048 + "B" * 1024
    assert [chunk["offset"] for chunk in chunks] == [1024, 2048, 3072]


def test_logs_websocket_keeps_emitter_chunk_during_initial_selection(tmp_path):
    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    log_file = task_dir / "run_logs" / "run1.log"
    log_file.write_text("initial\n", encoding="utf-8")
    initial_offset = log_file.stat().st_size
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    initial_read = threading.Event()
    release_read = threading.Event()
    stale_read = threading.Event()
    polled = threading.Event()
    poll_count = 0
    original_get_logs = runtime.get_task_logs

    def pause_initial_read(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0 and not initial_read.is_set():
            initial_read.set()
            assert release_read.wait(2)
        if kwargs.get("offset") == initial_offset:
            stale_read.set()
        return payload

    def track_identity(path):
        nonlocal poll_count
        poll_count += 1
        if poll_count >= 3:
            polled.set()
        return log_file_identity(path)

    with (
        patch.object(runtime, "get_task_logs", side_effect=pause_initial_read),
        patch.object(log_emitter, "subscribe", wraps=log_emitter.subscribe) as subscribe,
        patch("pyruns.web.app.log_file_identity", side_effect=track_identity),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 0.01),
        patch("pyruns.web.app.LOG_STREAM_EMITTER_QUIET_SEC", 0),
    ):
        with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
            try:
                assert initial_read.wait(2)
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write("live\n")
                log_emitter.emit(
                    "alpha", "live\r\n", offset=log_file.stat().st_size,
                    byte_length=log_file.stat().st_size - initial_offset,
                    log_file_name="run1.log", task_dir=str(task_dir),
                )
                delivered = threading.Event()
                subscribe.call_args.kwargs["loop"].call_soon_threadsafe(delivered.set)
                assert delivered.wait(2)
            finally:
                release_read.set()
            payload = websocket.receive_json()
            assert polled.wait(2)

    assert payload["content"] == "live\r\n"
    assert payload["offset"] == log_file.stat().st_size
    assert not stale_read.is_set()


def test_logs_websocket_replays_gap_during_initial_selection(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    log_file = task_dir / "run_logs" / "run1.log"
    log_file.write_text("initial\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    initial_read = threading.Event()
    release_read = threading.Event()
    original_get_logs = runtime.get_task_logs

    def pause_initial_read(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0 and not initial_read.is_set():
            initial_read.set()
            assert release_read.wait(2)
        return payload

    with (
        patch.object(runtime, "get_task_logs", side_effect=pause_initial_read),
        patch.object(log_emitter, "subscribe", wraps=log_emitter.subscribe) as subscribe,
    ):
        with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
            try:
                assert initial_read.wait(2)
                with log_file.open("a", encoding="utf-8") as handle:
                    handle.write("silent\nlive\n")
                log_emitter.emit(
                    "alpha", "live\n", offset=log_file.stat().st_size,
                    byte_length=len("live\n"), log_file_name="run1.log", task_dir=str(task_dir),
                )
                delivered = threading.Event()
                subscribe.call_args.kwargs["loop"].call_soon_threadsafe(delivered.set)
                assert delivered.wait(2)
            finally:
                release_read.set()
            payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["content"].replace("\r", "") == "silent\nlive\n"
    assert payload["offset"] == log_file.stat().st_size


@pytest.mark.parametrize(
    ("file_suffix", "emitter_text"),
    [
        ("silent\nlive\n", "live\n"),
        ("Xa\nb\n", "a\r\nb\r\n"),
    ],
)
def test_logs_websocket_replays_file_gap_before_emitter_chunk(tmp_path, file_suffix, emitter_text):
    import asyncio

    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    log_file = task_dir / "run_logs" / "run1.log"
    log_file.write_text("base\n", encoding="utf-8")
    offset = log_file.stat().st_size
    client = TestClient(create_app(_build_runtime(workspace)))
    idle = threading.Event()

    def mark_idle(path):
        asyncio.get_running_loop().call_soon(idle.set)
        return log_file_identity(path)

    with (
        patch("pyruns.web.app.log_file_identity", side_effect=mark_idle),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 1.0),
    ):
        with client.websocket_connect(
            f"/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset={offset}"
        ) as websocket:
            assert idle.wait(2)
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write(file_suffix)
            log_emitter.emit(
                "alpha", emitter_text, offset=log_file.stat().st_size,
                log_file_name="run1.log", task_dir=str(task_dir),
            )
            payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["content"].replace("\r", "") == file_suffix
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_uses_written_byte_length_for_crlf_emitter(tmp_path):
    import asyncio

    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    log_file = task_dir / "run_logs" / "run1.log"
    log_file.write_text("base\n", encoding="utf-8")
    offset = log_file.stat().st_size
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    idle = threading.Event()

    def mark_idle(path):
        asyncio.get_running_loop().call_soon(idle.set)
        return log_file_identity(path)

    with (
        patch.object(runtime, "get_task_logs", wraps=runtime.get_task_logs) as get_logs,
        patch("pyruns.web.app.log_file_identity", side_effect=mark_idle),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 1.0),
    ):
        with client.websocket_connect(
            f"/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset={offset}"
        ) as websocket:
            assert idle.wait(2)
            with log_file.open("ab") as handle:
                handle.write(b"raw\r\n")
            log_emitter.emit(
                "alpha", "raw\r\n", offset=log_file.stat().st_size,
                byte_length=len(b"raw\r\n"), log_file_name="run1.log", task_dir=str(task_dir),
            )
            payload = websocket.receive_json()
            assert get_logs.call_count == 1

    assert payload["content"] == "raw\r\n"
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_ignores_emitter_for_another_run_log(tmp_path):
    import asyncio

    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    task_dir = workspace / TASKS_DIR / "alpha"
    run_log = task_dir / "run_logs" / "run1.log"
    run_log.write_text("base\n", encoding="utf-8")
    other_log = task_dir / "run_logs" / "run2.log"
    offset = run_log.stat().st_size
    client = TestClient(create_app(_build_runtime(workspace)))
    idle = threading.Event()

    def mark_idle(path):
        asyncio.get_running_loop().call_soon(idle.set)
        return log_file_identity(path)

    with (
        patch("pyruns.web.app.log_file_identity", side_effect=mark_idle),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 0.01),
    ):
        with client.websocket_connect(
            f"/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset={offset}"
        ) as websocket:
            assert idle.wait(2)
            other_log.write_text("other run\n", encoding="utf-8")
            log_emitter.emit(
                "alpha", "other run\n", offset=other_log.stat().st_size,
                log_file_name="run2.log", task_dir=str(task_dir),
            )
            with run_log.open("a", encoding="utf-8") as handle:
                handle.write("current run\n")
            payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["log_file_name"] == "run1.log"
    assert payload["content"].replace("\r", "") == "current run\n"


def test_logs_websocket_rejects_invalid_log_file_name(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    client = TestClient(create_app(_build_runtime(workspace)))

    with client.websocket_connect(
        "/api/tasks/alpha/logs/stream?log_file_name=..%2Fsecret.log&offset=0"
    ) as websocket:
        with pytest.raises(WebSocketDisconnect):
            websocket.receive_json()


def test_logs_websocket_resets_after_live_log_is_truncated(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    log_file.write_text("existing output that is longer than the replacement\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    initial = client.get(
        "/api/tasks/alpha/logs",
        params={"log_file_name": "run1.log", "tail_lines": 20},
    ).json()
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if (
            kwargs.get("offset") == initial["offset"]
            and kwargs.get("log_identity") == initial["log_identity"]
        ):
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    stream_url = (
        "/api/tasks/alpha/logs/stream?log_file_name=run1.log"
        f"&offset={initial['offset']}&log_identity={initial['log_identity']}"
    )
    with client.websocket_connect(stream_url) as websocket:
        assert initialized.wait(2)
        log_file.write_text("new\n", encoding="utf-8")
        payload = websocket.receive_json()

    assert payload["type"] == "reset"
    assert payload["task_name"] == "alpha"
    assert payload["log_file_name"] == "run1.log"
    assert payload["log_identity"] == initial["log_identity"]
    assert payload["content"].replace("\r", "") == "new\n"
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_stream_tails_run_log_file_without_emitter(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    log_file.write_text("existing\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
        assert initialized.wait(2)
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write("file fallback chunk\n")
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["content"].replace("\r\n", "\n") == "file fallback chunk\n"
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_idle_tail_skips_log_listing_but_catches_up(tmp_path):
    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running", log_text="existing\n")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    offset = log_file.stat().st_size
    polled = threading.Event()
    identity_calls = 0

    def track_identity(path):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls >= 3:
            polled.set()
        return log_file_identity(path)

    with (
        patch.object(runtime, "get_task_logs", wraps=runtime.get_task_logs) as get_logs,
        patch("pyruns.web.app.log_file_identity", side_effect=track_identity),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 0.01),
        patch("pyruns.web.app.LOG_STREAM_EMITTER_QUIET_SEC", 0),
    ):
        with client.websocket_connect(
            f"/api/tasks/alpha/logs/stream?log_file_name=run1.log&offset={offset}"
        ) as websocket:
            assert polled.wait(2)
            assert get_logs.call_count == 1
            with log_file.open("a", encoding="utf-8") as handle:
                handle.write("after idle\n")
            payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["content"].replace("\r", "") == "after idle\n"
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_stream_tails_queued_gpu_log_from_client_offset(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="queued")
    task_dir = workspace / TASKS_DIR / "alpha"
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("waiting\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    initial = client.get(
        "/api/tasks/alpha/logs",
        params={"log_file_name": "queue.log", "tail_lines": 20},
    )
    assert initial.status_code == 200
    offset = initial.json()["offset"]

    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("log_file_name") == "queue.log" and kwargs.get("offset") == offset:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    with client.websocket_connect(
        f"/api/tasks/alpha/logs/stream?log_file_name=queue.log&offset={offset}"
    ) as websocket:
        assert initialized.wait(2)
        with queue_log.open("a", encoding="utf-8", newline="") as handle:
            handle.write("\rstill waiting")
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["log_file_name"] == "queue.log"
    assert payload["content"] == "\rstill waiting"
    assert payload["offset"] == queue_log.stat().st_size


def test_logs_websocket_idle_queue_skips_log_listing_until_run_starts(tmp_path):
    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="queued")
    task_dir = workspace / TASKS_DIR / "alpha"
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("waiting\n", encoding="utf-8")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    offset = queue_log.stat().st_size
    polled = threading.Event()
    identity_calls = 0

    def track_identity(path):
        nonlocal identity_calls
        identity_calls += 1
        if identity_calls >= 3:
            polled.set()
        return log_file_identity(path)

    with (
        patch.object(runtime, "get_task_logs", wraps=runtime.get_task_logs) as get_logs,
        patch("pyruns.web.app.log_file_identity", side_effect=track_identity),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 0.01),
        patch("pyruns.web.app.LOG_STREAM_EMITTER_QUIET_SEC", 0),
    ):
        with client.websocket_connect(
            f"/api/tasks/alpha/logs/stream?log_file_name=queue.log&offset={offset}"
        ) as websocket:
            assert polled.wait(2)
            assert get_logs.call_count == 1
            with runtime.task_manager._lock:
                current = runtime.task_manager._tasks_by_name["alpha"]
                current["status"] = "running"
                current["run_index"] = 1
            run_log = task_dir / "run_logs" / "run1.log"
            run_log.write_text("running after queue\n", encoding="utf-8")
            payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["log_file_name"] == "run1.log"
    assert payload["content"].replace("\r", "") == "running after queue\n"


def test_logs_websocket_drains_queue_log_before_run_emitter(tmp_path):
    import asyncio

    from pyruns.utils.log_io import log_file_identity

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="queued")
    task_dir = workspace / TASKS_DIR / "alpha"
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("waiting\n", encoding="utf-8")
    run_log = task_dir / "run_logs" / "run1.log"
    offset = queue_log.stat().st_size
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    idle = threading.Event()

    def mark_idle(path):
        asyncio.get_running_loop().call_soon(idle.set)
        return log_file_identity(path)

    with (
        patch("pyruns.web.app.log_file_identity", side_effect=mark_idle),
        patch("pyruns.web.app.LOG_STREAM_TAIL_INTERVAL_SEC", 1.0),
    ):
        with client.websocket_connect(
            f"/api/tasks/alpha/logs/stream?log_file_name=queue.log&offset={offset}"
        ) as websocket:
            assert idle.wait(2)
            with queue_log.open("a", encoding="utf-8") as handle:
                handle.write("assigned\n")
            with runtime.task_manager._lock:
                current = runtime.task_manager._tasks_by_name["alpha"]
                current["status"] = "running"
                current["run_index"] = 1
            run_log.write_text("run start\n", encoding="utf-8")
            log_emitter.emit(
                "alpha", "run start\n", offset=run_log.stat().st_size,
                byte_length=run_log.stat().st_size,
                log_file_name="run1.log", task_dir=str(task_dir),
            )
            queue_chunk = websocket.receive_json()
            assert queue_chunk["log_file_name"] == "queue.log"
            assert queue_chunk["content"].replace("\r", "") == "assigned\n"
            run_chunk = websocket.receive_json()

    assert run_chunk["log_file_name"] == "run1.log"
    assert run_chunk["content"].replace("\r", "") == "run start\n"


def test_logs_websocket_stream_tails_active_run_log_created_after_connect(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    log_file = workspace / TASKS_DIR / "alpha" / "run_logs" / "run1.log"
    assert not log_file.exists()
    runtime = _build_runtime(workspace)
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
        assert initialized.wait(2)
        log_file.write_text("created after connect\n", encoding="utf-8")
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["content"].replace("\r\n", "\n") == "created after connect\n"
    assert payload["offset"] == log_file.stat().st_size


def test_logs_websocket_stream_finds_queue_log_created_after_connect(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="queued")
    queue_log = workspace / TASKS_DIR / "alpha" / "run_logs" / "queue.log"
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    initialized = threading.Event()
    discovered = threading.Event()
    original_get_logs = runtime.get_task_logs

    def track_selection(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
            if payload.get("selected_log") == "queue.log":
                discovered.set()
        return payload

    with patch.object(runtime, "get_task_logs", side_effect=track_selection):
        with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
            assert initialized.wait(2)
            queue_log.write_text("queued after connect\n", encoding="utf-8")
            assert discovered.wait(2)
            payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["log_file_name"] == "queue.log"
    assert payload["content"].replace("\r", "") == "queued after connect\n"


def test_logs_websocket_stream_switches_from_queue_log_to_active_run_log(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="queued")
    task_dir = workspace / TASKS_DIR / "alpha"
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("waiting\n", encoding="utf-8")
    run_log = task_dir / "run_logs" / "run1.log"
    runtime = _build_runtime(workspace)
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
        assert initialized.wait(2)
        with runtime.task_manager._lock:
            current = runtime.task_manager._tasks_by_name["alpha"]
            current["status"] = "running"
            current["run_index"] = 1
        run_log.write_text("running after queue\n", encoding="utf-8")
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["content"].replace("\r\n", "\n") == "running after queue\n"
    assert payload["offset"] == run_log.stat().st_size


def test_logs_websocket_stream_accepts_run_log_emitter_chunk_after_queue_offset(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="queued")
    task_dir = workspace / TASKS_DIR / "alpha"
    queue_log = task_dir / "run_logs" / "queue.log"
    queue_log.write_text("waiting in a much larger queue log\n", encoding="utf-8")
    run_log = task_dir / "run_logs" / "run1.log"
    runtime = _build_runtime(workspace)
    initialized = threading.Event()
    original_get_logs = runtime.get_task_logs

    def tracked_get_logs(*args, **kwargs):
        payload = original_get_logs(*args, **kwargs)
        if kwargs.get("tail_lines") == 0:
            initialized.set()
        return payload

    runtime.get_task_logs = tracked_get_logs
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
        assert initialized.wait(2)
        with runtime.task_manager._lock:
            current = runtime.task_manager._tasks_by_name["alpha"]
            current["status"] = "running"
            current["run_index"] = 1
        run_log.write_text("run start\n", encoding="utf-8")
        log_emitter.emit(
            "alpha",
            "run start\r\n",
            offset=run_log.stat().st_size,
            log_file_name="run1.log",
            task_dir=str(task_dir),
        )
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["content"] == "run start\r\n"
    assert payload["offset"] == run_log.stat().st_size
    assert payload["log_file_name"] == "run1.log"


def test_logs_websocket_stream_uses_bounded_queue():
    source = WEB_APP.read_text(encoding="utf-8")

    assert "LOG_STREAM_QUEUE_LIMIT" in source
    assert "LOG_STREAM_TAIL_INTERVAL_SEC" in source
    assert "LOG_STREAM_EMITTER_QUIET_SEC" in source
    assert "tail_log_file" in source
    assert "tail_lines=0" in source
    assert "emitter_quiet = time.monotonic() - last_emitter_chunk_at >= LOG_STREAM_EMITTER_QUIET_SEC" in source
    assert "stream_log_name == QUEUE_LOG_FILENAME" in source
    assert "asyncio.Queue(maxsize=LOG_STREAM_QUEUE_LIMIT)" in source
    assert "include_metadata=True" in source
    assert "stream_offsets: dict[str, int]" in source
    assert "except asyncio.QueueFull" in source
    assert "queue.get_nowait()" in source


def test_metrics_endpoint_returns_sampler_payload(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    runtime.metrics_sampler.sample = MagicMock(return_value={
        "cpu_percent": 10.0,
        "mem_percent": 20.0,
        "gpus": [],
    })
    client = TestClient(create_app(runtime))

    response = client.get("/api/system/metrics")

    assert response.status_code == 200
    assert response.json()["cpu_percent"] == 10.0
    runtime.metrics_sampler.sample.assert_called_once_with(
        include_processes=False,
        detail=False,
    )

    response = client.get(
        "/api/system/metrics?include_processes=true&detail=true"
    )

    assert response.status_code == 200
    runtime.metrics_sampler.sample.assert_called_with(
        include_processes=True,
        detail=True,
    )


def test_process_details_endpoint_reads_only_requested_pid(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    runtime.metrics_sampler.get_process_details = MagicMock(return_value={
        "pid": 4321,
        "available": True,
        "user": "researcher",
    })
    client = TestClient(create_app(runtime))

    response = client.get("/api/system/processes/4321")

    assert response.status_code == 200
    assert response.json()["pid"] == 4321
    runtime.metrics_sampler.get_process_details.assert_called_once_with(4321)


def test_process_details_endpoint_rejects_invalid_pid(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    client = TestClient(create_app(_build_runtime(workspace)))

    response = client.get("/api/system/processes/0")

    assert response.status_code == 400
    assert "positive integer" in response.json()["detail"]


def test_runtime_helper_edges_and_conda_error_paths(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_mod

    assert runtime_mod._int_setting({"x": "bad"}, "x", 5) == 5
    assert runtime_mod._int_setting({"x": 0}, "x", 5, minimum=2) == 2
    assert runtime_mod._clip_text_middle("abcdef", 0) == ""
    assert runtime_mod._clip_text_middle("abcdef", 6) == "abcdef"
    assert runtime_mod._clip_text_middle("abcdef", 3) == "abc"
    assert "[truncated]" in runtime_mod._clip_text_middle("a" * 80, 40)

    monkeypatch.setattr(runtime_mod._cfg, "DEFAULT_TASK_SUMMARY_SEARCH_TEXT_CHARS", 10)
    capped = runtime_mod._cap_summary_task_payloads([
        {"name": "short", "search_text": "abc"},
        {"name": "long", "search_text": "x" * 30},
    ])
    assert capped[0]["search_text"] == "abc"
    assert len(capped[1]["search_text"]) == 10

    executable = tmp_path / "python.exe"
    executable.write_text("", encoding="utf-8")
    assert runtime_mod.PyrunsRuntime._resolve_executable("") == ""
    assert runtime_mod.PyrunsRuntime._resolve_executable(str(tmp_path / "missing.exe")) == ""
    assert runtime_mod.PyrunsRuntime._resolve_executable(str(executable)) == str(executable.resolve())
    monkeypatch.setattr(runtime_mod.shutil, "which", lambda value: str(executable) if value == "py" else None)
    assert runtime_mod.PyrunsRuntime._resolve_executable("py") == str(executable.resolve())
    assert runtime_mod.PyrunsRuntime._env_name_from_path("/opt/conda", "/opt/conda") == "base"

    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    runtime.settings["conda_executable"] = "missing-conda"
    assert runtime.list_conda_envs()["available"] is False

    runtime.settings["conda_executable"] = "conda"
    monkeypatch.setattr(runtime, "_resolve_executable", lambda value: "/bin/conda")

    def raise_on_env_list(command, **kwargs):
        if command[1:3] == ["info", "--json"]:
            raise RuntimeError("info failed")
        raise OSError("env list failed")

    monkeypatch.setattr(runtime_mod.subprocess, "run", raise_on_env_list)
    payload = runtime.list_conda_envs()
    assert payload["available"] is False
    assert "env list failed" in payload["error"]

    class Result:
        def __init__(self, returncode, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(
        runtime_mod.subprocess,
        "run",
        lambda command, **kwargs: Result(1, stdout="stdout failure") if command[1:4] == ["env", "list", "--json"] else Result(0, stdout="{}"),
    )
    payload = runtime.list_conda_envs()
    assert payload["available"] is False
    assert payload["error"] == "stdout failure"


def test_runtime_list_conda_envs_parses_successful_payloads(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_mod

    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    root_prefix = tmp_path / "conda"
    env_prefix = root_prefix / "envs" / "train"
    runtime.settings["conda_executable"] = "conda"
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "train")
    monkeypatch.setattr(runtime, "_resolve_executable", lambda value: str(tmp_path / "conda.exe"))

    class Result:
        def __init__(self, returncode=0, stdout="", stderr=""):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command, **kwargs):
        if command[1:3] == ["info", "--json"]:
            return Result(stdout=json.dumps({"root_prefix": str(root_prefix)}))
        return Result(stdout=json.dumps({"envs": [str(root_prefix), str(env_prefix), str(env_prefix)]}))

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)

    payload = runtime.list_conda_envs()

    assert payload["available"] is True
    assert [item["name"] for item in payload["envs"]] == ["base", "train"]
    assert payload["envs"][1]["active"] is True


def test_runtime_task_operation_error_branches(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    runtime.ensure_tasks_loaded()

    monkeypatch.setattr(runtime, "require_task", lambda name, refresh=True: {"name": name, "dir": str(workspace / TASKS_DIR / "alpha"), "_load_error": "load failed"})
    with pytest.raises(ValueError, match="load failed"):
        runtime.start_task("alpha")
    with pytest.raises(ValueError, match="load failed"):
        runtime.start_tasks_batch(["alpha"])

    monkeypatch.setattr(runtime, "require_task", lambda name, refresh=True: {"name": name, "dir": str(workspace / TASKS_DIR / "alpha")})
    monkeypatch.setattr(
        runtime.task_manager,
        "request_task_cancel",
        lambda name, **_identity: False,
    )
    with pytest.raises(ValueError, match="cannot be cancelled"):
        runtime.cancel_task("alpha")

    with pytest.raises(ValueError, match="No valid tasks"):
        runtime.start_tasks_batch(["", " "])
    with pytest.raises(ValueError, match="No valid tasks"):
        runtime.delete_tasks_batch(["", " "])
    with pytest.raises(ValueError, match="No valid tasks"):
        runtime.export_tasks_csv(["", " "])

    monkeypatch.setattr(runtime.task_manager, "delete_tasks", lambda names: [])
    with pytest.raises(ValueError, match="Could not move any selected tasks to trash"):
        runtime.delete_tasks_batch(["alpha"])

    monkeypatch.setattr(runtime.task_manager, "set_task_pinned", lambda name, pinned: (False, "Task not found"))
    with pytest.raises(KeyError):
        runtime.set_task_pin("alpha", True)
    monkeypatch.setattr(runtime.task_manager, "set_task_pinned", lambda name, pinned: (False, "bad pin"))
    with pytest.raises(ValueError, match="bad pin"):
        runtime.set_task_pin("alpha", True)

    monkeypatch.setattr(runtime.task_manager, "update_task_notes", lambda name, notes, expected: (False, "Task not found"))
    with pytest.raises(KeyError):
        runtime.update_task_notes("alpha", "note", "")
    monkeypatch.setattr(runtime.task_manager, "update_task_notes", lambda name, notes, expected: (False, "bad notes"))
    with pytest.raises(ValueError, match="bad notes"):
        runtime.update_task_notes("alpha", "note", "")

    monkeypatch.setattr(runtime.task_manager, "update_task_env", lambda name, env, expected: (False, "Task not found"))
    with pytest.raises(KeyError):
        runtime.update_task_env("alpha", {}, {})
    monkeypatch.setattr(runtime.task_manager, "update_task_env", lambda name, env, expected: (False, "bad env"))
    with pytest.raises(ValueError, match="bad env"):
        runtime.update_task_env("alpha", {}, {})

    monkeypatch.setattr(runtime.task_manager, "rename_task", lambda name, new_name: (False, "Task not found"))
    with pytest.raises(KeyError):
        runtime.rename_task("alpha", "beta")
    monkeypatch.setattr(runtime.task_manager, "rename_task", lambda name, new_name: (False, "bad rename"))
    with pytest.raises(ValueError, match="bad rename"):
        runtime.rename_task("alpha", "beta")

    monkeypatch.setattr(runtime.task_manager, "reorder_tasks", lambda items: (False, "Task not found: ghost"))
    with pytest.raises(KeyError):
        runtime.reorder_tasks([{"name": "ghost"}])
    monkeypatch.setattr(runtime.task_manager, "reorder_tasks", lambda items: (False, "bad order"))
    with pytest.raises(ValueError, match="bad order"):
        runtime.reorder_tasks([])


def test_runtime_cancel_binds_the_refreshed_runner_and_run(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    task_dir = workspace / TASKS_DIR / "alpha"
    captured = {}

    def request_cancel(name, **kwargs):
        captured["name"] = name
        captured.update(kwargs)
        return True

    monkeypatch.setattr(runtime.task_manager, "request_task_cancel", request_cancel)

    result = runtime.cancel_task("alpha")

    info = load_task_info(str(task_dir))
    assert result["name"] == "alpha"
    assert captured == {
        "name": "alpha",
        "expected_runner_id": info["runner_id"],
        "expected_run_index": 1,
    }


def test_runtime_cancel_returns_a_task_that_finished_during_the_request(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    task_dir = workspace / TASKS_DIR / "alpha"

    def finish_before_cancel(_name, **_identity):
        update_task_info(
            str(task_dir),
            lambda info: info.update({"status": "completed"}),
        )
        return False

    monkeypatch.setattr(runtime.task_manager, "request_task_cancel", finish_before_cancel)

    result = runtime.cancel_task("alpha")

    assert result["status"] == "completed"


def test_runtime_cancel_accepts_a_persisted_local_request_while_runner_retries(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    task_dir = workspace / TASKS_DIR / "alpha"
    runner_id = runtime.task_manager.runner_id

    def persist_request(_name, **_identity):
        update_task_info(
            str(task_dir),
            lambda info: info.update({"cancel_requested_at": "2026-03-20_00-00-02"}),
        )
        return False

    monkeypatch.setattr(runtime.task_manager, "request_task_cancel", persist_request)

    result = runtime.cancel_task("alpha")

    assert result["status"] == "running"
    assert result["runner_id"] == runner_id


def test_runtime_cancel_accepts_a_pending_local_stop_request(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    task_dir = workspace / TASKS_DIR / "alpha"
    runner_id = runtime.task_manager.runner_id

    def persist_pending_request(_name, **_identity):
        update_task_info(
            str(task_dir),
            lambda info: info.update(
                {
                    "cancel_requested_at": "2026-03-20_00-00-02",
                    "_pending_stop_summary": {
                        "run_index": active_task_run_index(info),
                        "event": "stopped",
                        "reason": "cancelled_by_user",
                    },
                }
            ),
        )
        return False

    monkeypatch.setattr(runtime.task_manager, "request_task_cancel", persist_pending_request)

    result = runtime.cancel_task("alpha")

    assert result["status"] == "running"
    assert result["runner_id"] == runner_id


def test_runtime_cancel_is_idempotent_for_an_already_terminal_task(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="completed")
    runtime = _build_runtime(workspace)

    monkeypatch.setattr(
        runtime.task_manager,
        "request_task_cancel",
        lambda *_args, **_kwargs: pytest.fail("a terminal task must not receive a cancel request"),
    )

    result = runtime.cancel_task("alpha")

    assert result["status"] == "completed"


def test_runtime_cancel_rejects_a_newer_run_that_finished_during_the_request(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    task_dir = workspace / TASKS_DIR / "alpha"

    def finish_newer_run_before_cancel(_name, **_identity):
        update_task_info(
            str(task_dir),
            lambda info: info.update(
                {
                    "status": "completed",
                    "run_index": 2,
                    "run_statuses": ["completed", "completed"],
                }
            ),
        )
        return False

    monkeypatch.setattr(
        runtime.task_manager,
        "request_task_cancel",
        finish_newer_run_before_cancel,
    )

    with pytest.raises(ValueError, match="cannot be cancelled"):
        runtime.cancel_task("alpha")


def test_runtime_workspace_reload_shutdown_and_path_edges(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_mod

    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    old_manager = runtime.task_manager
    shutdowns = []
    monkeypatch.setattr(old_manager, "shutdown", lambda: shutdowns.append("old"))

    new_workspace = _make_workspace(tmp_path, "next")
    info = runtime.change_run_root(str(new_workspace))

    assert info["run_root"] == str(new_workspace).replace("\\", "/")
    assert shutdowns == ["old"]

    with pytest.raises(ValueError, match="Run Root must contain"):
        runtime.change_run_root(str(tmp_path / "plain"))

    manager = runtime.task_manager
    monkeypatch.setattr(manager, "shutdown", lambda: shutdowns.append("current"))
    runtime.shutdown()
    assert shutdowns.count("old") == 1
    assert "current" in shutdowns

    shell_workspace = _make_workspace(tmp_path, SHELL_WORKSPACE_NAME)
    (shell_workspace / SCRIPT_INFO_FILENAME).write_text(json.dumps({"workspace_kind": WORKSPACE_KIND_SHELL}), encoding="utf-8")
    shell_runtime = _build_runtime(shell_workspace)
    shell_info = shell_runtime.get_workspace_info()
    assert shell_info["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert shell_info["working_root"].endswith(str(tmp_path).replace("\\", "/"))

    assert runtime_mod._coerce_bool_payload(True) is True
    assert runtime_mod._coerce_bool_payload("yes") is True
    assert runtime_mod._coerce_bool_payload("no") is False
    assert runtime_mod._coerce_int_payload("bad", 7, minimum=2) == 7
    assert runtime_mod._coerce_float_payload("bad", 1.5, minimum=2.0) == 2.0
    assert runtime_mod._coerce_gpu_device_ids_payload(["0", "0", "x", 2]) == [0, 2]
    assert runtime_mod._coerce_gpu_device_ids_payload(object()) == []


def test_runtime_generator_preview_and_create_error_edges(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)

    with pytest.raises(ValueError, match="Unsupported generator mode"):
        runtime.preview_tasks_from_template(mode="bad", yaml_text="lr: 1")
    with pytest.raises(ValueError, match="Invalid YAML"):
        runtime.preview_tasks_from_template(mode="form", yaml_text="lr: [")
    with pytest.raises(ValueError, match="mapping"):
        runtime.preview_tasks_from_template(mode="form", yaml_text="[1, 2]")
    with pytest.raises(ValueError, match="YAML mode does not support batch syntax"):
        runtime.preview_tasks_from_template(mode="yaml", yaml_text="lr: 1 | 2")

    template = workspace / CONFIG_DEFAULT_FILENAME
    template.write_text("lr: 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="类型错误"):
        runtime.preview_tasks_from_template(mode="form", yaml_text="lr: text", template_value=CONFIG_DEFAULT_FILENAME)

    result = runtime.preview_tasks_from_template(mode="form", yaml_text="lr: 1 | 2")
    assert result["count"] == 2
    assert len(result["items"]) == 2

    created = runtime.create_tasks_from_template(name_prefix="unit", mode="yaml", yaml_text="lr: 3", append_timestamp=False)
    assert created["count"] == 1
    assert created["task_kind"] == TASK_KIND_CONFIG

    with pytest.raises(ValueError, match="Unsupported generator mode"):
        runtime.create_tasks_from_template(name_prefix="bad", mode="bad", yaml_text="lr: 1", append_timestamp=False)

    shell_workspace = _make_workspace(tmp_path, SHELL_WORKSPACE_NAME)
    (shell_workspace / SCRIPT_INFO_FILENAME).write_text(json.dumps({"workspace_kind": WORKSPACE_KIND_SHELL}), encoding="utf-8")
    shell_runtime = _build_runtime(shell_workspace)
    with pytest.raises(ValueError, match="Shell workspace only supports shell mode"):
        shell_runtime.preview_tasks_from_template(mode="form", shell_text="echo hi")
    with pytest.raises(ValueError, match="non-empty"):
        shell_runtime.preview_tasks_from_template(mode="shell", shell_text="")
    shell_preview = shell_runtime.preview_tasks_from_template(mode="shell", shell_text="echo hi")
    assert shell_preview["task_kind"] == TASK_KIND_SHELL

    shell_created = shell_runtime.create_tasks_from_template(
        name_prefix="shell-task",
        mode="shell",
        shell_text="echo hi",
        append_timestamp=False,
    )
    assert shell_created["count"] == 1
    assert shell_created["task_kind"] == TASK_KIND_SHELL


def test_runtime_export_tasks_csv_handles_duplicate_names_and_empty_monitor_data(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "with-records", status="completed")
    update_task_info(
        str(workspace / TASKS_DIR / "with-records"),
        lambda info: info.update({"records": [{"loss": 0.12}]}),
    )
    _add_task(workspace, "no-records", status="completed")
    runtime = _build_runtime(workspace)

    csv_text = runtime.export_tasks_csv(["with-records", "", "with-records"])

    assert "with-records" in csv_text
    assert "loss" in csv_text
    with pytest.raises(ValueError, match="No valid tasks"):
        runtime.export_tasks_csv(["", " "])


def test_runtime_log_selection_and_launcher_picker_edges(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_mod

    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "missing-log", status="completed")
    _add_task(workspace, "running-log", status="running")
    running = workspace / TASKS_DIR / "running-log"
    log_dir = running / "run_logs"
    log_dir.mkdir(exist_ok=True)
    (log_dir / "run1.log").write_text("hello\nworld\n", encoding="utf-8")
    runtime = _build_runtime(workspace)

    empty_logs = runtime.get_task_logs("missing-log", log_file_name="run99.log")
    assert empty_logs["content"] == ""
    assert empty_logs["selected_log"] == "run99.log"
    with pytest.raises(KeyError):
        runtime.get_task_logs("ghost")

    monkeypatch.setattr(
        runtime,
        "get_task",
        lambda task_name, refresh=False: {
            "name": task_name,
            "dir": str(running),
            "status": "running",
            "run_index": "bad",
        },
    )
    payload = runtime.get_task_logs("running-log", tail_lines=1)
    assert payload["selected_log"] == "run1.log"
    assert "world" in payload["content"]
    chunk_payload = runtime.get_task_logs("running-log", offset=0, chunk_size=5)
    assert chunk_payload["content"].startswith("hello")
    tail_payload = runtime.get_task_logs("running-log", tail_bytes=5)
    tail_content = tail_payload["content"].replace("\r\n", "\n")
    assert "hello\nworld\n".endswith(tail_content)
    assert tail_content.endswith("ld\n")

    script = tmp_path / "train.py"
    config = tmp_path / "config.yaml"
    script.write_text("print('x')\n", encoding="utf-8")
    config.write_text("lr: 1\n", encoding="utf-8")
    assert runtime.validate_launcher_path("python", str(script))["ok"] is True
    assert runtime.validate_launcher_path("python", str(config))["ok"] is False
    assert runtime.validate_launcher_path("shell", str(tmp_path))["ok"] is True
    assert runtime.validate_launcher_path("config", "config.yaml", script_path=str(script))["ok"] is True
    assert runtime.validate_launcher_path("config", str(tmp_path / "bad.txt"))["ok"] is False
    assert runtime.validate_launcher_path("weird", str(tmp_path))["ok"] is False
    assert runtime.validate_launcher_path("python", "")["message"] == "Path is empty."

    monkeypatch.setattr(runtime_mod, "native_picker_available", lambda: False)
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_launcher_script_path()
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_launcher_config_path(str(script))
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_and_open_launcher_workspace()
    with pytest.raises(ValueError, match="Native folder picker"):
        runtime.pick_and_open_shell_workspace()

    monkeypatch.setattr(runtime_mod, "native_picker_available", lambda: True)
    monkeypatch.setattr(runtime_mod, "choose_script_file", lambda initial: "")
    with pytest.raises(ValueError, match="No script selected"):
        runtime.pick_launcher_script_path()
    with pytest.raises(ValueError, match="No script selected"):
        runtime.pick_and_open_launcher_workspace()
    monkeypatch.setattr(runtime_mod, "choose_directory", lambda initial: "")
    with pytest.raises(ValueError, match="No directory selected"):
        runtime.pick_and_open_shell_workspace()
    with pytest.raises(FileNotFoundError):
        runtime.pick_launcher_config_path(str(tmp_path / "missing.py"))

    config_info = runtime.get_template_content(CONFIG_DEFAULT_FILENAME)
    assert config_info["mode_hint"] == "yaml"
    assert config_info["parsed_config"]["lr"] == 0.01
    shell_template = workspace / "run.sh"
    shell_template.write_text("echo hi\n", encoding="utf-8")
    shell_info = runtime.get_template_content(str(shell_template))
    assert shell_info["mode_hint"] == "shell"


def test_runtime_shell_templates_only_include_task_payloads(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    project_root = tmp_path / "shell-project"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    (project_root / ".git" / "ignored.sh").write_text("ignored\n", encoding="utf-8")
    (project_root / "run.sh").write_text("echo run\n", encoding="utf-8")
    (project_root / "notes.txt").write_text("ignore\n", encoding="utf-8")
    nested = project_root / "scripts" / "deep"
    nested.mkdir(parents=True)
    (nested / "train.sh").write_text("echo train\n", encoding="utf-8")

    script_info = {
        "workspace_kind": WORKSPACE_KIND_SHELL,
        "project_root": str(project_root),
    }
    (workspace / "script_info.json").write_text(json.dumps(script_info), encoding="utf-8")
    shell_task_dir = workspace / TASKS_DIR / "from-task"
    shell_task_dir.mkdir(parents=True)
    (shell_task_dir / "run.sh").write_text("echo task\n", encoding="utf-8")
    save_task_info(
        str(shell_task_dir),
        {
            "name": "from-task",
            "task_kind": TASK_KIND_SHELL,
            "config_file": "run.sh",
            "status": "completed",
        },
    )
    hidden_task = workspace / TASKS_DIR / ".hidden"
    hidden_task.mkdir()
    (workspace / TASKS_DIR / "not-a-dir").write_text("skip", encoding="utf-8")

    runtime = _build_runtime(workspace)
    original_getmtime = __import__("os").path.getmtime

    def fake_getmtime(path):
        if str(path).endswith("task_info.json"):
            raise OSError("missing mtime")
        return original_getmtime(path)

    monkeypatch.setattr("pyruns.web.runtime.os.path.getmtime", fake_getmtime)

    items = runtime.list_shell_templates(script_info)
    labels = [item["label"] for item in items]

    assert labels[0] == "from-task"
    assert "run.sh" not in labels
    assert "scripts/deep/train.sh" not in labels
    assert ".git/ignored.sh" not in labels
    assert "notes.txt" not in labels


def test_runtime_shell_templates_follow_manager_order(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    runtime.open_shell_workspace()

    def add_shell_task(name, **info):
        task_dir = Path(runtime.tasks_dir) / name
        task_dir.mkdir(parents=True)
        (task_dir / SHELL_CONFIG_FILENAME).write_text(f"echo {name}\n", encoding="utf-8")
        save_task_info(
            str(task_dir),
            {
                "name": name,
                "task_kind": TASK_KIND_SHELL,
                "config_file": SHELL_CONFIG_FILENAME,
                "status": "pending",
                "created_at": "2026-05-28_02-25-46",
                "start_times": [],
                "finish_times": [],
                "pinned": False,
                **info,
            },
        )

    add_shell_task("manual-completed", status="completed", task_order=0)
    add_shell_task("fresh-new", created_at="2026-05-31_22-50-00")
    add_shell_task(
        "running-manual",
        status="running",
        start_times=["2026-05-28_02-25-48"],
        task_order=2,
    )
    add_shell_task("pinned-fresh", pinned=True, created_at="2026-05-31_22-55-00")

    assert [item["label"] for item in runtime.list_shell_templates()] == [
        "pinned-fresh",
        "running-manual",
        "fresh-new",
        "manual-completed",
    ]


def test_runtime_generator_shell_and_picker_error_branches(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    runtime.open_shell_workspace()

    with pytest.raises(ValueError, match="only supports shell mode"):
        runtime.preview_tasks_from_template(mode="form", yaml_text="a: 1")
    with pytest.raises(ValueError, match="requires non-empty"):
        runtime.preview_tasks_from_template(mode="shell", shell_text="")
    with pytest.raises(ValueError, match="only supports shell mode"):
        runtime.create_tasks_from_template(name_prefix="x", mode="yaml", yaml_text="a: 1", append_timestamp=False)
    with pytest.raises(ValueError, match="requires non-empty"):
        runtime.create_tasks_from_template(name_prefix="x", mode="shell", shell_text="", append_timestamp=False)

    monkeypatch.setattr("pyruns.web.runtime.native_picker_available", lambda: False)
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_generator_shell_file()

    monkeypatch.setattr("pyruns.web.runtime.native_picker_available", lambda: True)
    monkeypatch.setattr("pyruns.web.runtime.choose_shell_file", lambda initial_dir: "")
    with pytest.raises(ValueError, match="No shell script selected"):
        runtime.pick_generator_shell_file()

    monkeypatch.setattr("pyruns.web.runtime.choose_shell_file", lambda initial_dir: str(tmp_path / "missing.sh"))
    with pytest.raises(FileNotFoundError, match="Shell script not found"):
        runtime.pick_generator_shell_file()


def test_runtime_launcher_picker_error_branches(tmp_path, monkeypatch):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)

    monkeypatch.setattr("pyruns.web.runtime.native_picker_available", lambda: False)
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_launcher_script_path()
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_launcher_config_path(str(tmp_path / "train.py"))
    with pytest.raises(ValueError, match="Native file picker"):
        runtime.pick_and_open_launcher_workspace()
    with pytest.raises(ValueError, match="Native folder picker"):
        runtime.pick_and_open_shell_workspace()

    monkeypatch.setattr("pyruns.web.runtime.native_picker_available", lambda: True)
    monkeypatch.setattr("pyruns.web.runtime.choose_script_file", lambda initial_dir: "")
    with pytest.raises(ValueError, match="No script selected"):
        runtime.pick_launcher_script_path()
    with pytest.raises(ValueError, match="No script selected"):
        runtime.pick_and_open_launcher_workspace()

    not_script = tmp_path / "not-script.txt"
    not_script.write_text("", encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        runtime.pick_launcher_config_path(str(not_script))

    script_path = tmp_path / "train.py"
    script_path.write_text("print('train')\n", encoding="utf-8")
    monkeypatch.setattr("pyruns.web.runtime.choose_config_file", lambda initial_dir: "")
    with pytest.raises(ValueError, match="No YAML config selected"):
        runtime.pick_launcher_config_path(str(script_path))

    bad_config = tmp_path / "bad.txt"
    bad_config.write_text("", encoding="utf-8")
    monkeypatch.setattr("pyruns.web.runtime.choose_config_file", lambda initial_dir: str(bad_config))
    with pytest.raises(FileNotFoundError):
        runtime.pick_launcher_config_path(str(script_path))

    with pytest.raises(ValueError, match="Shell folder does not exist"):
        runtime.open_shell_workspace_at(str(tmp_path / "missing-dir"))
