"""
Tests for pyruns.cli — display, commands, interactive, and workspace resolution.
"""
import json

import pytest
from unittest.mock import patch

from pyruns._config import CONFIG_DEFAULT_FILENAME, CONFIG_FILENAME, TASKS_DIR
from pyruns.launcher import bootstrap_workspace
from pyruns.utils.info_io import save_task_info
from pyruns.utils.config_utils import save_yaml


# ═══════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def workspace(tmp_path):
    """Create a minimal pyruns workspace with tasks."""
    # _pyruns_/main/
    ws = tmp_path / "_pyruns_" / "main"
    ws.mkdir(parents=True)

    # script_info.json
    (ws / "script_info.json").write_text(json.dumps({
        "script_name": "main",
        "script_path": str(tmp_path / "main.py"),
    }))

    # config_default.yaml
    (ws / "config_default.yaml").write_text("lr: 0.01\nepochs: 10\n")

    # tasks/
    tasks_dir = ws / TASKS_DIR
    tasks_dir.mkdir()

    return ws


def _add_task(workspace, name, status="pending", config=None, records=None,
              start_times=None, finish_times=None):
    """Helper to create a task on disk."""
    task_dir = workspace / TASKS_DIR / name
    task_dir.mkdir(parents=True, exist_ok=True)

    info = {
        "name": name,
        "status": status,
        "progress": 1.0 if status == "completed" else 0.0,
        "created_at": "2026-03-04_22-00-00",
        "start_times": start_times or [],
        "finish_times": finish_times or [],
        "pids": [],
        "records": records or [],
        "tracks": [],
    }
    save_task_info(str(task_dir), info)
    save_yaml(str(task_dir / CONFIG_FILENAME), config or {"lr": 0.01})

    # run_logs dir
    (task_dir / "run_logs").mkdir(exist_ok=True)

    return {
        "dir": str(task_dir),
        "name": name,
        "status": status,
        "created_at": info["created_at"],
        "config": config or {"lr": 0.01},
        "start_times": info["start_times"],
        "finish_times": info["finish_times"],
        "pids": [],
        "records": len(info["records"]),
        "pinned": False,
        "env": {},
        "log": "",
        "progress": info["progress"],
    }


@pytest.fixture
def task_manager(workspace):
    """Create a TaskManager pointed at the workspace."""
    from pyruns.core.task_manager import TaskManager
    tasks_dir = str(workspace / TASKS_DIR)
    with patch.object(TaskManager, "scan_disk_async", lambda self: self.scan_disk()):
        with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
            tm = TaskManager(tasks_dir=tasks_dir)
    tm.scan_disk()
    tm.refresh_from_disk(force_all=True)
    return tm


def test_task_manager_scan_uses_folder_name_as_task_name(workspace, task_manager):
    _add_task(workspace, "stable-name-task")
    task_manager.scan_disk()
    task = next(t for t in task_manager.tasks if t["name"] == "stable-name-task")
    assert task["name"] == "stable-name-task"


def test_task_manager_rename_task_moves_folder(workspace, task_manager):
    _add_task(workspace, "before-rename")
    task_manager.scan_disk()
    ok, result = task_manager.rename_task("before-rename", "after-rename")
    assert ok is True
    assert result == "after-rename"
    assert (workspace / TASKS_DIR / "after-rename").exists()
    assert not (workspace / TASKS_DIR / "before-rename").exists()


def test_task_manager_rename_task_rejects_conflict(workspace, task_manager):
    _add_task(workspace, "task-a")
    _add_task(workspace, "task-b")
    task_manager.scan_disk()
    ok, message = task_manager.rename_task("task-a", "task-b")
    assert ok is False
    assert "already exists" in message


def test_task_manager_rename_task_rejects_running(workspace, task_manager):
    _add_task(workspace, "runner", status="queued")
    task_manager.scan_disk()
    ok, message = task_manager.rename_task("runner", "runner-renamed")
    assert ok is False
    assert "cannot be renamed" in message


