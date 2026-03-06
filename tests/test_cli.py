"""
Tests for pyruns.cli — display, commands, interactive, and workspace resolution.
"""
import json

import pytest
from unittest.mock import patch

from pyruns._config import CONFIG_FILENAME, TASKS_DIR
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
