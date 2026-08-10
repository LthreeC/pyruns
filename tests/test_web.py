import json
import ast
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import psutil
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pyruns import __version__
from pyruns._config import (
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_TASK_SUMMARY_SEARCH_TEXT_CHARS,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
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
from pyruns.core.task_manager import TaskManager
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


def create_app(runtime=None, **kwargs):
    """Create an app with the ASGI test-client bypass enabled explicitly."""

    kwargs.setdefault("allow_test_client_bypass", True)
    return _create_app(runtime, **kwargs)


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


def test_log_websocket_rejects_cross_origin_browser_clients():
    client = TestClient(create_app(_RouteRuntime()))

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/tasks/alpha/logs/stream",
            headers={"Origin": "https://attacker.example", "Host": "testserver"},
        ):
            pass

    assert exc_info.value.code == 4403


def test_task_event_websocket_rejects_cross_origin_browser_clients():
    client = TestClient(create_app(_RouteRuntime()))

    with pytest.raises(WebSocketDisconnect) as exc_info:
        with client.websocket_connect(
            "/api/tasks/events",
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
    client = TestClient(web_app.create_app(_RouteRuntime()))

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

    monkeypatch.setattr(web_app.uvicorn, "run", fake_run)

    web_app.main(reload=True, open_browser=False, access_token="reload-secret")

    assert captured["app_target"] == "pyruns.web.app:create_app"
    assert captured["token"] == "reload-secret"
    assert re.fullmatch(r"[0-9a-f]{32}", captured["cookie_nonce"])
    assert captured["cookie_nonce"] == web_app._session_cookie_nonce_for_port(8099)
    assert web_app._UI_TOKEN_ENV not in web_app.os.environ
    assert web_app._UI_COOKIE_NONCE_ENV not in web_app.os.environ


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


def test_launcher_open_endpoint_rejects_non_python_script_path(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    not_script = tmp_path / "notes.txt"
    not_script.write_text("not a script\n", encoding="utf-8")

    response = client.post("/api/launcher/open", json={"script_path": str(not_script)})

    assert response.status_code == 400
    assert "Python script" in response.json()["detail"]


def test_launcher_open_endpoint_rejects_directory_script_path(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))
    directory_path = tmp_path / "configs"
    directory_path.mkdir()

    response = client.post("/api/launcher/open", json={"script_path": str(directory_path)})

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


def test_workspace_switch_waits_for_in_flight_task_start(tmp_path, monkeypatch):
    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    _add_task(workspace_a, "same-name")
    _add_task(workspace_b, "same-name")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    switched = threading.Event()
    started_in: list[str] = []
    errors: list[BaseException] = []
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

    def start_task():
        try:
            runtime.start_task("same-name")
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            switched.set()

    monkeypatch.setattr(runtime, "require_task", blocked_require_task)
    monkeypatch.setattr(TaskManager, "start_task_now", record_start)
    start_thread = threading.Thread(target=start_task)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        start_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert not switched.wait(0.5), "workspace changed while task start was in flight"
    finally:
        release.set()
        start_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not start_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []
    assert started_in == [str(workspace_a)]


def test_workspace_switch_waits_for_in_flight_runtime_update(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_module

    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    switched = threading.Event()
    saved_batches: list[tuple[str, dict[str, object]]] = []
    errors: list[BaseException] = []

    def blocked_save(root, values):
        saved_batches.append((str(Path(root)), dict(values)))
        entered.set()
        if not release.wait(5):
            raise TimeoutError("runtime update test did not release")

    def update_runtime():
        try:
            runtime.update_runtime_settings({
                "python_executable": "python-a",
                "conda_env": "env-a",
            })
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            switched.set()

    monkeypatch.setattr(runtime_module, "save_settings_for_root", blocked_save)
    monkeypatch.setattr(runtime_module, "load_settings", lambda root: {})
    monkeypatch.setattr(runtime, "get_runtime_info", lambda refresh_providers=False: {"ok": True})
    update_thread = threading.Thread(target=update_runtime)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        update_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert not switched.wait(0.5), "workspace changed while runtime settings were being saved"
    finally:
        release.set()
        update_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not update_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []
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
    switched = threading.Event()
    added_in: list[str] = []
    errors: list[BaseException] = []

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

    def create_task():
        try:
            runtime.create_tasks_from_template(
                name_prefix="created",
                mode="yaml",
                yaml_text="lr: 0.1\n",
                append_timestamp=False,
            )
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            switched.set()

    monkeypatch.setattr(generator, "create_tasks", blocked_create)
    monkeypatch.setattr(TaskManager, "add_tasks", record_add)
    create_thread = threading.Thread(target=create_task)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        create_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert not switched.wait(0.5), "workspace changed while tasks were being created"
    finally:
        release.set()
        create_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not create_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []
    assert added_in == [str(workspace_a)]


def test_workspace_switch_waits_for_in_flight_workspace_read(tmp_path, monkeypatch):
    from pyruns.web import runtime as runtime_module

    workspace_a = _make_workspace(tmp_path, "main")
    workspace_b = _make_workspace(tmp_path, "alt")
    runtime = _build_runtime(workspace_a, owns_task_lifecycle=False)
    entered = threading.Event()
    release = threading.Event()
    switched = threading.Event()
    responses: list[dict] = []
    errors: list[BaseException] = []
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

    def read_workspace():
        try:
            responses.append(runtime.get_workspace_info())
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            switched.set()

    monkeypatch.setattr(runtime_module, "load_script_info", blocked_load_script_info)
    read_thread = threading.Thread(target=read_workspace)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        read_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert not switched.wait(0.5), "workspace changed while metadata was being assembled"
    finally:
        release.set()
        read_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not read_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []
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
    switched = threading.Event()
    responses: list[dict] = []
    errors: list[BaseException] = []
    original_get_log_options = runtime_module.get_log_options

    def blocked_get_log_options(task_dir):
        entered.set()
        if not release.wait(5):
            raise TimeoutError("log read test did not release")
        return original_get_log_options(task_dir)

    def read_log():
        try:
            responses.append(runtime.get_task_logs("same-name", tail_lines=20))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)

    def switch_workspace():
        try:
            runtime.change_run_root(str(workspace_b))
        except BaseException as exc:  # pragma: no cover - surfaced by assertion below
            errors.append(exc)
        finally:
            switched.set()

    monkeypatch.setattr(runtime_module, "get_log_options", blocked_get_log_options)
    read_thread = threading.Thread(target=read_log)
    switch_thread = threading.Thread(target=switch_workspace)
    try:
        read_thread.start()
        assert entered.wait(2)
        switch_thread.start()
        assert not switched.wait(0.5), "workspace changed while a log response was being assembled"
    finally:
        release.set()
        read_thread.join(timeout=5)
        switch_thread.join(timeout=5)
        runtime.shutdown()

    assert not read_thread.is_alive()
    assert not switch_thread.is_alive()
    assert errors == []
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


def test_notes_endpoint_rejects_stale_write_without_overwriting_disk(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    first = client.patch(
        "/api/tasks/alpha/notes",
        json={"notes": "first", "expected_notes": ""},
    )
    stale = client.patch(
        "/api/tasks/alpha/notes",
        json={"notes": "stale", "expected_notes": ""},
    )
    blind = client.patch(
        "/api/tasks/alpha/notes",
        json={"notes": "blind"},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]
    assert blind.status_code == 422
    task_dir = workspace / TASKS_DIR / "alpha"
    assert load_task_info(str(task_dir))["notes"] == "first"


def test_env_endpoint_rejects_stale_write_without_overwriting_disk(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    first = client.patch(
        "/api/tasks/alpha/env",
        json={"env": {"FIRST": "1"}, "expected_env": {}},
    )
    stale = client.patch(
        "/api/tasks/alpha/env",
        json={"env": {"STALE": "1"}, "expected_env": {}},
    )
    blind = client.patch(
        "/api/tasks/alpha/env",
        json={"env": {"BLIND": "1"}},
    )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert "changed" in stale.json()["detail"]
    assert blind.status_code == 422
    task_dir = workspace / TASKS_DIR / "alpha"
    assert load_task_info(str(task_dir))["env"] == {"FIRST": "1"}


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
    listed = client.get("/api/tasks", params={"limit": 10_000, "refresh": True}).json()["items"]
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
    runtime.metrics_sampler.sample = lambda: {"cpu_percent": 10.0, "mem_percent": 20.0, "gpus": []}
    client = TestClient(create_app(runtime))

    response = client.get("/api/system/metrics")

    assert response.status_code == 200
    assert response.json()["cpu_percent"] == 10.0


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