def test_task_manager_update_notes_updates_search_cache(workspace, task_manager):
    _add_task(workspace, "notes-task")
    task_manager.scan_disk()
    ok, notes = task_manager.update_task_notes("notes-task", "best checkpoint")
    assert ok is True
    assert notes == "best checkpoint"
    task = next(t for t in task_manager.tasks if t["name"] == "notes-task")
    assert task["notes"] == "best checkpoint"
    assert "best checkpoint" in task["search_text"]


def test_task_manager_update_env_updates_memory_and_disk(workspace, task_manager):
    _add_task(workspace, "env-task")
    task_manager.scan_disk()
    ok, env = task_manager.update_task_env("env-task", {"CUDA_VISIBLE_DEVICES": "0"})
    assert ok is True
    assert env == {"CUDA_VISIBLE_DEVICES": "0"}
    task = next(t for t in task_manager.tasks if t["name"] == "env-task")
    assert task["env"] == {"CUDA_VISIBLE_DEVICES": "0"}
    info = json.loads((workspace / TASKS_DIR / "env-task" / "task_info.json").read_text(encoding="utf-8"))
    assert info["env"] == {"CUDA_VISIBLE_DEVICES": "0"}


def test_task_manager_set_task_pinned_updates_memory_and_disk(workspace, task_manager):
    _add_task(workspace, "pin-task")
    task_manager.scan_disk()
    ok, pinned = task_manager.set_task_pinned("pin-task", True)
    assert ok is True
    assert pinned is True
    task = next(t for t in task_manager.tasks if t["name"] == "pin-task")
    assert task["pinned"] is True
    info = json.loads((workspace / TASKS_DIR / "pin-task" / "task_info.json").read_text(encoding="utf-8"))
    assert info["pinned"] is True


def test_task_manager_scan_ignores_task_without_config(workspace, task_manager):
    bad_dir = workspace / TASKS_DIR / "broken-task"
    bad_dir.mkdir(parents=True, exist_ok=True)
    save_task_info(str(bad_dir), {"name": "broken-task", "status": "pending"})
    task_manager.scan_disk()
    task = next(t for t in task_manager.tasks if t["name"] == "broken-task")
    assert task["_load_error"] == "config.yaml is missing"


def test_task_manager_scan_keeps_task_with_invalid_config(workspace, task_manager):
    bad_dir = workspace / TASKS_DIR / "bad-config-task"
    bad_dir.mkdir(parents=True, exist_ok=True)
    save_task_info(str(bad_dir), {"name": "bad-config-task", "status": "pending"})
    (bad_dir / CONFIG_FILENAME).write_text("a: [1, 2\n", encoding="utf-8")
    task_manager.scan_disk()
    task = next(t for t in task_manager.tasks if t["name"] == "bad-config-task")
    assert task["config"] == {}
    assert task["_load_error"]


# ═══════════════════════════════════════════════════════════════
#  Display tests
# ═══════════════════════════════════════════════════════════════


class TestDisplay:
    def test_print_task_table_empty(self, capsys):
        from pyruns.cli.display import print_task_table
        print_task_table([])
        captured = capsys.readouterr()
        assert "No tasks" in captured.out

    def test_print_task_table_with_tasks(self, capsys):
        from pyruns.cli.display import print_task_table
        tasks = [
            {"name": "exp1", "status": "running", "created_at": "2026-03-04"},
            {"name": "exp2", "status": "completed", "created_at": "2026-03-03"},
        ]
        print_task_table(tasks)
        captured = capsys.readouterr()
        assert "exp1" in captured.out
        assert "exp2" in captured.out
        assert "2 total" in captured.out

    def test_print_jobs_no_active(self, capsys):
        from pyruns.cli.display import print_jobs
        tasks = [{"name": "done", "status": "completed"}]
        print_jobs(tasks)
        captured = capsys.readouterr()
        assert "No active jobs" in captured.out

    def test_print_jobs_with_running(self, capsys):
        from pyruns.cli.display import print_jobs
        tasks = [
            {"name": "runner", "status": "running"},
            {"name": "waiter", "status": "queued"},
        ]
        print_jobs(tasks)
        captured = capsys.readouterr()
        assert "[1]" in captured.out
        assert "runner" in captured.out
        assert "[2]" in captured.out
        assert "waiter" in captured.out


# ═══════════════════════════════════════════════════════════════
#  Commands tests
# ═══════════════════════════════════════════════════════════════


