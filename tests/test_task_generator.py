"""Task generation, persisted payloads, and legacy task metadata."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from omegaconf import OmegaConf

from pyruns._config import (
    CONFIG_FILENAME,
    POWERSHELL_CONFIG_FILENAME,
    SHELL_CONFIG_FILENAME,
    TASK_INFO_FILENAME,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
)
from pyruns.core.task_generator import TaskGenerator, create_task_object
from pyruns.core.task_manager import TaskManager
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.info_io import load_task_info, save_task_info


class TestCreateTaskObject:
    def test_python_task_fields_and_created_at_format(self):
        obj = create_task_object("/tmp/task1", "my-task", config={"lr": 0.01})
        assert obj["dir"] == "/tmp/task1"
        assert obj["name"] == "my-task"
        assert obj["status"] == "pending"
        assert obj["config"] == {"lr": 0.01}
        assert obj["env"] == {}
        assert len(obj["created_at"]) == 19
        assert "-" in obj["created_at"]
        assert "_" in obj["created_at"]

    def test_shell_task_fields(self):
        obj = create_task_object(
            "/tmp/task-shell",
            "shell-task",
            task_kind=TASK_KIND_SHELL,
            config_text="echo hello\n",
        )
        assert obj["task_kind"] == TASK_KIND_SHELL
        assert obj["config_file"] == SHELL_CONFIG_FILENAME
        assert obj["config_text"] == "echo hello\n"


class TestTaskGeneratorCreateTask:
    def test_create_task_writes_expected_files_and_metadata(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {
            "lr": 0.01,
            "model": {"name": "resnet"},
            "_meta_desc": "lr=0.01",
            "_meta_other": "x",
        }
        task = gen.create_task("my-exp", cfg)

        assert os.path.isdir(task["dir"])
        assert os.path.basename(task["dir"]).startswith("my-exp")
        info_path = os.path.join(task["dir"], TASK_INFO_FILENAME)
        assert os.path.exists(info_path)
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == "my-exp"
        assert info["status"] == "pending"
        cfg_path = os.path.join(task["dir"], CONFIG_FILENAME)
        assert os.path.exists(cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded == {"lr": 0.01, "model": {"name": "resnet"}}
        log_dir = os.path.join(task["dir"], "run_logs")
        assert os.path.isdir(log_dir)
        assert not os.path.exists(os.path.join(log_dir, "run1.log"))

    def test_group_index_is_used_in_task_name_and_folder(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="3-of-10")

        assert task["name"] == "batch-run_3-of-10"
        assert os.path.basename(task["dir"]) == task["name"]

    def test_deduplication_keeps_unique_dirs_when_timestamp_suffix_collides(self, tmp_path, monkeypatch):
        gen = TaskGenerator(root_dir=str(tmp_path))
        monkeypatch.setattr("pyruns.core.task_generator.time.time", lambda: 1234.567)

        tasks = [
            gen.create_task("same-name", {"x": 1}),
            gen.create_task("same-name", {"x": 2}),
            gen.create_task("same-name", {"x": 3}),
        ]

        assert len({task["dir"] for task in tasks}) == 3
        assert len({task["name"] for task in tasks}) == 3
        for task in tasks:
            assert os.path.isdir(task["dir"])
            assert load_task_info(task["dir"])["name"] == task["name"]

    def test_empty_prefix_uses_timestamp(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("", {"x": 1})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("task_")

    def test_task_kind_and_runtime_specific_shell_payload_are_persisted(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task_cfg = gen.create_task("cfg-task", {"x": 1}, task_kind=TASK_KIND_CONFIG)
        with patch(
            "pyruns.core.task_generator.get_shell_config_filename_for_workspace",
            return_value=POWERSHELL_CONFIG_FILENAME,
        ):
            task_shell = gen.create_shell_task("shell-task", "Write-Host 'hello'\n")

        info_cfg = load_task_info(task_cfg["dir"])
        info_shell = load_task_info(task_shell["dir"])

        assert info_cfg["task_kind"] == TASK_KIND_CONFIG
        assert "config_mode" not in info_cfg
        assert info_cfg["config_file"] == CONFIG_FILENAME
        assert info_shell["task_kind"] == TASK_KIND_SHELL
        assert "config_mode" not in info_shell
        assert info_shell["config_file"] == POWERSHELL_CONFIG_FILENAME
        assert task_shell["task_kind"] == TASK_KIND_SHELL
        assert task_shell["config_file"] == POWERSHELL_CONFIG_FILENAME
        assert os.path.exists(os.path.join(task_shell["dir"], POWERSHELL_CONFIG_FILENAME))

    def test_legacy_config_task_kind_is_loaded_as_python(self, tmp_path):
        task_dir = tmp_path / "legacy-task"
        task_dir.mkdir()
        save_task_info(str(task_dir), {
            "name": "legacy-task",
            "status": "pending",
            "created_at": "2026-01-01_00-00-00",
            "config_mode": "config",
            "config_file": CONFIG_FILENAME,
        })
        save_yaml(str(task_dir / CONFIG_FILENAME), {"x": 1})

        manager = TaskManager(
            tasks_dir=str(tmp_path), lazy_scan=False, owns_task_lifecycle=False,
        )
        task = manager.get_task("legacy-task")

        assert task is not None
        assert task["task_kind"] == TASK_KIND_CONFIG
        assert task["config_file"] == CONFIG_FILENAME

    def test_legacy_config_task_kind_input_writes_python(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("legacy-input", {"x": 1}, task_kind="config")

        with open(os.path.join(task["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info = json.load(f)

        assert task["task_kind"] == TASK_KIND_CONFIG
        assert info["task_kind"] == TASK_KIND_CONFIG
        assert "config_mode" not in info

    def test_invalid_task_kind_and_name_are_rejected(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        with pytest.raises(ValueError, match="Unsupported task kind"):
            gen.create_task("invalid", {"x": 1}, task_kind="unknown-kind")
        with pytest.raises(ValueError, match="invalid characters"):
            gen.create_task("bad/name", {"x": 1})


class TestTaskGeneratorCreateTasks:
    def test_single_and_batch_names_are_complete_and_unique(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        single = gen.create_tasks([{"x": 1}], "single")
        tasks = gen.create_tasks([{"x": i} for i in range(3)], "batch")

        assert [task["name"] for task in single] == ["single"]
        assert len(tasks) == 3
        assert [task["name"] for task in tasks] == [
            "batch_1-of-3",
            "batch_2-of-3",
            "batch_3-of-3",
        ]
        assert len({task["dir"] for task in tasks}) == 3

    def test_batch_with_pipe_configs(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        base = {"lr": "0.001 | 0.01", "bs": 32}
        configs = generate_batch_configs(base)
        assert len(configs) == 2

        tasks = gen.create_tasks(configs, "exp")
        assert len(tasks) == 2
        # Configs should have typed values, not pipe strings
        for task in tasks:
            cfg_path = os.path.join(task["dir"], "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            assert isinstance(cfg["lr"], (int, float))

    def test_batch_tasks_persist_unresolved_interpolations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYRUNS_TEST_SECRET", "must-not-be-persisted")
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = generate_batch_configs(
            OmegaConf.create(
                {
                    "lr": "0.001 | 0.01",
                    "secret": "${oc.env:PYRUNS_TEST_SECRET}",
                    "output": "${secret}/results",
                    "secret_choice": "${oc.env:PYRUNS_TEST_SECRET} | public",
                    "secret_list": [
                        "${oc.env:PYRUNS_TEST_SECRET}",
                        {"nested": "${secret}"},
                    ],
                }
            )
        )

        tasks = gen.create_tasks(configs, "interpolation")

        assert len(tasks) == 4
        config_texts = []
        for task in tasks:
            config_text = Path(task["dir"], CONFIG_FILENAME).read_text(encoding="utf-8")
            config_texts.append(config_text)
            assert "must-not-be-persisted" not in config_text
            assert "secret: ${oc.env:PYRUNS_TEST_SECRET}" in config_text
            assert "output: ${secret}/results" in config_text
            saved = yaml.safe_load(config_text)
            assert saved["secret_list"] == [
                "${oc.env:PYRUNS_TEST_SECRET}",
                {"nested": "${secret}"},
            ]
        assert any(
            "secret_choice: ${oc.env:PYRUNS_TEST_SECRET}" in text
            for text in config_texts
        )
        assert any("secret_choice: public" in text for text in config_texts)
