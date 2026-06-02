import json
import socket
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from pyruns._config import (
    CONFIG_FILENAME,
    SHELL_CONFIG_FILENAME,
    TASKS_DIR,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    WORKSPACE_KIND_SCRIPT,
    WORKSPACE_KIND_SHELL,
)
from pyruns.core.task_manager import TaskManager
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.events import log_emitter
from pyruns.utils.info_io import save_task_info, update_task_info
from pyruns.web.app import create_app
from pyruns.web.runtime import PyrunsRuntime


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


def _build_runtime(workspace: Path) -> PyrunsRuntime:
    def make_task_manager(tasks_dir: str) -> TaskManager:
        with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
            return TaskManager(tasks_dir=tasks_dir, lazy_scan=False)

    return PyrunsRuntime(root_dir=str(workspace), task_manager_factory=make_task_manager)


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

    web_app.main(open_browser=True, start_path="/generator?launcher=1")

    assert captured["port"] == 8101
    assert captured["browser_url"] == "http://127.0.0.1:8101/generator?launcher=1"


def test_main_explicit_port_overrides_workspace_setting(monkeypatch):
    from pyruns.web import app as web_app

    captured: dict[str, object] = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    def fake_find_available_port(port, host="127.0.0.1"):
        captured["requested_port"] = port
        captured["host"] = host
        return port

    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(web_app, "find_available_port", fake_find_available_port)
    monkeypatch.setattr(web_app, "_schedule_browser_open", lambda url: captured.update(browser_url=url))
    monkeypatch.setattr(web_app.uvicorn, "run", lambda app_target, **kwargs: captured.update(kwargs))

    web_app.main(open_browser=True, port=9022)

    assert captured["requested_port"] == 9022
    assert captured["port"] == 9022
    assert captured["browser_url"] == "http://127.0.0.1:9022/"


def test_main_does_not_auto_open_browser_in_tmux(monkeypatch):
    from pyruns.web import app as web_app

    captured: dict[str, object] = {}

    class DummyRuntime:
        settings = {"ui_port": 8099}

    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")
    monkeypatch.delenv("PYRUNS_OPEN_BROWSER", raising=False)
    monkeypatch.delenv("PYRUNS_NO_BROWSER", raising=False)
    monkeypatch.setattr(web_app, "PyrunsRuntime", lambda: DummyRuntime())
    monkeypatch.setattr(web_app, "find_available_port", lambda port, host="127.0.0.1": port)
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

    web_app.main(open_browser=True)

    assert captured["browser_url"] == "http://127.0.0.1:8099/"


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


def test_workspace_endpoint_reports_native_picker_capability(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with patch("pyruns.web.runtime.native_picker_available", return_value=False):
        response = client.get("/api/workspace")

    assert response.status_code == 200
    assert response.json()["native_file_picker"] is False


def test_root_serves_react_frontend_shell(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Pyruns</title>" in response.text
    assert '<div id="root"></div>' in response.text
    assert "assets/" in response.text


def test_root_serves_fallback_frontend_when_static_bundle_is_missing(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)

    with patch("pyruns.web.app._frontend_dist_dir", return_value=None):
        client = TestClient(create_app(runtime))
        response = client.get("/")

    assert response.status_code == 200
    assert "Pyruns API server is running" in response.text


def test_dashboard_endpoint_returns_summary_and_recent_tasks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    _add_task(workspace, "beta", status="failed")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/api/dashboard")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["total"] == 2
    assert payload["summary"]["running"] == 1
    assert payload["summary"]["failed"] == 1
    assert payload["recent_tasks"][0]["name"] in {"alpha", "beta"}


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

    def fake_start(task_name: str, execution_mode: str | None = None) -> None:
        task_dir = workspace / TASKS_DIR / task_name

        def apply(info):
            info["status"] = "running"

        update_task_info(str(task_dir), apply)

    def fake_cancel(task_name: str) -> bool:
        task_dir = workspace / TASKS_DIR / task_name

        def apply(info):
            info["status"] = "failed"

        update_task_info(str(task_dir), apply)
        return True

    with patch.object(runtime.task_manager, "start_task_now", side_effect=fake_start):
        run_response = client.post("/api/tasks/alpha/run", json={})
    with patch.object(runtime.task_manager, "cancel_task", side_effect=fake_cancel):
        cancel_response = client.post("/api/tasks/alpha/cancel")

    assert run_response.status_code == 200
    assert run_response.json()["task"]["status"] == "running"
    assert cancel_response.status_code == 200
    assert cancel_response.json()["task"]["status"] == "failed"


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
    assert {item["name"] for item in payload["items"]} == {"demo_[1-of-2]", "demo_[2-of-2]"}


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
    assert payload["items"][0]["name"] == "range-demo_[1-of-10]"
    assert payload["items"][-1]["name"] == "range-demo_[10-of-10]"


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
    assert shell_payload["templates"] == [
        {"value": str(shell_file).replace("\\", "/"), "label": "run_smoke.sh"}
    ]

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
    assert {"value": str(shell_file).replace("\\", "/"), "label": "run_smoke.sh"} in items

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

    def fake_start_batch(task_names, execution_mode=None, max_workers=None):
        for task_name in task_names:
            task_dir = workspace / TASKS_DIR / task_name

            def apply(info):
                info["status"] = "queued"

            update_task_info(str(task_dir), apply)

    with patch.object(runtime.task_manager, "start_batch_tasks", side_effect=fake_start_batch):
        run_response = client.post("/api/tasks/batch/run", json={"task_names": ["alpha", "beta"]})

    delete_response = client.post("/api/tasks/batch/delete", json={"task_names": ["alpha"]})

    assert run_response.status_code == 200
    assert run_response.json()["count"] == 2
    assert {item["status"] for item in run_response.json()["items"]} == {"queued"}
    assert delete_response.status_code == 200
    assert delete_response.json()["deleted"] == ["alpha"]


def test_pin_notes_env_and_rename_endpoints(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    pin_response = client.post("/api/tasks/alpha/pin", json={"pinned": True})
    notes_response = client.patch("/api/tasks/alpha/notes", json={"notes": "needs review"})
    env_response = client.patch("/api/tasks/alpha/env", json={"env": {"CUDA_VISIBLE_DEVICES": "0"}})
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
    listed = client.get("/api/tasks", params={"limit": 0, "refresh": True}).json()["items"]
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

    response = client.get("/api/tasks", params={"limit": 0, "refresh": True})

    assert response.status_code == 200
    names = [item["name"] for item in response.json()["items"]]
    assert names[:4] == ["running-old", "new-pending", "completed-old", "pending-old"]


def test_logs_websocket_streams_live_chunks(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    _add_task(workspace, "alpha", status="running")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    with client.websocket_connect("/api/tasks/alpha/logs/stream") as websocket:
        log_emitter.emit("alpha", "hello from stream")
        payload = websocket.receive_json()

    assert payload["type"] == "chunk"
    assert payload["task_name"] == "alpha"
    assert payload["content"] == "hello from stream"


def test_metrics_endpoint_returns_sampler_payload(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    runtime.metrics_sampler.sample = lambda: {"cpu_percent": 10.0, "mem_percent": 20.0, "gpus": []}
    client = TestClient(create_app(runtime))

    response = client.get("/api/system/metrics")

    assert response.status_code == 200
    assert response.json()["cpu_percent"] == 10.0