class TestCmdList:
    def test_list_empty(self, task_manager, capsys):
        from pyruns.cli.commands import cmd_list
        cmd_list(task_manager)
        captured = capsys.readouterr()
        assert "No tasks" in captured.out

    def test_list_with_tasks(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_list
        _add_task(workspace, "task-a", "completed")
        _add_task(workspace, "task-b", "running")
        task_manager.scan_disk()

        cmd_list(task_manager)
        captured = capsys.readouterr()
        assert "task-a" in captured.out
        assert "task-b" in captured.out

    def test_list_with_filter(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_list
        _add_task(workspace, "exp-lr-001", "completed")
        _add_task(workspace, "exp-lr-01", "completed")
        _add_task(workspace, "baseline", "pending")
        task_manager.scan_disk()

        cmd_list(task_manager, ["exp-lr"])
        captured = capsys.readouterr()
        assert "exp-lr-001" in captured.out
        assert "exp-lr-01" in captured.out
        # baseline should be filtered out since filter matches name
        # (filter_tasks does substring search on name + config)

    def test_list_with_status_and_limit(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_list

        _add_task(workspace, "done-a", "completed")
        _add_task(workspace, "done-b", "completed")
        _add_task(workspace, "queued-a", "queued")
        task_manager.scan_disk()

        cmd_list(task_manager, ["--status", "completed", "--limit", "1"])
        captured = capsys.readouterr()
        assert "queued-a" not in captured.out
        assert "1 total" in captured.out


class TestCmdJobs:
    def test_jobs_empty(self, task_manager, capsys):
        from pyruns.cli.commands import cmd_jobs
        cmd_jobs(task_manager)
        captured = capsys.readouterr()
        assert "No active jobs" in captured.out

    def test_jobs_with_active(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_jobs
        # Use "queued" instead of "running" — _load_task_dir marks orphan
        # "running" tasks (no live PID) as "failed" during scan_disk.
        _add_task(workspace, "runner", "queued")
        task_manager.scan_disk()

        cmd_jobs(task_manager)
        captured = capsys.readouterr()
        assert "runner" in captured.out
        assert "[1]" in captured.out


class TestCmdRun:
    def test_run_no_args(self, task_manager, capsys):
        from pyruns.cli.commands import cmd_run
        cmd_run(task_manager)
        captured = capsys.readouterr()
        assert "Usage" in captured.out

    def test_run_by_index(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_run
        _add_task(workspace, "my-task", "pending")
        task_manager.scan_disk()

        with patch.object(task_manager, "start_task_now") as mock_start:
            with patch("pyruns.cli.commands.cmd_fg"):
                cmd_run(task_manager, ["1"])
            mock_start.assert_called_once_with("my-task")

    def test_run_by_name(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_run
        _add_task(workspace, "target-task", "pending")
        task_manager.scan_disk()

        with patch.object(task_manager, "start_task_now") as mock_start:
            with patch("pyruns.cli.commands.cmd_fg"):
                cmd_run(task_manager, ["target-task"])
            mock_start.assert_called_once_with("target-task")

    def test_run_single_detach_skips_fg(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_run

        _add_task(workspace, "detach-task", "pending")
        task_manager.scan_disk()

        with patch.object(task_manager, "start_task_now") as mock_start:
            with patch("pyruns.cli.commands.cmd_fg") as mock_fg:
                cmd_run(task_manager, ["detach-task", "--detach"])
            mock_start.assert_called_once_with("detach-task")
            mock_fg.assert_not_called()

    def test_run_batch_with_flags(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_run

        _add_task(workspace, "batch-a", "pending")
        _add_task(workspace, "batch-b", "pending")
        task_manager.scan_disk()

        with patch.object(task_manager, "start_batch_tasks") as mock_batch:
            cmd_run(task_manager, ["batch-a", "batch-b", "--workers", "3", "--mode", "process", "--detach"])
            mock_batch.assert_called_once_with(["batch-a", "batch-b"], execution_mode="process", max_workers=3)


class TestCmdDelete:
    def test_delete_no_args(self, task_manager, capsys):
        from pyruns.cli.commands import cmd_delete
        cmd_delete(task_manager)
        captured = capsys.readouterr()
        assert "Usage" in captured.out

    def test_delete_confirms(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_delete
        _add_task(workspace, "to-delete", "completed")
        task_manager.scan_disk()

        with patch("builtins.input", return_value="y"):
            with patch.object(task_manager, "delete_tasks") as mock_del:
                cmd_delete(task_manager, ["to-delete"])
                mock_del.assert_called_once_with(["to-delete"])

    def test_delete_cancels(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_delete
        _add_task(workspace, "keep-me", "completed")
        task_manager.scan_disk()

        with patch("builtins.input", return_value="n"):
            with patch.object(task_manager, "delete_tasks") as mock_del:
                cmd_delete(task_manager, ["keep-me"])
                mock_del.assert_not_called()
        captured = capsys.readouterr()
        assert "Cancelled" in captured.out

    def test_delete_yes_skips_prompt(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_delete

        _add_task(workspace, "fast-delete", "completed")
        task_manager.scan_disk()

        with patch("builtins.input") as mock_input:
            with patch.object(task_manager, "delete_tasks") as mock_del:
                cmd_delete(task_manager, ["fast-delete", "--yes"])
            mock_del.assert_called_once_with(["fast-delete"])
            mock_input.assert_not_called()


class TestCmdShow:
    def test_show_prints_task_detail(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_show

        _add_task(workspace, "detail-task", "completed", config={"lr": 0.02, "epochs": 10})
        task_manager.scan_disk()

        cmd_show(task_manager, ["detail-task"])
        captured = capsys.readouterr()
        assert "detail-task" in captured.out
        assert "Status:" in captured.out


class TestCmdExport:
    def test_export_csv_to_explicit_path(self, workspace, task_manager, tmp_path):
        from pyruns.cli.commands import cmd_export

        _add_task(workspace, "export-task", "completed")
        task_manager.scan_disk()
        output_path = tmp_path / "tasks.csv"

        cmd_export(task_manager, ["export-task", "--output", str(output_path)])

        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")
        assert "name,status,run,start_time,finish_time,pid" in content
        assert "export-task" in content

    def test_export_status_filter_limits_output(self, workspace, task_manager, tmp_path):
        from pyruns.cli.commands import cmd_export

        _add_task(workspace, "export-completed", "completed")
        _add_task(workspace, "export-pending", "pending")
        task_manager.scan_disk()
        output_path = tmp_path / "filtered.csv"

        cmd_export(task_manager, ["--all", "--status", "completed", "--output", str(output_path)])

        assert output_path.exists()
        content = output_path.read_text(encoding="utf-8")
        assert "export-completed" in content
        assert "export-pending" not in content




# ═══════════════════════════════════════════════════════════════
#  Workspace resolution
# ═══════════════════════════════════════════════════════════════


class TestResolveWorkspace:
    def test_finds_workspace(self, workspace, tmp_path):
        from pyruns.cli import _resolve_workspace
        with patch("os.getcwd", return_value=str(tmp_path)):
            ws = _resolve_workspace()
            assert ws is not None
            assert "main" in ws

    def test_no_workspace(self, tmp_path):
        from pyruns.cli import _resolve_workspace
        with patch("os.getcwd", return_value=str(tmp_path)):
            ws = _resolve_workspace()
            assert ws is None


class TestEntryPoint:
    def test_pyr_without_args_opens_shell_workspace(self, tmp_path, monkeypatch, capsys):
        from pyruns.cli import pyr

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["pyr"])

        with patch("pyruns.cli.bootstrap_shell_workspace", return_value=str(tmp_path / "_pyruns_" / "_shell_")) as mock_bootstrap:
            with patch("pyruns.cli._launch_ui") as mock_launch:
                pyr()

        mock_bootstrap.assert_called_once_with(str(tmp_path / "_pyruns_").replace("\\", "/"))
        mock_launch.assert_called_once_with("/generator")
        captured = capsys.readouterr()
        assert "Starting shell workspace" in captured.out
        assert "Generator" in captured.out

    def test_help_mentions_shell_start(self, monkeypatch, capsys):
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "help"])

        with pytest.raises(SystemExit):
            pyr()

        captured = capsys.readouterr()
        assert "Start web app in shell mode for current directory" in captured.out
        assert "pyr ui" in captured.out

    def test_direct_info_command_dispatches_to_cli(self, monkeypatch):
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "info"])

        with patch("pyruns.cli._dispatch_cli") as mock_dispatch:
            pyr()

        mock_dispatch.assert_called_once_with(["info"])


class TestScriptLaunchRules:
    def test_pyruns_load_requires_yaml_on_first_launch(self, tmp_path):
        script_path = tmp_path / "train.py"
        script_path.write_text(
            "import pyruns\ncfg = pyruns.load()\nprint(cfg)\n",
            encoding="utf-8",
        )

        with pytest.raises(FileNotFoundError, match="needs a YAML template on first launch"):
            bootstrap_workspace(str(script_path))

    def test_pyruns_load_reuses_workspace_default_yaml_after_first_launch(self, tmp_path):
        script_path = tmp_path / "train.py"
        script_path.write_text(
            "import pyruns\ncfg = pyruns.load()\nprint(cfg)\n",
            encoding="utf-8",
        )

        workspace = tmp_path / "_pyruns_" / "train"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / CONFIG_DEFAULT_FILENAME).write_text("lr: 0.01\n", encoding="utf-8")

        resolved = bootstrap_workspace(str(script_path))

        assert resolved.replace("\\", "/").endswith("_pyruns_/train")


# ═══════════════════════════════════════════════════════════════
#  Resolve targets helper
# ═══════════════════════════════════════════════════════════════


class TestResolveTargets:
    def test_by_index(self, workspace, task_manager):
        from pyruns.cli.commands import _resolve_targets
        _add_task(workspace, "first", "completed")
        _add_task(workspace, "second", "pending")
        task_manager.scan_disk()

        targets = _resolve_targets(task_manager, ["1"])
        assert len(targets) == 1

    def test_by_name(self, workspace, task_manager):
        from pyruns.cli.commands import _resolve_targets
        _add_task(workspace, "exact-name", "pending")
        task_manager.scan_disk()

        targets = _resolve_targets(task_manager, ["exact-name"])
        assert len(targets) == 1
        assert targets[0]["name"] == "exact-name"

    def test_not_found(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import _resolve_targets
        _add_task(workspace, "exists", "pending")
        task_manager.scan_disk()

        targets = _resolve_targets(task_manager, ["nonexistent"])
        assert len(targets) == 0
        captured = capsys.readouterr()
        assert "not found" in captured.out


# ═══════════════════════════════════════════════════════════════
#  Interactive REPL
# ═══════════════════════════════════════════════════════════════


class TestInteractive:
    def test_exit_command(self, task_manager, capsys):
        from pyruns.cli.interactive import run_interactive
        with patch("builtins.input", side_effect=["exit"]):
            run_interactive(task_manager)
        captured = capsys.readouterr()
        assert "Pyruns CLI" in captured.out

    def test_entry_shows_task_overview(self, workspace, task_manager, capsys):
        from pyruns.cli.interactive import run_interactive

        _add_task(workspace, "overview-task", "completed")
        task_manager.scan_disk()

        with patch("builtins.input", side_effect=["exit"]):
            run_interactive(task_manager)
        captured = capsys.readouterr()
        assert "overview-task" in captured.out

    def test_help_command(self, task_manager, capsys):
        from pyruns.cli.interactive import run_interactive
        with patch("builtins.input", side_effect=["help", "exit"]):
            run_interactive(task_manager)
        captured = capsys.readouterr()
        assert "Commands" in captured.out

    def test_unknown_command(self, task_manager, capsys):
        from pyruns.cli.interactive import run_interactive
        with patch("builtins.input", side_effect=["foobar", "exit"]):
            run_interactive(task_manager)
        captured = capsys.readouterr()
        assert "Unknown command" in captured.out

    def test_keyboard_interrupt(self, task_manager):
        from pyruns.cli.interactive import run_interactive
        # Should exit gracefully without raising
        try:
            with patch("builtins.input", side_effect=KeyboardInterrupt):
                run_interactive(task_manager)
        except KeyboardInterrupt:
            pytest.fail("KeyboardInterrupt was not caught by run_interactive")
