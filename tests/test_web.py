import json
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
    assert payload["script_name"] == "main"
    assert payload["workspace_kind"] == WORKSPACE_KIND_SCRIPT
    assert payload["settings"]["shell_mode"] == "follow"
    assert payload["settings"]["monitor_sidebar_width_pct"] == 24
    assert payload["shell_runtime"]["mode"] == "follow"
    assert payload["shell_runtime"]["display_name"] == "PowerShell"
    assert payload["templates"]
    assert payload["workspace_ready"] is True


def test_root_serves_react_frontend_shell(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    response = client.get("/")

    assert response.status_code == 200
    assert "<title>Pyruns</title>" in response.text
    assert '<div id="root"></div>' in response.text
    assert "assets/" in response.text


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


def test_shell_workspace_endpoint_and_generator_shell_mode(tmp_path):
    workspace = _make_workspace(tmp_path, "main")
    runtime = _build_runtime(workspace)
    client = TestClient(create_app(runtime))

    shell_response = client.post("/api/workspace/shell")

    assert shell_response.status_code == 200
    shell_payload = shell_response.json()
    assert shell_payload["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert shell_payload["script_name"] == "_shell_"
    assert shell_payload["templates"] == []

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
