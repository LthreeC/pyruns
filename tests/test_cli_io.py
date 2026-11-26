"""CLI regressions for bounded task summaries, polling, and log tails."""

from collections import Counter
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pyruns.cli import commands
from pyruns.cli.app import main
from pyruns.core.task_generator import TaskGenerator
from pyruns.core.task_manager import TaskManager
from pyruns.launcher import bootstrap_shell_workspace
from pyruns.utils.info_io import ensure_run_slot, update_task_info


def test_show_reads_external_curves_only_for_json(tmp_path, capsys, monkeypatch):
    from pyruns._config import ENV_KEY_CLI_TERMINAL_RUNTIME, ENV_KEY_ROOT
    from pyruns.utils import track_store

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv(ENV_KEY_ROOT, str(tmp_path / "_pyruns_"))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    task = TaskGenerator(root_dir=str(workspace / "tasks")).create_shell_task(
        "curve", "echo curve", workdir=str(tmp_path), exact_name=True,
    )
    points = list(range(track_store.INLINE_TRACK_POINTS))
    update_task_info(task["dir"], lambda info: info.update(
        status="completed", run_index=1, run_statuses=["completed"], tracks=[{"step": points}],
    ))
    assert (Path(task["dir"]) / track_store.TRACK_STORE_FILENAME).is_file()
    prefix = ["-C", str(tmp_path), "-w", "shell", "show"]
    with monkeypatch.context() as m:
        m.setattr(track_store, "read_tracks", lambda *a, **kw: pytest.fail("text show loaded curves"))
        for name in ("curve", "curve@1"):
            assert main([*prefix, name]) == 0
            assert "Status:     completed" in capsys.readouterr().out
    assert main([*prefix, "curve@1", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["tracks"] == [{"step": points}]
    assert data["selected_run"]["track"] == {"step": points}


def test_ls_limits_before_serializing_and_reuses_filtered_snapshot(monkeypatch, capsys):
    tasks = [
        {"name": f"task-{index:04d}", "status": "completed", "pinned": index == 999}
        for index in range(1000)
    ]
    tasks[1]["status"] = "failed"
    serialized = []

    def record(task, *, info_snapshot):
        assert info_snapshot is task
        serialized.append(task["name"])
        return {"name": task["name"], "status": info_snapshot["status"]}

    monkeypatch.setattr(commands, "_refresh_tasks", lambda _manager: tasks)
    monkeypatch.setattr(commands, "_task_record", record)
    args = SimpleNamespace(
        trash=False, query=None, status=["completed"], sort="name", reverse=False, limit=3,
    )
    commands.cmd_ls(SimpleNamespace(json_output=True), args, cast(TaskManager, None))

    result = json.loads(capsys.readouterr().out)
    assert serialized == ["task-0999", "task-0000", "task-0002"]
    assert [task["name"] for task in result["tasks"]] == serialized
    assert result["count"] == 3


def test_wait_keeps_settled_run_results_and_backs_off_until_progress(monkeypatch):
    tasks = [{"name": "done"}, {"name": "active"}]
    identities = {name: commands._TaskRunIdentity(1, name) for name in ("done", "active")}
    calls = Counter()
    sleeps = []

    def record(task, identity):
        name = task["name"]
        assert identity is identities[name]
        calls[name] += 1
        if name == "done":
            assert calls[name] == 1, "A completed bound run must not follow a later rerun"
            status = "completed"
        else:
            status = "queued" if calls[name] <= 12 else "running" if calls[name] == 13 else "completed"
        return {"name": name, "status": status, "run_index": 1}

    monkeypatch.setattr(commands, "_bound_task_record", record)
    monkeypatch.setattr(commands.time, "sleep", sleeps.append)
    result = commands._wait_for_task_records(tasks, identities)

    assert calls == {"done": 1, "active": 14}
    assert [record["name"] for record in result] == ["done", "active"]
    assert all(record["run_index"] == 1 and record["status"] == "completed" for record in result)
    assert sleeps[0] == 0.1 and max(sleeps) == 1.0
    assert sleeps[-1] == 0.1  # Starting the queued run resets the backoff.


def test_wait_backoff_respects_timeout_deadline(monkeypatch):
    now = 0.0
    sleeps = []

    def sleep(seconds):
        nonlocal now
        sleeps.append(seconds)
        now += seconds

    monkeypatch.setattr(commands.time, "monotonic", lambda: now)
    monkeypatch.setattr(commands.time, "sleep", sleep)
    monkeypatch.setattr(commands, "_bound_task_record", lambda *_: {"name": "active", "status": "running"})
    with pytest.raises(commands.CliError, match="timed out waiting for: active"):
        commands._wait_for_task_records(
            [{"name": "active"}], {"active": commands._TaskRunIdentity(1, "runner")}, timeout=0.35,
        )

    assert now == pytest.approx(0.35)
    assert sleeps == pytest.approx([0.1, 0.15, 0.1])


def test_follow_logs_backs_off_when_a_running_task_is_quiet(monkeypatch):
    statuses = iter(["queued", "queued", "queued", "running", "running", "completed"])
    sleeps = []
    identity = commands._TaskRunIdentity(run_index=1, runner_id="host:1:token")

    monkeypatch.setattr(
        commands,
        "_bound_task_record",
        lambda *_args: {"status": next(statuses)},
    )
    monkeypatch.setattr(
        commands,
        "_resolve_log_reference",
        lambda _task, **_kwargs: commands._LogReference("run1.log", 1, "run"),
    )
    monkeypatch.setattr(commands, "_write_available_log", lambda _path, offset: offset)
    monkeypatch.setattr(commands.time, "sleep", sleeps.append)

    result = commands._follow_task_logs({}, identity, 0, lambda _path, offset: offset)

    assert result["status"] == "completed"
    # A status change resets the next poll to the fast interval; only quiet
    # consecutive reads grow the backoff.
    assert sleeps[:4] == pytest.approx([0.1, 0.1, 0.15, 0.225])
    assert max(sleeps) > 0.1


@pytest.mark.parametrize(("data", "lines", "expected"), [
    (b"", 1, ""),
    (b"one\ntwo\n", 0, ""),
    (b"one\ntwo\n", 1, "two\n"),
    (b"one\ntwo", 1, "two"),
    (b"one\n\n", 1, "\n"),
    (b"one\ntwo\n", 100, "one\ntwo\n"),
    ("old\r\n中文🌈\r\nlast".encode(), 2, "中文🌈\nlast"),
    (b"progress 1\rprogress 2\nlast\n", 2, "progress 1\rprogress 2\nlast\n"),
])
def test_log_tail_preserves_line_and_unicode_boundaries(tmp_path, capsys, data, lines, expected):
    path = tmp_path / "run1.log"
    path.write_bytes(data)
    offset = commands._log_tail_offset(str(path), lines)
    assert commands._write_available_log(str(path), offset) == len(data)
    assert capsys.readouterr().out == expected


def test_tty_queue_progress_redraws_carriage_return_updates_in_one_block():
    progress = commands._QueueProgressOutput()

    rendered = progress.render("GPU 1/2 ready\rGPU 2/2 ready")

    assert rendered.count("\x1b[2K") == 1
    assert "GPU 1/2 ready" in rendered
    assert rendered.endswith("GPU 2/2 ready\r\n")


@pytest.mark.parametrize("line_length", [65535, 2_000_000])
def test_log_tail_limits_reverse_reads_and_streams_long_lines(tmp_path, monkeypatch, line_length):
    path = tmp_path / "run1.log"
    path.write_bytes(b"old\n" * 400_000 + b"x" * line_length + b"\r\n" + "中文🌈".encode())
    reads = []
    original_open = open

    class Reader:
        def __init__(self, *args, **kwargs):
            self.handle = original_open(*args, **kwargs)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.handle.close()

        def __getattr__(self, key):
            return getattr(self.handle, key)

        def read(self, size):
            assert 0 < size <= 65536
            reads.append(size)
            return self.handle.read(size)

    class Sink:
        encoding = "utf-8"
        parts = []

        def write(self, text):
            assert len(text) <= 65536
            self.parts.append(text)

        def flush(self):
            pass

    monkeypatch.setattr(commands, "open", Reader, raising=False)
    offset = commands._log_tail_offset(str(path), 2)
    assert sum(reads) <= line_length + len("\r\n中文🌈".encode()) + 65536
    sink = Sink()
    monkeypatch.setattr(commands.sys, "stdout", sink)
    commands._write_available_log(str(path), offset)
    assert "".join(sink.parts) == "x" * line_length + "\n中文🌈"


@pytest.mark.parametrize("replacement", ["rotate", "truncate"])
def test_log_follow_tail_restarts_after_file_replacement(tmp_path, monkeypatch, capsys, replacement):
    path = tmp_path / "run1.log"
    path.write_text("old\nlast\n", encoding="utf-8")
    calls = 0

    def record(*_args):
        nonlocal calls
        calls += 1
        if calls == 2:
            if replacement == "rotate":
                next_path = path.with_name("replacement.log")
                next_path.write_text("replacement is longer than the original\n", encoding="utf-8")
                next_path.replace(path)
            else:
                path.write_text("new\n", encoding="utf-8")
        return {"status": "running" if calls == 1 else "completed"}

    monkeypatch.setattr(commands, "_bound_task_record", record)
    monkeypatch.setattr(commands, "_resolve_log_reference", lambda *_args, **_kw: commands._LogReference(str(path), 1, "run"))
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)
    result = commands._follow_task({"name": "tail"}, identity=commands._TaskRunIdentity(1, "runner"), tail=1)
    expected = "last\n" + ("replacement is longer than the original\n" if replacement == "rotate" else "new\n")
    assert result["status"] == "completed"
    assert capsys.readouterr().out == expected


def test_log_tail_zero_follows_queue_to_captured_run(tmp_path, monkeypatch, capsys):
    queue = tmp_path / "queue.log"
    queue.write_text("old wait diagnostics\n", encoding="utf-8")
    run = tmp_path / "run2.log"
    run.write_text("new run output\n", encoding="utf-8")
    statuses = iter(["queued", "running", "completed"])
    monkeypatch.setattr(commands, "_bound_task_record", lambda *_: {"status": next(statuses)})
    monkeypatch.setattr(commands, "_resolve_log_reference", lambda *_args, run_index=None, **_kw: commands._LogReference(
        str(run if run_index else queue), run_index, "run" if run_index else "queue",
    ))
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)
    commands._follow_task({"name": "tail"}, identity=commands._TaskRunIdentity(2, "runner", True), tail=0)
    assert capsys.readouterr().out == "new run output\n"


