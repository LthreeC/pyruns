import json
import shutil
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from pyruns._config import (
    CMD_CONFIG_FILENAME,
    POWERSHELL_CONFIG_FILENAME,
    RECORDS_KEY,
    SHELL_CONFIG_FILENAME,
    TASK_INFO_FILENAME,
    TRACKS_KEY,
)
from pyruns.core.task_generator import TaskGenerator
from pyruns.launcher import bootstrap_workspace, list_config_candidates, list_script_candidates
from pyruns.utils.batch_utils import count_batch_configs, generate_batch_configs
from pyruns.utils.config_utils import load_yaml
from pyruns.web.runtime import PyrunsRuntime


EXAMPLES_DIR = Path(__file__).resolve().parents[1] / "examples"


def _copy_example(tmp_path: Path, name: str) -> Path:
    target = tmp_path / name
    shutil.copytree(
        EXAMPLES_DIR / name,
        target,
        ignore=shutil.ignore_patterns("_pyruns_", "outputs", "__pycache__"),
    )
    return target


def _wait_for_task(runtime: PyrunsRuntime, task_name: str, timeout: float = 20.0) -> dict:
    deadline = time.time() + timeout
    task = None
    while time.time() < deadline:
        task = runtime.get_task(task_name, refresh=True)
        if task and task.get("status") in {"completed", "failed"}:
            return task
        time.sleep(0.1)
    raise AssertionError(f"Task {task_name!r} did not finish in time; last={task!r}")


def _task_info(task: dict) -> dict:
    info_path = Path(task["dir"]) / TASK_INFO_FILENAME
    return json.loads(info_path.read_text(encoding="utf-8"))


def test_advanced_argparse_example_runs_through_runtime(tmp_path):
    example = _copy_example(tmp_path, "4_advanced_argparse")
    script = example / "main.py"
    config = example / "configs" / "quick.yaml"

    workspace = bootstrap_workspace(str(script), str(config))
    runtime = PyrunsRuntime(workspace)
    created = runtime.create_tasks_from_template(
        name_prefix="advanced",
        mode="yaml",
        yaml_text=config.read_text(encoding="utf-8"),
        append_timestamp=False,
    )
    task_name = created["items"][0]["name"]
    runtime.update_task_env(task_name, {"PYRUNS_EXAMPLE_ENV": "example-env-ok"})
    runtime.start_task(task_name)

    task = _wait_for_task(runtime, task_name)

    assert task["status"] == "completed"
    info = _task_info(task)
    record = info[RECORDS_KEY][0]
    tracks = info[TRACKS_KEY][0]
    assert record["layer_count"] == 3
    assert record["compile"] is False
    assert record["cache"] is False
    assert record["verbose"] == 2
    assert record["env_marker"] == "example-env-ok"
    assert len(tracks["loss"]) == 3
    assert len(tracks["throughput"]) == 3


def test_pyruns_load_nested_example_runs_through_runtime(tmp_path):
    example = _copy_example(tmp_path, "5_pyruns_load_nested")
    script = example / "train.py"
    config = example / "configs" / "base.yaml"

    workspace = bootstrap_workspace(str(script), str(config))
    runtime = PyrunsRuntime(workspace)
    created = runtime.create_tasks_from_template(
        name_prefix="nested",
        mode="yaml",
        yaml_text=config.read_text(encoding="utf-8"),
        append_timestamp=False,
    )
    task_name = created["items"][0]["name"]
    runtime.update_task_env(task_name, {"PYRUNS_EXAMPLE_ENV": "nested-env-ok"})
    runtime.start_task(task_name)

    task = _wait_for_task(runtime, task_name)

    assert task["status"] == "completed"
    info = _task_info(task)
    record = info[RECORDS_KEY][0]
    tracks = info[TRACKS_KEY][0]
    assert record["seed"] == 13
    assert record["model"] == "mlp"
    assert record["env_marker"] == "nested-env-ok"
    assert len(tracks["loss"]) == 3
    assert len(tracks["acc"]) == 3


