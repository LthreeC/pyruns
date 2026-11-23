"""Run input integrity, executed payload identity, and bounded history reads."""

import json
import os
import shutil
import sys

import pytest

from pyruns.core import executor, run_inputs
from pyruns.utils.info_io import get_log_options, load_task_metadata, save_task_info


def _task(tmp_path, **metadata):
    task_dir = tmp_path / "workspace" / "tasks" / "sample"
    task_dir.mkdir(parents=True)
    save_task_info(str(task_dir), {"name": "sample", "status": "queued", "run_index": 1, **metadata})
    return task_dir


def _input(**changes):
    return {
        "captured_at": "2026-10-15_12-00-00", "task_kind": "shell", "payload_file": "config.sh",
        "payload_text": "echo first\n", "script": None, "command": None,
        "environment": {"SEED": "1"}, "workdir": "/project", "shell_executable": "/bin/bash",
        **changes,
    }


def test_run_input_publication_is_immutable_bounded_and_excluded_from_logs(tmp_path, monkeypatch):
    task_dir = _task(tmp_path)
    assert run_inputs.load_run_input(str(task_dir), 1) is None
    run_inputs.save_run_input(str(task_dir), 1, _input())
    captured = run_inputs.load_run_input(str(task_dir), 1)
    assert captured["payload_text"] == "echo first\n"
    assert not get_log_options(str(task_dir))
    with pytest.raises(FileExistsError):
        run_inputs.save_run_input(str(task_dir), 1, _input(payload_text="overwrite"))
    assert run_inputs.load_run_input(str(task_dir), 1) == captured
    with pytest.raises(RuntimeError, match="ownership changed"):
        run_inputs.save_run_input(str(task_dir), 2, _input(), runner_id="stale")
    with pytest.raises(ValueError, match="Run must"):
        run_inputs.load_run_input(str(task_dir), -1)
    monkeypatch.setattr(run_inputs, "MAX_RUN_INPUT_BYTES", 10)
    with pytest.raises(ValueError, match="too large"):
        run_inputs.load_run_input(str(task_dir), 1)
    with pytest.raises(ValueError, match="too large"):
        run_inputs.save_run_input(str(task_dir), 2, _input())
    assert not (task_dir / "run_logs" / "run2.input.json").exists()


def test_run_input_refuses_redirected_and_corrupt_history(tmp_path):
    task_dir = _task(tmp_path)
    run_inputs.save_run_input(str(task_dir), 1, _input())
    path = task_dir / "run_logs" / "run1.input.json"
    path.write_text(json.dumps({"schema_version": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid run input"):
        run_inputs.load_run_input(str(task_dir), 1)
    outside = tmp_path / "outside.json"
    outside.write_text("unchanged", encoding="utf-8")
    path.unlink()
    try:
        path.symlink_to(outside)
    except OSError:
        pytest.skip("File symlinks unavailable")
    with pytest.raises(ValueError):
        run_inputs.load_run_input(str(task_dir), 1)
    with pytest.raises(ValueError):
        run_inputs.save_run_input(str(task_dir), 1, _input())
    assert outside.read_text() == "unchanged"


@pytest.mark.parametrize("kind", ["config", "shell"])
def test_worker_uses_live_shell_file_and_captured_python_config(tmp_path, monkeypatch, kind):
    filename = "config.yaml" if kind == "config" else "config.cmd" if os.name == "nt" else "config.sh"
    shell = (shutil.which("cmd.exe") if os.name == "nt" else shutil.which("sh")) if kind == "shell" else None
    task_dir = _task(tmp_path, task_kind=kind, config_file=filename, shell_executable=shell)
    payload = task_dir / filename
    initial = "echo captured-value\n" if kind == "shell" else "value: captured-value\n"
    payload.write_text(initial, encoding="utf-8", newline="")
    if kind == "config":
        script = tmp_path / "train.py"
        script.write_text(
            "import pyruns\ncfg = pyruns.load()\nprint(cfg.value)\npyruns.record(value=cfg.value)\n",
            encoding="utf-8",
        )
        from pyruns.utils.info_io import update_task_metadata

        update_task_metadata(str(task_dir), lambda info: info.update(script=str(script)))
    spawn = executor._spawn_captured_process

    def change_original_before_spawn(command, **kwargs):
        payload.write_text("echo later-value\n" if kind == "shell" else "value: later-value\n", encoding="utf-8")
        return spawn(command, **kwargs)

    monkeypatch.setattr(executor, "_spawn_captured_process", change_original_before_spawn)
    monkeypatch.setattr(executor, "_build_run_source_state", lambda **kwargs: "test source")
    monkeypatch.setattr(executor, "collect_run_environment", lambda *args, **kwargs: {})
    result = executor.run_task_worker(
        str(task_dir), "sample", "now", {"value": "captured-value"},
        env_vars={"EXPLICIT": "saved", "__PYRUNS_PYTHON_EXECUTABLE__": sys.executable},
    )
    assert result["status"] == "completed", result
    saved = run_inputs.load_run_input(str(task_dir), 1)
    output = (task_dir / "run_logs" / "run1.log").read_text(encoding="utf-8")
    assert not list(task_dir.glob(".pyruns-input-*"))
    if kind == "shell":
        assert saved is None
        # Windows CMD adapts the original script into its UTF-8 launch wrapper.
        expected = "captured-value" if os.name == "nt" else "later-value"
        assert expected in output
        assert ".pyruns-input-" not in load_task_metadata(str(task_dir))["launch_command"]
    else:
        assert saved["payload_text"] == initial
        assert saved["environment"]["EXPLICIT"] == "saved"
        assert "PATH" not in saved["environment"]
        assert "captured-value" in output and "later-value" not in output
        assert load_task_metadata(str(task_dir))["records"][0]["value"] == "captured-value"


def test_input_diff_ignores_capture_details_but_preserves_text_and_override_changes():
    same = run_inputs.compare_run_inputs(
        _input(launch_argv=["sh", ".temporary-1"]),
        _input(captured_at="later", launch_argv=["sh", ".temporary-2"]),
        left_label="test@1", right_label="test@2",
    )
    assert same["equal"] and not same["diff"]
    changed = run_inputs.compare_run_inputs(
        _input(), _input(payload_text="echo second\n", environment={"SEED": "2"}),
        left_label="test@1", right_label="test@2",
    )
    assert changed["changed_fields"] == ["payload_text", "environment"]
    assert "-echo first" in changed["diff"] and "+echo second" in changed["diff"]
    newline = run_inputs.compare_run_inputs(_input(), _input(payload_text="echo first"), left_label="a", right_label="b")
    assert not newline["equal"] and "No newline" in newline["diff"]
    missing = run_inputs.compare_run_inputs(None, _input(), left_label="old@1", right_label="new@1")
    assert not missing["available"] and missing["equal"] is None


def test_input_diff_preserves_json_types_and_complete_change_detection_when_truncated():
    typed = run_inputs.compare_run_inputs(
        _input(environment={"value": False}), _input(environment={"value": 0}),
        left_label="a", right_label="b",
    )
    assert not typed["equal"] and typed["changed_fields"] == ["environment"]
    large = run_inputs.compare_run_inputs(
        _input(payload_text="old\n" * 10_000),
        _input(payload_text="new\n" * 10_000, environment={"SEED": "changed"}),
        left_label="a", right_label="b",
    )
    assert large["truncated"] and not large["equal"]
    assert large["changed_fields"] == ["payload_text", "environment"]
