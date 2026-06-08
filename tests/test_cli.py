"""
Tests for pyruns.cli — display, commands, interactive, and workspace resolution.
"""
import json
import os
import sys

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


def test_cmd_generate_reports_oversized_batch_without_prompting(workspace, task_manager, capsys):
    from pyruns.cli.commands import cmd_generate

    with patch("pyruns.cli.commands._get_git_editor", return_value="editor"), \
            patch("pyruns.cli.commands.subprocess.run"), \
            patch("pyruns.cli.commands.load_yaml", return_value={"epochs": "0:1000000:1"}), \
            patch("builtins.input") as mock_input:
        cmd_generate(task_manager)

    output = capsys.readouterr().out
    assert "Batch expansion would create" in output
    mock_input.assert_not_called()
    assert list((workspace / TASKS_DIR).iterdir()) == []


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
    def test_display_helpers_and_task_detail_optional_fields(self, capsys, monkeypatch):
        from pyruns.cli import display

        assert "Completed(3)" in display._status_str("completed", runs=3)
        assert "? Mystery" in display._status_str("mystery")
        assert display._truncate("abcdef", 0) == ""
        assert display._truncate("abcdef", 2) == ".."
        assert display._truncate("abcdef", 5) == "ab..."

        def raise_terminal_size_error():
            raise OSError("no terminal")

        monkeypatch.setattr(display.shutil, "get_terminal_size", raise_terminal_size_error)
        assert display._get_terminal_width() == 80

        monkeypatch.setattr(display, "_get_terminal_width", lambda: 48)
        display.print_task_detail({
            "name": "detail-task",
            "status": "failed",
            "created_at": "2026-03-04_22-00-00",
            "dir": "C:/workspace/tasks/detail-task",
            "start_times": ["2026-03-04_22-01-00", "2026-03-04_22-02-00"],
            "finish_times": ["2026-03-04_22-03-00"],
            "pinned": True,
            "env": {"B": "2", "A": "1"},
            "notes": " keep this checkpoint ",
            "_load_error": "config.yaml is invalid",
            "config": {"lr": 0.01, "model": "vit"},
        })

        output = capsys.readouterr().out
        assert "detail-task" in output
        assert "Runs:       2" in output
        assert "Last start: 2026-03-04_22-02-00" in output
        assert "Last end:   2026-03-04_22-03-00" in output
        assert "Pinned:     yes" in output
        assert "A=1" in output and "B=2" in output
        assert "Notes:" in output and "keep this checkpoint" in output
        assert "Load error:" in output and "config.yaml is invalid" in output
        assert "Config:" in output and "lr=0.01" in output

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
    def test_sorted_tasks_matches_manager_order(self):
        from pyruns.cli.commands import _sorted_tasks

        class DummyTaskManager:
            tasks = [
                {
                    "name": "manual-completed",
                    "status": "completed",
                    "created_at": "2026-05-28_02-25-46",
                    "task_order": 0,
                },
                {
                    "name": "fresh-new",
                    "status": "pending",
                    "created_at": "2026-05-31_22-50-00",
                },
                {
                    "name": "pinned-fresh",
                    "status": "pending",
                    "created_at": "2026-05-31_22-55-00",
                    "pinned": True,
                },
            ]

        assert [task["name"] for task in _sorted_tasks(DummyTaskManager())] == [
            "pinned-fresh",
            "fresh-new",
            "manual-completed",
        ]

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