def test_multi_script_example_is_discoverable_by_launcher(tmp_path):
    example = _copy_example(tmp_path, "7_multi_script_project")

    scripts = list_script_candidates(str(example))
    script_labels = {item["label"] for item in scripts}
    assert {"train.py", "evaluate.py"} <= script_labels

    train_configs = list_config_candidates(str(example / "train.py"))
    train_labels = {item["label"] for item in train_configs}
    assert "configs/train_cpu.yaml" in train_labels
    assert "configs/train_grid.yaml" in train_labels

    eval_configs = list_config_candidates(str(example / "evaluate.py"))
    eval_labels = {item["label"] for item in eval_configs}
    assert "configs/eval.yaml" in eval_labels


def test_shell_workspace_payload_examples_create_runtime_specific_tasks(tmp_path):
    payloads = EXAMPLES_DIR / "6_shell_workspace" / "payloads"
    cases = [
        ("bash-task", payloads / "bash_or_wsl.sh", SHELL_CONFIG_FILENAME),
        ("powershell-task", payloads / "powershell.ps1", POWERSHELL_CONFIG_FILENAME),
        ("cmd-task", payloads / "cmd.cmd", CMD_CONFIG_FILENAME),
    ]

    generator = TaskGenerator(root_dir=str(tmp_path))
    for name, payload_path, config_file in cases:
        with patch(
            "pyruns.core.task_generator.get_shell_config_filename_for_workspace",
            return_value=config_file,
        ):
            task = generator.create_shell_task(name, payload_path.read_text(encoding="utf-8"))

        assert task["config_file"] == config_file
        written = Path(task["dir"]) / config_file
        assert written.exists()
        assert "PYRUNS_EXAMPLE_ENV" in written.read_text(encoding="utf-8")


def test_new_example_yaml_files_are_concrete_single_run_configs():
    config_paths = [
        EXAMPLES_DIR / "4_advanced_argparse" / "configs" / "quick.yaml",
        EXAMPLES_DIR / "4_advanced_argparse" / "configs" / "grid.yaml",
        EXAMPLES_DIR / "5_pyruns_load_nested" / "configs" / "base.yaml",
        EXAMPLES_DIR / "5_pyruns_load_nested" / "configs" / "batch_grid.yaml",
        EXAMPLES_DIR / "7_multi_script_project" / "configs" / "train_cpu.yaml",
        EXAMPLES_DIR / "7_multi_script_project" / "configs" / "train_grid.yaml",
        EXAMPLES_DIR / "7_multi_script_project" / "configs" / "eval.yaml",
    ]

    for config_path in config_paths:
        config = load_yaml(str(config_path))
        assert count_batch_configs(config) == 1
        assert generate_batch_configs(config) == [config]


def test_new_examples_do_not_contain_ui_batch_pipe_syntax():
    roots = [
        EXAMPLES_DIR / "4_advanced_argparse",
        EXAMPLES_DIR / "5_pyruns_load_nested",
        EXAMPLES_DIR / "6_shell_workspace",
        EXAMPLES_DIR / "7_multi_script_project",
    ]
    checked_files = [
        path
        for root in roots
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in {".md", ".py", ".yaml", ".yml", ".sh", ".ps1", ".cmd"}
    ]

    assert checked_files
    for path in checked_files:
        assert "|" not in path.read_text(encoding="utf-8"), f"Unexpected pipe in {path}"


def test_example_batch_templates_are_form_mode_only(tmp_path):
    example = _copy_example(tmp_path, "4_advanced_argparse")
    script = example / "main.py"
    quick_config = example / "configs" / "quick.yaml"

    workspace = bootstrap_workspace(str(script), str(quick_config))
    runtime = PyrunsRuntime(workspace)
    yaml_text = "\n".join(
        [
            "dataset: toy",
            "layers:",
            "  - 64",
            "  - 128",
            "tag: grid",
            "compile: true",
            "use_amp: false",
            "cache: true",
            'verbose: "0 | 1 | 2"',
            'dropout: "-1.0 | 0.1"',
            "device: cpu",
            'seed: "11 | 22"',
            "",
        ]
    )

    form_preview = runtime.preview_tasks_from_template(mode="form", yaml_text=yaml_text)
    assert form_preview["count"] == 12

    with pytest.raises(ValueError, match="YAML mode does not support batch syntax"):
        runtime.preview_tasks_from_template(mode="yaml", yaml_text=yaml_text)