def test_cli_tail_selects_historical_log_and_rejects_invalid_options(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    task = TaskGenerator(root_dir=str(workspace / "tasks")).create_shell_task(
        "logged", "echo logged\n", workdir=str(tmp_path), exact_name=True,
    )
    task_dir = Path(task["dir"])

    def history(info):
        ensure_run_slot(info, 2)
        info.update(status="completed", run_index=2, run_statuses=["completed", "completed"], exit_codes=[0, 0])

    update_task_info(str(task_dir), history)
    logs = task_dir / "run_logs"
    logs.mkdir(exist_ok=True)
    (logs / "run1.log").write_text("first\nold last\n", encoding="utf-8")
    (logs / "run2.log").write_text("new run\n", encoding="utf-8")
    # A missing or corrupt input snapshot cannot invalidate a log or exit status.
    (logs / "run1.input.json").write_text("corrupt", encoding="utf-8")
    (logs / "run2.input.json").write_text("corrupt", encoding="utf-8")
    assert main(["-C", str(tmp_path), "-w", "shell", "wait", "logged", "--timeout", "1"]) == 0
    capsys.readouterr()
    prefix = ["-C", str(tmp_path), "-w", "shell", "log"]
    assert main([*prefix, "logged@1", "--tail", "1"]) == 0
    assert capsys.readouterr().out == "old last\n"
    assert main([*prefix, "logged", "--tail", "0"]) == 0
    assert capsys.readouterr().out == ""
    assert main([*prefix, "logged", "--tail", "1", "--path"]) == 2
    assert capsys.readouterr().err
    for options in (["--tail", "-1"], ["--tail", "1.5"]):
        with pytest.raises(SystemExit) as exc:
            main([*prefix, "logged", *options])
        assert exc.value.code == 2
        assert capsys.readouterr().err