class TestCmdForegroundAndLog:
    def test_fg_prints_existing_log_and_stops_when_task_is_finished(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_fg

        _add_task(workspace, "finished-log", "completed", start_times=["2026-06-03_12-00-00"])
        log_path = workspace / TASKS_DIR / "finished-log" / "run_logs" / "run1.log"
        log_path.write_text("line 1\r\nline 2\n", encoding="utf-8")
        task_manager.scan_disk()

        cmd_fg(task_manager, ["finished-log"])

        captured = capsys.readouterr()
        assert "== finished-log ==" in captured.out
        assert "line 1" in captured.out
        assert "line 2" in captured.out
        assert "Task completed" in captured.out

    def test_log_delegates_to_interactive_log_viewer(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_log

        _add_task(workspace, "log-target", "completed")
        task_manager.scan_disk()

        with patch("pyruns.cli.interactive_ls._view_log") as mock_view_log:
            cmd_log(task_manager, ["log-target"])

        mock_view_log.assert_called_once()
        assert mock_view_log.call_args.args[0]["name"] == "log-target"


class TestCmdOpenStatInfo:
    def test_open_task_info_uses_configured_editor_without_wait_flag(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_open

        _add_task(workspace, "open-task", "completed")
        task_manager.scan_disk()

        with (
            patch("pyruns.cli.commands._get_git_editor", return_value="code --wait"),
            patch("pyruns.cli.commands.subprocess.Popen") as mock_popen,
        ):
            cmd_open(task_manager, ["open-task", "task"])

        command = mock_popen.call_args.args[0]
        assert command[0] == "code"
        assert command[-1].endswith("task_info.json")

    def test_open_reports_missing_config_file(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_open

        _add_task(workspace, "missing-config", "completed")
        (workspace / TASKS_DIR / "missing-config" / CONFIG_FILENAME).unlink()
        task_manager.scan_disk()

        cmd_open(task_manager, ["missing-config", "config"])

        captured = capsys.readouterr()
        assert "File not found" in captured.out

    def test_info_prints_workspace_and_status_counts(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_info

        _add_task(workspace, "done", "completed")
        _add_task(workspace, "todo", "pending")
        task_manager.scan_disk()

        cmd_info(task_manager)

        captured = capsys.readouterr()
        assert "Workspace Info" in captured.out
        assert "Script:" in captured.out
        assert "Tasks:" in captured.out
        assert "pending=1" in captured.out
        assert "completed=1" in captured.out

    def test_stat_once_prints_cpu_ram_and_gpu_metrics(self, capsys):
        from pyruns.cli import commands

        class DummyMemory:
            used = 512 * 1024 ** 2
            total = 1024 * 1024 ** 2

        class DummyMonitor:
            def sample(self):
                return {
                    "cpu_percent": 25,
                    "mem_percent": 50,
                    "gpus": [{"index": 0, "util": 40, "mem_used": 100, "mem_total": 200}],
                }

        with (
            patch.object(commands, "SystemMonitor", return_value=DummyMonitor()),
            patch.object(commands.psutil, "virtual_memory", return_value=DummyMemory()),
        ):
            commands.cmd_stat(None)

        captured = capsys.readouterr()
        assert "System Metrics" in captured.out
        assert "CPU:" in captured.out
        assert "RAM:" in captured.out
        assert "GPU 0:" in captured.out

    def test_stat_once_reports_no_gpus(self, capsys):
        from pyruns.cli import commands

        class DummyMemory:
            used = 128 * 1024 ** 2
            total = 1024 * 1024 ** 2

        class DummyMonitor:
            def sample(self):
                return {"cpu_percent": 5, "mem_percent": 12, "gpus": []}

        with (
            patch.object(commands, "SystemMonitor", return_value=DummyMonitor()),
            patch.object(commands.psutil, "virtual_memory", return_value=DummyMemory()),
        ):
            commands.cmd_stat(None)

        assert "No GPUs detected" in capsys.readouterr().out

    def test_stat_interactive_flag_delegates_to_live_view(self):
        from pyruns.cli import commands

        with patch.object(commands, "_stat_interactive") as mock_interactive:
            commands.cmd_stat(None, ["--interactive"])

        mock_interactive.assert_called_once_with()


class TestCliCommandHelpers:
    def test_consume_option_accepts_equals_and_separate_values(self):
        from pyruns.cli.commands import _consume_option

        value, remaining = _consume_option(["--limit=5", "query"], "--limit", "-n")
        assert value == "5"
        assert remaining == ["query"]

        value, remaining = _consume_option(["-n", "2", "left"], "--limit", "-n")
        assert value == "2"
        assert remaining == ["left"]

    def test_consume_option_reports_missing_value(self, capsys):
        from pyruns.cli.commands import _consume_option

        value, remaining = _consume_option(["task", "--output"], "--output", "-o")

        assert value is None
        assert remaining == ["task"]
        assert "Missing value" in capsys.readouterr().out

    def test_consume_multi_option_accepts_comma_values_and_missing_value(self, capsys):
        from pyruns.cli.commands import _consume_multi_option

        values, remaining = _consume_multi_option(
            ["--status=completed,failed", "-s", "running,queued", "task"],
            "--status",
            "-s",
        )
        assert values == ["completed", "failed", "running", "queued"]
        assert remaining == ["task"]

        values, remaining = _consume_multi_option(["task", "-s"], "--status", "-s")
        assert values == []
        assert remaining == ["task"]
        assert "Missing value" in capsys.readouterr().out

    def test_parse_helpers_fall_back_for_invalid_values(self, capsys):
        from pyruns.cli.commands import _normalize_mode, _normalize_status_filters, _parse_limit, _parse_workers

        assert _parse_limit("abc") is None
        assert _parse_limit("0") is None
        assert _parse_limit("3") == 3
        assert _parse_workers("bad") == 1
        assert _parse_workers("-1") == 1
        assert _parse_workers("4") == 4
        assert _normalize_mode("process") == "process"
        assert _normalize_mode("weird") == "thread"
        assert _normalize_status_filters(["completed", "unknown", "FAILED"]) == ["completed", "failed"]

        output = capsys.readouterr().out
        assert "Invalid limit" in output
        assert "Workers must be greater than 0" in output
        assert "Unknown mode" in output
        assert "Unknown status filter" in output

    def test_get_git_editor_uses_env_git_and_vscode_fallbacks(self, monkeypatch):
        from pyruns.cli import commands

        monkeypatch.setenv("GIT_EDITOR", "nano")
        assert commands._get_git_editor() == "nano"

        monkeypatch.delenv("GIT_EDITOR")
        monkeypatch.delenv("VISUAL", raising=False)
        monkeypatch.delenv("EDITOR", raising=False)
        with patch.object(commands.subprocess, "check_output", return_value="vim -f\n"):
            assert commands._get_git_editor() == "vim -f"

        with patch.object(commands.subprocess, "check_output", side_effect=RuntimeError):
            monkeypatch.setenv("VISUAL", "emacs")
            assert commands._get_git_editor() == "emacs"
            monkeypatch.delenv("VISUAL")
            monkeypatch.setenv("EDITOR", "micro")
            assert commands._get_git_editor() == "micro"
            monkeypatch.delenv("EDITOR")
            monkeypatch.setenv("TERM_PROGRAM", "vscode")
            monkeypatch.setenv("VSCODE_IPC_HOOK_CLI", "cursor-ipc")
            assert commands._get_git_editor() == "cursor --wait"

    def test_resolve_targets_reports_out_of_range_and_ambiguous_names(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import _resolve_targets

        _add_task(workspace, "alpha-one", "pending")
        _add_task(workspace, "alpha-two", "pending")
        task_manager.scan_disk()

        assert _resolve_targets(task_manager, ["99"]) == []
        assert _resolve_targets(task_manager, ["alpha"]) == []
        output = capsys.readouterr().out
        assert "Task index out of range" in output
        assert "Ambiguous name" in output

    def test_resolve_export_tasks_defaults_to_all_and_applies_status(self, workspace, task_manager):
        from pyruns.cli.commands import _resolve_export_tasks

        _add_task(workspace, "done", "completed")
        _add_task(workspace, "todo", "pending")
        task_manager.scan_disk()

        tasks = _resolve_export_tasks(task_manager, [], ["completed"], include_all=False)

        assert [task["name"] for task in tasks] == ["done"]

    def test_progress_bar_clamps_and_switches_warning_colors(self):
        from pyruns.cli.commands import _bar

        assert "100.0%" in _bar(120)
        assert "  0.0%" in _bar(-5)
        assert "\033[33m" in _bar(70)
        assert "\033[31m" in _bar(90)

    def test_stat_interactive_renders_once_then_exits_on_keyboard_interrupt(self, capsys):
        from pyruns.cli import commands

        class DummyMemory:
            used = 256 * 1024 ** 2
            total = 1024 * 1024 ** 2

        class DummyMonitor:
            def sample(self):
                return {
                    "cpu_percent": 85,
                    "mem_percent": 25,
                    "gpus": [{"index": 1, "util": 90, "mem_used": 300, "mem_total": 600}],
                }

        with (
            patch.object(commands, "SystemMonitor", return_value=DummyMonitor()),
            patch.object(commands.psutil, "virtual_memory", return_value=DummyMemory()),
            patch.object(commands.time, "sleep", side_effect=KeyboardInterrupt),
        ):
            commands._stat_interactive()

        output = capsys.readouterr().out
        assert "System Metrics" in output
        assert "GPU 1:" in output
        assert "Exited stat view" in output

    def test_generate_creates_tasks_from_edited_config(self, workspace, task_manager, tmp_path, capsys):
        from pyruns.cli import commands

        template = tmp_path / "template.yaml"
        template.write_text("lr: 0.1\n", encoding="utf-8")

        class DummyGenerator:
            def __init__(self, root_dir):
                self.root_dir = root_dir

            def create_tasks(self, configs, name_prefix, task_kind=None):
                assert configs == [{"lr": 0.1}, {"lr": 0.2}]
                assert name_prefix == "batch"
                return [
                    {"name": "batch-1", "dir": str(tmp_path / "batch-1")},
                    {"name": "batch-2", "dir": str(tmp_path / "batch-2")},
                ]

        with (
            patch.object(commands, "_get_git_editor", return_value="editor"),
            patch.object(commands.subprocess, "run"),
            patch.object(commands, "load_yaml", return_value={"lr": "0.1 | 0.2"}),
            patch.object(commands, "generate_batch_configs", return_value=[{"lr": 0.1}, {"lr": 0.2}]),
            patch.object(commands, "TaskGenerator", DummyGenerator),
            patch("builtins.input", side_effect=["y", "batch"]),
        ):
            commands.cmd_generate(task_manager, [str(template)])

        output = capsys.readouterr().out
        assert "Created 2 task(s)" in output
        assert "batch-1" in output

    def test_generate_handles_editor_failure_empty_config_and_cancel(self, workspace, task_manager, tmp_path, capsys):
        from pyruns.cli import commands

        template = tmp_path / "template.yaml"
        template.write_text("lr: 0.1\n", encoding="utf-8")

        with (
            patch.object(commands, "_get_git_editor", return_value="missing-editor"),
            patch.object(commands.subprocess, "run", side_effect=RuntimeError),
        ):
            commands.cmd_generate(task_manager, [str(template)])
        assert "Failed to launch editor" in capsys.readouterr().out

        with (
            patch.object(commands, "_get_git_editor", return_value="editor"),
            patch.object(commands.subprocess, "run"),
            patch.object(commands, "load_yaml", return_value={}),
        ):
            commands.cmd_generate(task_manager, [str(template)])
        assert "Empty config" in capsys.readouterr().out

        with (
            patch.object(commands, "_get_git_editor", return_value="editor"),
            patch.object(commands.subprocess, "run"),
            patch.object(commands, "load_yaml", return_value={"lr": "0.1 | 0.2"}),
            patch.object(commands, "generate_batch_configs", return_value=[{"lr": 0.1}, {"lr": 0.2}]),
            patch("builtins.input", return_value="n"),
        ):
            commands.cmd_generate(task_manager, [str(template)])
        assert "Cancelled" in capsys.readouterr().out

    def test_generate_reports_missing_template(self, task_manager, tmp_path, capsys):
        from pyruns.cli.commands import cmd_generate

        class DummyTaskManager:
            tasks_dir = str(tmp_path / "workspace" / TASKS_DIR)

        cmd_generate(DummyTaskManager(), [str(tmp_path / "missing.yaml")])

        assert "Template not found" in capsys.readouterr().out


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

    def test_run_exact_name_lazy_loads_without_full_scan(self, workspace):
        from pyruns.cli.commands import cmd_run
        from pyruns.core.task_manager import TaskManager

        _add_task(workspace, "target-task", "pending")
        with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
            manager = TaskManager(tasks_dir=str(workspace / TASKS_DIR), lazy_scan=None)

        with patch.object(manager, "scan_disk", side_effect=AssertionError("unexpected scan")):
            with patch.object(manager, "start_task_now") as mock_start:
                cmd_run(manager, ["target-task", "--detach"])

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

    def test_run_skips_active_tasks_and_reports_no_runnable_targets(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_run

        _add_task(workspace, "busy", "queued")
        task_manager.scan_disk()

        cmd_run(task_manager, ["busy", "--detach"])

        output = capsys.readouterr().out
        assert "Skipping 'busy'" in output
        assert "No runnable tasks found" in output

    def test_run_single_with_explicit_mode_and_workers_note(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_run

        _add_task(workspace, "process-one", "pending")
        task_manager.scan_disk()

        with (
            patch.object(task_manager, "start_task_now") as mock_start,
            patch("pyruns.cli.commands.cmd_fg"),
        ):
            cmd_run(task_manager, ["process-one", "--mode", "process", "--workers", "3"])

        mock_start.assert_called_once_with("process-one", execution_mode="process")
        assert "--workers is ignored" in capsys.readouterr().out

    def test_run_batch_with_flags(self, workspace, task_manager):
        from pyruns.cli.commands import cmd_run

        _add_task(workspace, "batch-a", "pending")
        _add_task(workspace, "batch-b", "pending")
        task_manager.scan_disk()

        with patch.object(task_manager, "start_batch_tasks") as mock_batch:
            cmd_run(task_manager, ["batch-a", "batch-b", "--workers", "3", "--mode", "process", "--detach"])
            mock_batch.assert_called_once_with(["batch-a", "batch-b"], execution_mode="process", max_workers=3)

    def test_run_config_creates_tasks_and_delegates_to_run(self, workspace, task_manager, tmp_path):
        from pyruns.cli import commands

        script_dir = tmp_path / "project"
        config_dir = script_dir / "configs"
        config_dir.mkdir(parents=True)
        script_path = script_dir / "train.py"
        script_path.write_text("print('train')\n", encoding="utf-8")
        config_path = config_dir / "quick.yaml"
        config_path.write_text("lr: 0.1 | 0.2\n", encoding="utf-8")

        with patch.object(commands, "cmd_run") as mock_run:
            commands.cmd_run_config(
                task_manager,
                "configs/quick.yaml",
                ["--workers", "2", "--detach"],
                script_path=str(script_path),
            )

        run_args = mock_run.call_args.args[1]
        created_names = run_args[:2]
        assert run_args[2:] == ["--workers", "2", "--detach"]
        assert created_names == ["quick_[1-of-2]", "quick_[2-of-2]"]
        for name in created_names:
            assert (workspace / TASKS_DIR / name / CONFIG_FILENAME).exists()

    def test_run_config_reports_missing_invalid_and_generator_failures(self, task_manager, tmp_path, capsys):
        from pyruns.cli import commands

        commands.cmd_run_config(task_manager, str(tmp_path / "missing.yaml"))
        assert "Config not found" in capsys.readouterr().out

        bad_config = tmp_path / "bad.yaml"
        bad_config.write_text("a: [1, 2\n", encoding="utf-8")
        commands.cmd_run_config(task_manager, str(bad_config))
        assert "Failed to prepare config" in capsys.readouterr().out

        good_config = tmp_path / "good.yaml"
        good_config.write_text("lr: 0.1\n", encoding="utf-8")
        with patch.object(commands.TaskGenerator, "create_tasks", side_effect=ValueError("bad task name")):
            commands.cmd_run_config(task_manager, str(good_config))
        assert "bad task name" in capsys.readouterr().out

    def test_run_config_falls_back_to_add_task_when_manager_lacks_add_tasks(self, tmp_path):
        from pyruns.cli import commands

        config_path = tmp_path / "single.yaml"
        config_path.write_text("lr: 0.1\n", encoding="utf-8")

        class DummyTaskManager:
            tasks_dir = str(tmp_path / "tasks")

            def __init__(self):
                self.added = []

            def add_task(self, task):
                self.added.append(task["name"])

        manager = DummyTaskManager()
        with patch.object(commands, "cmd_run") as mock_run:
            commands.cmd_run_config(manager, str(config_path), ["--detach"])

        assert manager.added == ["single"]
        assert mock_run.call_args.args[1] == ["single", "--detach"]


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
    def test_show_no_args_prints_usage(self, task_manager, capsys):
        from pyruns.cli.commands import cmd_show

        cmd_show(task_manager)

        assert "Usage: show" in capsys.readouterr().out

    def test_show_prints_task_detail(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_show

        _add_task(workspace, "detail-task", "completed", config={"lr": 0.02, "epochs": 10})
        task_manager.scan_disk()

        cmd_show(task_manager, ["detail-task"])
        captured = capsys.readouterr()
        assert "detail-task" in captured.out
        assert "Status:" in captured.out


class TestCmdLog:
    def test_log_no_args_prints_usage(self, task_manager, capsys):
        from pyruns.cli.commands import cmd_log

        cmd_log(task_manager)

        assert "Usage: log" in capsys.readouterr().out


class TestCmdOpenEdges:
    def test_open_no_args_and_editor_failure(self, workspace, task_manager, capsys):
        from pyruns.cli.commands import cmd_open

        cmd_open(task_manager)
        assert "Usage: open" in capsys.readouterr().out

        _add_task(workspace, "open-error", "completed")
        task_manager.scan_disk()
        with (
            patch("pyruns.cli.commands._get_git_editor", return_value="bad-editor"),
            patch("pyruns.cli.commands.subprocess.Popen", side_effect=RuntimeError("cannot launch")),
        ):
            cmd_open(task_manager, ["open-error"])

        assert "Failed to open editor" in capsys.readouterr().out


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

    def test_export_reports_empty_matches_and_empty_content(self, workspace, task_manager, tmp_path, capsys):
        from pyruns.cli import commands

        _add_task(workspace, "pending-export", "pending")
        task_manager.scan_disk()

        commands.cmd_export(task_manager, ["--status", "completed", "--output", str(tmp_path / "none.csv")])
        assert "No tasks matched" in capsys.readouterr().out

        with patch.object(commands, "build_export_csv", return_value=""):
            commands.cmd_export(task_manager, ["pending-export", "--output", str(tmp_path / "empty.csv")])
        assert "No exportable data" in capsys.readouterr().out

    def test_export_json_defaults_to_generated_filename(self, workspace, task_manager, tmp_path, monkeypatch):
        from pyruns.cli import commands

        _add_task(workspace, "json-export", "completed")
        task_manager.scan_disk()
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(commands, "export_timestamp", lambda: "20260609_010203")

        commands.cmd_export(task_manager, ["json-export", "--format", "json"])

        output_path = tmp_path / "pyruns_export_20260609_010203.json"
        assert output_path.exists()
        assert json.loads(output_path.read_text(encoding="utf-8")) == []




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
        mock_launch.assert_called_once_with("/generator?launcher=1", port=None, open_browser=None)
        captured = capsys.readouterr()
        assert "Starting shell workspace" in captured.out
        assert "Generator" in captured.out

    def test_pyr_port_without_args_opens_shell_workspace_on_requested_port(self, tmp_path, monkeypatch):
        from pyruns.cli import pyr

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["pyr", "-p", "9012"])

        with patch("pyruns.cli.bootstrap_shell_workspace", return_value=str(tmp_path / "_pyruns_" / "_shell_")):
            with patch("pyruns.cli._launch_ui") as mock_launch:
                pyr()

        mock_launch.assert_called_once_with("/generator?launcher=1", port=9012, open_browser=None)

    def test_pyr_no_browser_without_args_opens_shell_workspace_without_browser(self, tmp_path, monkeypatch):
        from pyruns.cli import pyr

        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr("sys.argv", ["pyr", "--no-browser"])

        with patch("pyruns.cli.bootstrap_shell_workspace", return_value=str(tmp_path / "_pyruns_" / "_shell_")):
            with patch("pyruns.cli._launch_ui") as mock_launch:
                pyr()

        mock_launch.assert_called_once_with("/generator?launcher=1", port=None, open_browser=False)

    def test_help_mentions_shell_start(self, monkeypatch, capsys):
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "help"])

        with pytest.raises(SystemExit):
            pyr()

        captured = capsys.readouterr()
        assert "Start web app in shell mode for current directory" in captured.out
        assert "pyr -p <port>" in captured.out
        assert "pyr train.py -p 9000" in captured.out
        assert "pyr --no-browser" in captured.out
        assert "pyr ui" in captured.out

    def test_direct_info_command_dispatches_to_cli(self, monkeypatch):
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "info"])

        with patch("pyruns.cli._dispatch_cli") as mock_dispatch:
            pyr()

        mock_dispatch.assert_called_once_with(["info"])

    def test_direct_cli_command_keeps_port_like_args(self, monkeypatch):
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "info", "-p", "abc"])

        with patch("pyruns.cli._dispatch_cli") as mock_dispatch:
            pyr()

        mock_dispatch.assert_called_once_with(["info", "-p", "abc"])

    def test_pyr_script_launch_accepts_port_after_script(self, tmp_path, monkeypatch):
        from pyruns.cli import pyr

        script_path = tmp_path / "train.py"
        script_path.write_text("print('train')\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["pyr", str(script_path), "-p", "9020"])

        with patch("pyruns.cli.ensure_root_dir"):
            with patch("pyruns.cli._setup_env") as mock_setup:
                with patch("pyruns.cli._launch_ui") as mock_launch:
                    pyr()

        mock_setup.assert_called_once_with(str(script_path).replace("\\", "/"), None)
        mock_launch.assert_called_once_with("/", port=9020, open_browser=None)

    def test_pyr_script_launch_accepts_port_before_script(self, tmp_path, monkeypatch):
        from pyruns.cli import pyr

        script_path = tmp_path / "train.py"
        script_path.write_text("print('train')\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", ["pyr", "--port=9021", str(script_path)])

        with patch("pyruns.cli.ensure_root_dir"):
            with patch("pyruns.cli._setup_env") as mock_setup:
                with patch("pyruns.cli._launch_ui") as mock_launch:
                    pyr()

        mock_setup.assert_called_once_with(str(script_path).replace("\\", "/"), None)
        mock_launch.assert_called_once_with("/", port=9021, open_browser=None)

    def test_pyr_rejects_invalid_port(self, monkeypatch, capsys):
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "-p", "abc"])

        with pytest.raises(SystemExit):
            pyr()

        captured = capsys.readouterr()
        assert "Invalid port: abc" in captured.out


class TestCliEntryHelpers:
    def test_print_version_exits_with_version(self, capsys):
        from pyruns import __version__
        from pyruns.cli import _print_version

        with pytest.raises(SystemExit) as exc:
            _print_version()

        assert exc.value.code == 0
        assert f"pyruns {__version__}" in capsys.readouterr().out

    def test_consume_ui_options_accepts_browser_and_port_forms(self):
        from pyruns.cli import _consume_ui_options

        port, open_browser, remaining = _consume_ui_options(
            ["--browser", "--port=9020", "train.py", "--no-browser", "-p", "9021"]
        )

        assert port == 9021
        assert open_browser is False
        assert remaining == ["train.py"]

    def test_consume_ui_options_rejects_missing_port(self, capsys):
        from pyruns.cli import _consume_ui_options

        with pytest.raises(SystemExit):
            _consume_ui_options(["--port"])

        assert "Missing value for --port" in capsys.readouterr().out

    def test_parse_port_value_rejects_out_of_range(self, capsys):
        from pyruns.cli import _parse_port_value

        with pytest.raises(SystemExit):
            _parse_port_value("70000")

        assert "Port must be between" in capsys.readouterr().out

    def test_resolve_workspace_can_match_script_info_path_and_ignores_bad_entries(self, tmp_path, monkeypatch):
        from pyruns.cli import _resolve_workspace

        monkeypatch.chdir(tmp_path)
        script = tmp_path / "nested" / "train.py"
        script.parent.mkdir()
        script.write_text("print('train')\n", encoding="utf-8")

        pyruns_root = tmp_path / "_pyruns_"
        (pyruns_root / "_shell_").mkdir(parents=True)
        (pyruns_root / "broken").mkdir()
        (pyruns_root / "broken" / "script_info.json").write_text("{bad json", encoding="utf-8")
        target = pyruns_root / "custom-workspace"
        target.mkdir()
        (target / "script_info.json").write_text(
            json.dumps({"script_name": "other", "script_path": str(script)}),
            encoding="utf-8",
        )

        assert _resolve_workspace(str(script)) == str(target)

    def test_resolve_workspace_without_script_picks_latest_existing_script(self, tmp_path, monkeypatch):
        from pyruns.cli import _resolve_workspace

        monkeypatch.chdir(tmp_path)
        older = tmp_path / "older.py"
        newer = tmp_path / "newer.py"
        older.write_text("print('old')\n", encoding="utf-8")
        newer.write_text("print('new')\n", encoding="utf-8")
        os.utime(older, (1000, 1000))
        os.utime(newer, (2000, 2000))

        pyruns_root = tmp_path / "_pyruns_"
        old_ws = pyruns_root / "older"
        new_ws = pyruns_root / "newer"
        old_ws.mkdir(parents=True)
        new_ws.mkdir()
        (old_ws / "script_info.json").write_text(json.dumps({"script_path": str(older)}), encoding="utf-8")
        (new_ws / "script_info.json").write_text(json.dumps({"script_path": str(newer)}), encoding="utf-8")

        assert _resolve_workspace() == str(new_ws)

    def test_interactive_repl_handles_parse_errors_unknown_commands_and_handler_errors(self, capsys):
        from pyruns.cli import interactive

        calls = []

        class DummyTaskManager:
            def refresh_from_disk(self, **kwargs):
                calls.append(("refresh", kwargs))

        def failing_handler(tm, args):
            calls.append(("fail", args))
            raise RuntimeError("boom")

        with (
            patch.object(interactive, "cmd_list", lambda tm, args: calls.append(("list", args))),
            patch.dict(interactive.COMMANDS, {"fail": failing_handler}, clear=True),
            patch("builtins.input", side_effect=["bad 'quote", "unknown", "help", "fail one", "exit"]),
        ):
            interactive.run_interactive(DummyTaskManager())

        output = capsys.readouterr().out
        assert "Unknown command" in output
        assert "Pyruns CLI Commands" in output
        assert "Command failed: RuntimeError: boom" in output
        assert calls[0] == ("list", ["--limit", "12"])
        assert ("fail", ["one"]) in calls

    def test_init_task_manager_sets_root_and_creates_tasks_dir(self, tmp_path, monkeypatch):
        import pyruns.cli as cli

        workspace = tmp_path / "_pyruns_" / "main"
        workspace.mkdir(parents=True)
        monkeypatch.delenv(cli.ENV_KEY_ROOT, raising=False)

        class DummyTaskManager:
            def __init__(self, tasks_dir, lazy_scan=False):
                self.tasks_dir = tasks_dir
                self.lazy_scan = lazy_scan

        monkeypatch.setattr("pyruns.core.task_manager.TaskManager", DummyTaskManager)
        monkeypatch.setattr("pyruns.utils.settings.ensure_settings_file", lambda workspace: None)
        monkeypatch.setattr("pyruns.utils.settings.load_settings", lambda workspace: {})

        manager = cli._init_task_manager(str(workspace))

        assert manager.tasks_dir == str(workspace / TASKS_DIR)
        assert manager.lazy_scan is False
        assert os.environ[cli.ENV_KEY_ROOT] == str(workspace)
        assert (workspace / TASKS_DIR).exists()

    def test_dispatch_cli_reports_missing_workspace(self, monkeypatch, capsys):
        import pyruns.cli as cli

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        monkeypatch.setattr(cli, "_resolve_workspace", lambda: None)

        with pytest.raises(SystemExit) as exc:
            cli._dispatch_cli(["info"])

        assert exc.value.code == 1
        assert "No pyruns workspace found" in capsys.readouterr().out

    def test_dispatch_cli_runs_interactive_and_named_command(self, monkeypatch):
        import pyruns.cli as cli
        from pyruns.cli import commands

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        calls = []
        dummy_manager = object()
        monkeypatch.setattr(cli, "_resolve_workspace", lambda: "workspace")
        monkeypatch.setattr(cli, "_init_task_manager", lambda workspace, **kwargs: dummy_manager)
        monkeypatch.setattr("pyruns.cli.interactive.run_interactive", lambda tm: calls.append(("interactive", tm)))

        cli._dispatch_cli(["cli"])
        assert calls == [("interactive", dummy_manager)]

        monkeypatch.setitem(commands.COMMANDS, "sentinel", lambda tm, args: calls.append(("sentinel", tm, args)))
        cli._dispatch_cli(["sentinel", "a", "b"])
        assert calls[-1] == ("sentinel", dummy_manager, ["a", "b"])

    def test_dispatch_cli_reuses_script_argument_workspace(self, tmp_path, monkeypatch):
        import pyruns.cli as cli
        from pyruns.cli import commands

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        calls = []
        dummy_manager = object()
        monkeypatch.setattr(cli, "_setup_env", lambda path: calls.append(("setup", path)) or "workspace")
        monkeypatch.setattr(cli, "_init_task_manager", lambda workspace, **kwargs: dummy_manager)
        monkeypatch.setitem(commands.COMMANDS, "info", lambda tm, args: calls.append(("info", tm, args)))

        cli._dispatch_cli(["info", str(script), "--verbose"])

        assert calls == [("setup", str(script)), ("info", dummy_manager, ["--verbose"])]

    def test_dispatch_cli_run_script_task_uses_existing_run_command(self, tmp_path, monkeypatch):
        import pyruns.cli as cli
        from pyruns.cli import commands

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        calls = []
        dummy_manager = object()
        monkeypatch.setattr(cli, "_setup_env", lambda path: calls.append(("setup", path)) or "workspace")
        monkeypatch.setattr(cli, "_init_task_manager", lambda workspace, **kwargs: dummy_manager)
        monkeypatch.setitem(commands.COMMANDS, "run", lambda tm, args: calls.append(("run", tm, args)))

        cli._dispatch_cli(["run", str(script), "task-a", "--detach"])

        assert calls == [
            ("setup", str(script)),
            ("run", dummy_manager, ["task-a", "--detach"]),
        ]

    def test_dispatch_cli_run_script_yaml_like_task_name_stays_task(self, tmp_path, monkeypatch):
        import pyruns.cli as cli
        from pyruns.cli import commands

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        calls = []
        dummy_manager = object()
        monkeypatch.setattr(cli, "_setup_env", lambda path: calls.append(("setup", path)) or "workspace")
        monkeypatch.setattr(cli, "_init_task_manager", lambda workspace, **kwargs: dummy_manager)
        monkeypatch.setitem(commands.COMMANDS, "run", lambda tm, args: calls.append(("run", tm, args)))

        cli._dispatch_cli(["run", str(script), "task-a.yaml"])

        assert calls == [
            ("setup", str(script)),
            ("run", dummy_manager, ["task-a.yaml"]),
        ]

    def test_dispatch_cli_run_script_yaml_creates_and_runs_config(self, tmp_path, monkeypatch):
        import pyruns.cli as cli

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        script = tmp_path / "train.py"
        config = tmp_path / "quick.yaml"
        script.write_text("print('train')\n", encoding="utf-8")
        config.write_text("lr: 0.1\n", encoding="utf-8")
        calls = []
        dummy_manager = object()

        def setup_env(path, yaml=None):
            calls.append(("setup", path, yaml))
            return "workspace"

        def run_config(tm, path, args, *, script_path=None):
            calls.append(("run_config", tm, path, args, script_path))

        monkeypatch.setattr(cli, "_setup_env", setup_env)
        monkeypatch.setattr(cli, "_init_task_manager", lambda workspace, **kwargs: dummy_manager)
        monkeypatch.setattr("pyruns.cli.commands.cmd_run_config", run_config)

        cli._dispatch_cli(["run", str(script), str(config), "--workers", "2"])

        assert calls == [
            ("setup", str(script), str(config)),
            ("run_config", dummy_manager, str(config), ["--workers", "2"], str(script)),
        ]

    def test_dispatch_cli_rejects_unknown_command(self, monkeypatch, capsys):
        import pyruns.cli as cli

        monkeypatch.delenv(cli.ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
        monkeypatch.setattr(cli, "_resolve_workspace", lambda: "workspace")
        monkeypatch.setattr(cli, "_init_task_manager", lambda workspace, **kwargs: object())

        with pytest.raises(SystemExit) as exc:
            cli._dispatch_cli(["not-real"])

        assert exc.value.code == 1
        assert "Unknown command" in capsys.readouterr().out

    def test_launch_ui_delegates_to_web_main_and_resets_argv(self, monkeypatch):
        import pyruns.cli as cli
        import pyruns.web.app as web_app

        captured = {}
        monkeypatch.setattr("sys.argv", ["pyr", "leftover"])
        monkeypatch.setattr(web_app, "main", lambda **kwargs: captured.update(kwargs))

        cli._launch_ui("/manager", port=9010, open_browser=True)

        assert captured == {"start_path": "/manager", "port": 9010, "open_browser": True}
        assert sys.argv == ["pyr"]

    def test_handle_ui_launch_rejects_missing_path_and_launches_existing_script(self, tmp_path, monkeypatch, capsys):
        import pyruns.cli as cli

        with pytest.raises(SystemExit):
            cli._handle_ui_launch(str(tmp_path / "missing.py"), None)
        assert "is not a file or known command" in capsys.readouterr().out

        script = tmp_path / "train.py"
        config = tmp_path / "config.yaml"
        script.write_text("print('train')\n", encoding="utf-8")
        config.write_text("lr: 0.1\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(cli, "ensure_root_dir", lambda *args: calls.append(("ensure", args)))
        monkeypatch.setattr(cli, "_setup_env", lambda path, yaml: calls.append(("setup", path, yaml)))
        monkeypatch.setattr(cli, "_launch_ui", lambda path, **kwargs: calls.append(("launch", path, kwargs)))

        cli._handle_ui_launch(str(script), str(config), port=9009, open_browser=False)

        assert calls[0][0] == "ensure"
        assert calls[1] == ("setup", str(script).replace("\\", "/"), str(config))
        assert calls[2] == ("launch", "/", {"port": 9009, "open_browser": False})

    def test_launch_shell_workspace_ui_bootstraps_current_directory(self, tmp_path, monkeypatch, capsys):
        import pyruns.cli as cli

        calls = []
        monkeypatch.chdir(tmp_path)
        monkeypatch.setattr(cli, "ensure_root_dir", lambda root: calls.append(("ensure", root)))
        monkeypatch.setattr(cli, "bootstrap_shell_workspace", lambda root: calls.append(("bootstrap", root)) or str(tmp_path / "_pyruns_" / "_shell_"))
        monkeypatch.setattr(cli, "_launch_ui", lambda path, **kwargs: calls.append(("launch", path, kwargs)))

        cli._launch_shell_workspace_ui(port=9022, open_browser=False)

        assert calls[-1] == ("launch", "/generator?launcher=1", {"port": 9022, "open_browser": False})
        assert "Starting shell workspace" in capsys.readouterr().out

    def test_pyr_dev_requires_script_and_delegates_launch(self, tmp_path, monkeypatch, capsys):
        import pyruns.cli as cli
        from pyruns.cli import pyr

        monkeypatch.setattr("sys.argv", ["pyr", "dev"])
        with pytest.raises(SystemExit):
            pyr()
        assert "Usage: pyr dev" in capsys.readouterr().out

        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr("sys.argv", ["pyr", "--port", "9040", "--browser", "dev", str(script), "cfg.yaml"])
        monkeypatch.setattr(cli, "_launch_dev", lambda script_arg, custom_yaml=None, **kwargs: calls.append((script_arg, custom_yaml, kwargs)))

        pyr()

        assert calls == [(str(script), "cfg.yaml", {"port": 9040, "open_browser": True})]

    def test_pyr_ui_paths_and_cli_option_rejection(self, tmp_path, monkeypatch, capsys):
        import pyruns.cli as cli
        from pyruns.cli import pyr

        calls = []
        monkeypatch.setattr(cli, "ensure_root_dir", lambda *args: calls.append(("ensure", args)))
        monkeypatch.setattr(cli, "launcher_query", lambda: "/launcher")
        monkeypatch.setattr(cli, "_launch_ui", lambda path, **kwargs: calls.append(("launch", path, kwargs)))
        monkeypatch.setattr("sys.argv", ["pyr", "ui"])
        pyr()
        assert calls[-1] == ("launch", "/launcher", {"port": None, "open_browser": None})
        assert "Opening launcher" in capsys.readouterr().out

        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        monkeypatch.setattr(cli, "_handle_ui_launch", lambda path, yaml=None, **kwargs: calls.append(("handle", path, yaml, kwargs)))
        monkeypatch.setattr("sys.argv", ["pyr", "ui", str(script), "cfg.yaml"])
        pyr()
        assert calls[-1] == ("handle", str(script), "cfg.yaml", {"port": None, "open_browser": None})

        monkeypatch.setattr("sys.argv", ["pyr", "--no-browser", "ls"])
        with pytest.raises(SystemExit):
            pyr()
        assert "UI launch options only apply" in capsys.readouterr().out

    def test_launch_dev_rejects_missing_script_and_runs_web_app(self, tmp_path, monkeypatch, capsys):
        import pyruns.cli as cli

        with pytest.raises(SystemExit):
            cli._launch_dev(str(tmp_path / "missing.py"))
        assert "not found" in capsys.readouterr().out

        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        calls = []
        monkeypatch.setattr(cli, "_setup_env", lambda path, yaml=None: calls.append(("setup", path, yaml)))
        monkeypatch.setattr(cli.subprocess, "run", lambda command, check=False: calls.append(("run", command, check)))

        cli._launch_dev(str(script), "config.yaml", port=9033, open_browser=False)

        assert calls[0] == ("setup", str(script).replace("\\", "/"), "config.yaml")
        assert calls[1][1][-3:] == ["--port", "9033", "--no-browser"]


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

    def test_argparse_workspace_default_selection_refreshes_current_defaults(self, tmp_path):
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

        workspace = tmp_path / "_pyruns_" / "train"
        workspace.mkdir(parents=True, exist_ok=True)
        default_path = workspace / CONFIG_DEFAULT_FILENAME
        default_path.write_text("epochs: 99\n", encoding="utf-8")

        resolved = bootstrap_workspace(str(script_path), str(default_path))

        assert resolved.replace("\\", "/").endswith("_pyruns_/train")
        default_text = default_path.read_text(encoding="utf-8")
        assert "epochs: 3" in default_text
        assert "epochs: 99" not in default_text


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


class TestInteractiveLs:
    def test_interactive_ls_renders_tasks_and_can_run_current_task(self, workspace, task_manager, capsys):
        from pyruns.cli import interactive_ls

        _add_task(workspace, "task-a", "pending")
        _add_task(workspace, "task-b", "completed")
        task_manager.scan_disk()

        with (
            patch.object(interactive_ls, "_enter_alt"),
            patch.object(interactive_ls, "_leave_alt"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls, "getch", side_effect=["a", "r", "q"]),
            patch.object(task_manager, "start_task_now") as mock_start,
        ):
            interactive_ls.run_interactive_ls(task_manager)

        captured = capsys.readouterr()
        assert "Pyruns Interactive View" in captured.out
        assert "task-a" in captured.out
        assert "task-b" in captured.out
        assert "selected" in captured.out
        mock_start.assert_called_once()
        assert mock_start.call_args.args[0] in {"task-a", "task-b"}

    def test_interactive_ls_filter_prompt_updates_query(self, workspace, task_manager, capsys):
        from pyruns.cli import interactive_ls

        _add_task(workspace, "visible-match", "pending")
        _add_task(workspace, "hidden-other", "pending")
        task_manager.scan_disk()

        with (
            patch.object(interactive_ls, "_enter_alt"),
            patch.object(interactive_ls, "_leave_alt"),
            patch.object(interactive_ls, "_flush_input"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls, "getch", side_effect=["f", "q"]),
            patch("builtins.input", return_value="visible"),
        ):
            interactive_ls.run_interactive_ls(task_manager)

        captured = capsys.readouterr()
        assert "(filter: 'visible')" in captured.out
        assert "visible-match" in captured.out

    def test_interactive_ls_handles_empty_list_and_filter_interrupt(self, workspace, task_manager, capsys):
        from pyruns.cli import interactive_ls

        with (
            patch.object(interactive_ls, "_enter_alt"),
            patch.object(interactive_ls, "_leave_alt"),
            patch.object(interactive_ls, "_flush_input"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=80),
            patch.object(interactive_ls, "getch", side_effect=["f", "q"]),
            patch("builtins.input", side_effect=KeyboardInterrupt),
        ):
            interactive_ls.run_interactive_ls(task_manager)

        captured = capsys.readouterr()
        assert "No tasks found" in captured.out
        assert "Exited interactive mode" in captured.out

    def test_interactive_ls_selection_navigation_delete_and_batch_branches(self, workspace, task_manager):
        from pyruns.cli import interactive_ls

        _add_task(workspace, "alpha", "pending")
        _add_task(workspace, "beta", "pending")
        task_manager.scan_disk()

        with (
            patch.object(interactive_ls, "_enter_alt"),
            patch.object(interactive_ls, "_leave_alt"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls, "getch", side_effect=["down", "up", "c", "c", "a", "a", "d", "q"]),
            patch.object(interactive_ls, "_delete_tasks") as mock_delete,
        ):
            interactive_ls.run_interactive_ls(task_manager)

        mock_delete.assert_called_once()
        assert len(mock_delete.call_args.args[1]) == 1

        batch_seen = {}

        def capture_batch(tm, tasks, selected):
            batch_seen["selected"] = set(selected)

        with (
            patch.object(interactive_ls, "_enter_alt"),
            patch.object(interactive_ls, "_leave_alt"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls, "getch", side_effect=["c", "b", "q"]),
            patch.object(interactive_ls, "_batch_run", side_effect=capture_batch) as mock_batch,
        ):
            interactive_ls.run_interactive_ls(task_manager)

        mock_batch.assert_called_once()
        assert batch_seen["selected"]

    def test_interactive_ls_open_log_env_export_branches(self, workspace, task_manager):
        from pyruns.cli import interactive_ls

        _add_task(workspace, "alpha", "pending")
        task_manager.scan_disk()

        with (
            patch.object(interactive_ls, "_enter_alt"),
            patch.object(interactive_ls, "_leave_alt"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls, "getch", side_effect=["o", "l", "e", "x", "q"]),
            patch("pyruns.cli.commands.cmd_open") as mock_open,
            patch.object(interactive_ls, "_view_log") as mock_view_log,
            patch.object(interactive_ls, "_edit_env") as mock_edit_env,
            patch.object(interactive_ls, "_do_export") as mock_export,
            patch("builtins.input", return_value=""),
        ):
            interactive_ls.run_interactive_ls(task_manager)

        mock_open.assert_called_once()
        mock_view_log.assert_called_once()
        mock_edit_env.assert_called_once()
        mock_export.assert_called_once()

    def test_batch_run_submits_only_runnable_selected_tasks(self):
        from pyruns.cli.interactive_ls import _batch_run

        class DummyTaskManager:
            calls = []

            def start_batch_tasks(self, names, execution_mode=None, max_workers=None):
                self.calls.append((names, execution_mode, max_workers))

        tm = DummyTaskManager()
        tasks = [
            {"name": "ready", "status": "pending"},
            {"name": "busy", "status": "running"},
        ]

        with patch("builtins.input", side_effect=["3", "process", ""]):
            _batch_run(tm, tasks, {"ready", "busy"})

        assert tm.calls == [(["ready"], "process", 3)]

    def test_batch_run_reports_when_no_selected_task_is_runnable(self, capsys):
        from pyruns.cli.interactive_ls import _batch_run

        class DummyTaskManager:
            def start_batch_tasks(self, names, execution_mode=None, max_workers=None):
                raise AssertionError("No task should be submitted")

        with patch("builtins.input", return_value=""):
            _batch_run(DummyTaskManager(), [{"name": "busy", "status": "queued"}], {"busy"})

        captured = capsys.readouterr()
        assert "No runnable tasks selected" in captured.out

    def test_batch_run_and_delete_handle_keyboard_interrupt(self, capsys):
        from pyruns.cli.interactive_ls import _batch_run, _delete_tasks

        class DummyTaskManager:
            def start_batch_tasks(self, names, execution_mode=None, max_workers=None):
                raise AssertionError("Interrupted batch should not submit")

            def delete_tasks(self, names):
                raise AssertionError("Interrupted delete should not submit")

        with patch("builtins.input", side_effect=[KeyboardInterrupt, ""]):
            _batch_run(DummyTaskManager(), [{"name": "ready", "status": "pending"}], {"ready"})

        with patch("builtins.input", side_effect=[KeyboardInterrupt, ""]):
            _delete_tasks(DummyTaskManager(), [{"name": "ready"}])

        captured = capsys.readouterr()
        assert captured.out.count("Cancelled") == 2

    def test_delete_tasks_confirms_before_deleting(self):
        from pyruns.cli.interactive_ls import _delete_tasks

        class DummyTaskManager:
            deleted = []

            def delete_tasks(self, names):
                self.deleted.append(names)

        tm = DummyTaskManager()
        with patch("builtins.input", side_effect=["yes", ""]):
            _delete_tasks(tm, [{"name": "obsolete"}])

        assert tm.deleted == [["obsolete"]]

    def test_delete_tasks_can_cancel(self, capsys):
        from pyruns.cli.interactive_ls import _delete_tasks

        class DummyTaskManager:
            def delete_tasks(self, names):
                raise AssertionError("Cancelled delete should not call delete_tasks")

        with patch("builtins.input", side_effect=["n", ""]):
            _delete_tasks(DummyTaskManager(), [{"name": "keep"}])

        captured = capsys.readouterr()
        assert "Cancelled" in captured.out

    def test_edit_env_merges_existing_task_env(self, workspace):
        from pyruns.cli.interactive_ls import _edit_env

        task_dir = workspace / TASKS_DIR / "env-edit"
        task_dir.mkdir(parents=True)
        save_task_info(str(task_dir), {"name": "env-edit", "status": "pending", "env": {"OLD": "1"}})

        class DummyTaskManager:
            updated = []

            def update_task_env(self, name, env):
                self.updated.append((name, env))

        tm = DummyTaskManager()
        with patch("builtins.input", return_value="NEW=2"):
            _edit_env(tm, {"name": "env-edit", "dir": str(task_dir)})

        assert tm.updated == [("env-edit", {"OLD": "1", "NEW": "2"})]

    def test_edit_env_can_delete_cancel_and_ignore_keyboard_interrupt(self, workspace, capsys):
        from pyruns.cli.interactive_ls import _edit_env

        task_dir = workspace / TASKS_DIR / "env-delete"
        task_dir.mkdir(parents=True)
        save_task_info(str(task_dir), {"name": "env-delete", "status": "pending", "env": {"OLD": "1"}})

        class DummyTaskManager:
            def __init__(self):
                self.updated = []

            def update_task_env(self, name, env):
                self.updated.append((name, env))

        tm = DummyTaskManager()
        with patch("builtins.input", return_value="OLD"):
            _edit_env(tm, {"name": "env-delete", "dir": str(task_dir)})
        assert tm.updated == [("env-delete", {})]

        empty_dir = workspace / TASKS_DIR / "env-empty"
        empty_dir.mkdir(parents=True)
        save_task_info(str(empty_dir), {"name": "env-empty", "status": "pending"})
        with patch("builtins.input", return_value=""):
            _edit_env(tm, {"name": "env-empty", "dir": str(empty_dir)})
        with patch("builtins.input", side_effect=KeyboardInterrupt):
            _edit_env(tm, {"name": "env-empty", "dir": str(empty_dir)})

        captured = capsys.readouterr()
        assert "(No custom variables)" in captured.out

    def test_do_export_writes_json_file(self, tmp_path, monkeypatch):
        from pyruns.cli import interactive_ls

        monkeypatch.chdir(tmp_path)
        task_dir = tmp_path / "tasks" / "export-me"
        task_dir.mkdir(parents=True)
        save_task_info(
            str(task_dir),
            {
                "name": "export-me",
                "status": "completed",
                "records": [{"loss": 0.1}],
                "tracks": [],
            },
        )
        tasks = [
            {
                "name": "export-me",
                "dir": str(task_dir),
                "status": "completed",
                "start_times": ["2026-06-03_12-00-00"],
                "finish_times": ["2026-06-03_12-01-00"],
            }
        ]

        with (
            patch.object(interactive_ls, "export_timestamp", return_value="2026-06-03_12-00-00"),
            patch("builtins.input", return_value="json"),
        ):
            interactive_ls._do_export(tasks)

        output = tmp_path / "pyruns_export_2026-06-03_12-00-00.json"
        assert output.exists()
        assert "export-me" in output.read_text(encoding="utf-8")

    def test_do_export_reports_no_data_for_empty_export(self, capsys):
        from pyruns.cli import interactive_ls

        with patch("builtins.input", return_value="csv"):
            interactive_ls._do_export([])

        captured = capsys.readouterr()
        assert "No data to export" in captured.out

    def test_view_log_reports_missing_log_files(self, tmp_path, capsys):
        from pyruns.cli.interactive_ls import _view_log

        task_dir = tmp_path / "task-no-logs"
        task_dir.mkdir()

        with patch("builtins.input", return_value=""):
            _view_log({"name": "task-no-logs", "dir": str(task_dir)})

        captured = capsys.readouterr()
        assert "No log files" in captured.out

    def test_view_log_renders_log_files_and_handles_navigation(self, tmp_path, capsys):
        from pyruns.cli import interactive_ls

        task_dir = tmp_path / "task-with-logs"
        log_dir = task_dir / "run_logs"
        log_dir.mkdir(parents=True)
        (log_dir / "run1.log").write_text("first run\n", encoding="utf-8")
        (log_dir / "run2.log").write_text("second run\n", encoding="utf-8")

        class TerminalSize:
            lines = 12

        if interactive_ls.os.name == "nt":
            readiness = patch.object(interactive_ls.msvcrt, "kbhit", return_value=True)
        else:
            readiness = patch.object(
                interactive_ls.select,
                "select",
                return_value=([interactive_ls.sys.stdin], [], []),
            )

        with (
            readiness,
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls.shutil, "get_terminal_size", return_value=TerminalSize()),
            patch.object(interactive_ls, "getch", side_effect=["n", "p", "q"]),
        ):
            interactive_ls._view_log({"name": "task-with-logs", "dir": str(task_dir)})

        captured = capsys.readouterr()
        assert "== task-with-logs ==" in captured.out
        assert "first run" in captured.out
        assert "second run" in captured.out

    def test_view_log_streams_live_chunks_and_handles_read_errors(self, tmp_path, capsys):
        from pyruns.cli import interactive_ls

        task_dir = tmp_path / "task-live-log"
        log_dir = task_dir / "run_logs"
        log_dir.mkdir(parents=True)
        (log_dir / "run1.log").write_text("", encoding="utf-8")

        class TerminalSize:
            lines = 12

        def subscribe(task_name, callback):
            callback("live one\nlive two\n")

        class LiveQueue:
            def __init__(self):
                self.items = []
                self.empty_checks = 0

            def put(self, item):
                self.items.append(item)

            def empty(self):
                self.empty_checks += 1
                return self.empty_checks == 1

            def get_nowait(self):
                if self.items:
                    return self.items.pop(0)
                raise interactive_ls.queue.Empty

        def readiness_patch():
            if interactive_ls.os.name == "nt":
                return patch.object(interactive_ls.msvcrt, "kbhit", return_value=True)
            return patch.object(
                interactive_ls.select,
                "select",
                return_value=([interactive_ls.sys.stdin], [], []),
            )

        with (
            readiness_patch(),
            patch.object(interactive_ls.queue, "Queue", LiveQueue),
            patch.object(interactive_ls.log_emitter, "subscribe", side_effect=subscribe),
            patch.object(interactive_ls.log_emitter, "unsubscribe"),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls.shutil, "get_terminal_size", return_value=TerminalSize()),
            patch.object(interactive_ls, "getch", side_effect=["q"]),
        ):
            interactive_ls._view_log({"name": "task-live-log", "dir": str(task_dir)})

        captured = capsys.readouterr()
        assert "live one" in captured.out
        assert "live two" in captured.out

        with (
            readiness_patch(),
            patch.object(interactive_ls, "_get_terminal_width", return_value=100),
            patch.object(interactive_ls.shutil, "get_terminal_size", return_value=TerminalSize()),
            patch.object(interactive_ls, "getch", side_effect=["q"]),
            patch("builtins.open", side_effect=OSError("cannot read")),
        ):
            interactive_ls._view_log({"name": "task-live-log", "dir": str(task_dir)})

        captured = capsys.readouterr()
        assert "Error: cannot read" in captured.out
