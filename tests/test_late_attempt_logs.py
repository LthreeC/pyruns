"""Late worker diagnostics must not follow a task path into a new admission."""

import io
from types import SimpleNamespace

import pytest

from pyruns._config import ERROR_LOG_FILENAME, RUN_LOGS_DIR, TASK_KIND_CONFIG, TASK_KIND_SHELL
from pyruns.core import executor
from pyruns.utils.info_io import load_task_metadata, save_task_info, update_task_metadata


@pytest.fixture
def owned_task(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(str(task_dir), {
        "name": "task", "task_kind": TASK_KIND_SHELL, "cmd": ["unused"],
        "status": "running", "run_index": 1, "runner_id": "owner",
        "_attempt_id": "original", "run_attempt_ids": ["original"],
    })
    monkeypatch.setattr(executor, "_resolve_python_runtime", lambda *a, **k: {})
    monkeypatch.setattr(executor, "_build_command", lambda *a, **k: (["unused"], str(tmp_path), []))
    monkeypatch.setattr(executor, "_build_run_source_state", lambda **k: "")
    monkeypatch.setattr(executor, "collect_run_environment", lambda *a, **k: {})
    monkeypatch.setattr(executor, "get_process_create_time", lambda pid: 1.0)
    monkeypatch.setattr(executor.log_emitter, "emit", lambda *a, **k: None)
    return task_dir


def _run(task_dir):
    return executor.run_task_worker(
        str(task_dir), "task", "now", {}, run_index=1,
        runner_id="owner", attempt_id="original",
    )


def _replace_task(task_dir):
    """Simulate moving a finished task and admitting another at its old name."""
    task_dir.rename(task_dir.with_name("archived"))
    task_dir.mkdir()
    save_task_info(str(task_dir), {
        "name": "task", "status": "running", "run_index": 1,
        "runner_id": "owner", "_attempt_id": "replacement",
        "run_attempt_ids": ["replacement"],
    })
    logs = task_dir / RUN_LOGS_DIR
    logs.mkdir()
    for filename in ("run1.log", ERROR_LOG_FILENAME):
        (logs / filename).write_text("replacement log\n", encoding="utf-8")


def _assert_replacement_untouched(task_dir):
    info = load_task_metadata(str(task_dir))
    assert info["status"] == "running"
    assert info["_attempt_id"] == "replacement"
    for filename in ("run1.log", ERROR_LOG_FILENAME):
        assert (task_dir / RUN_LOGS_DIR / filename).read_text(encoding="utf-8") == "replacement log\n"


def test_rejected_run_input_does_not_log_errors_into_replacement(owned_task, monkeypatch):
    update_task_metadata(str(owned_task), lambda info: info.update(task_kind=TASK_KIND_CONFIG))
    save_input = executor.save_run_input

    def replace_before_input(*args, **kwargs):
        _replace_task(owned_task)
        return save_input(*args, **kwargs)

    monkeypatch.setattr(executor, "save_run_input", replace_before_input)
    monkeypatch.setattr(executor, "_spawn_captured_process", lambda *a, **k: pytest.fail("stale task launched"))
    result = _run(owned_task)

    assert result["status"] == "failed"
    assert "ownership changed" in result["error"]
    _assert_replacement_untouched(owned_task)
    assert not (owned_task / RUN_LOGS_DIR / "run1.input.json").exists()


@pytest.mark.parametrize("terminal_status", ["failed", "cancelled"])
def test_terminal_publication_then_path_reuse_keeps_late_logs_out(
    owned_task, monkeypatch, terminal_status,
):
    def finish():
        if terminal_status == "cancelled":
            update_task_metadata(str(owned_task), lambda info: info.update({
                "_pending_stop_summary": {
                    "run_index": 1, "reason": "cancelled_by_user", "event": "stopped",
                },
            }))
        return 1

    process = SimpleNamespace(pid=123, stdout=io.BytesIO(b"original output\n"), returncode=1,
                              wait=finish, poll=lambda: 1)
    monkeypatch.setattr(executor, "_spawn_captured_process", lambda *a, **k: process)
    replaced = []

    def publish_then_replace(path, updater, **kwargs):
        updated = update_task_metadata(path, updater, **kwargs)
        if updated["status"] == terminal_status and not replaced:
            replaced.append(True)
            _replace_task(owned_task)
        return updated

    monkeypatch.setattr(executor, "update_task_metadata", publish_then_replace)
    result = _run(owned_task)

    assert replaced == [True]
    assert result["status"] == terminal_status
    archived_log = owned_task.with_name("archived") / RUN_LOGS_DIR / "run1.log"
    assert "original output" in archived_log.read_text(encoding="utf-8")
    _assert_replacement_untouched(owned_task)


def test_owned_attempt_still_records_launch_errors(owned_task, monkeypatch):
    def fail_launch(*args, **kwargs):
        raise OSError("controlled launch failure")

    monkeypatch.setattr(executor, "_spawn_captured_process", fail_launch)
    result = _run(owned_task)

    assert result["status"] == "failed"
    assert load_task_metadata(str(owned_task))["run_statuses"] == ["failed"]
    for filename in ("run1.log", ERROR_LOG_FILENAME):
        assert "controlled launch failure" in (owned_task / RUN_LOGS_DIR / filename).read_text(encoding="utf-8")
