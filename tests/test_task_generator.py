"""
Tests for pyruns.core.task_generator — task creation and file writing.
"""
import os
import json
import yaml
import pytest

from pyruns.core.task_generator import TaskGenerator, create_task_object
from pyruns.utils.config_utils import generate_batch_configs


# ═══════════════════════════════════════════════════════════════
#  create_task_object
# ═══════════════════════════════════════════════════════════════

class TestCreateTaskObject:
    def test_basic_fields(self):
        obj = create_task_object("id1", "/tmp/task1", "my-task", {"lr": 0.01})
        assert obj["id"] == "id1"
        assert obj["dir"] == "/tmp/task1"
        assert obj["name"] == "my-task"
        assert obj["status"] == "pending"
        assert obj["config"] == {"lr": 0.01}
        assert obj["env"] == {}
        assert obj["run_at"] is None
        assert obj["rerun_at"] == []
        assert obj["run_pid"] is None
        assert obj["rerun_pid"] == []

    def test_created_at_format(self):
        obj = create_task_object("x", "/tmp", "t", {})
        # Should be like "2026-02-12 15:30:00"
        assert len(obj["created_at"]) == 19
        assert "-" in obj["created_at"]
        assert ":" in obj["created_at"]


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator.create_task
# ═══════════════════════════════════════════════════════════════

class TestTaskGeneratorCreateTask:
    def test_creates_folder(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "epochs": 10}
        task = gen.create_task("my-exp", cfg)

        assert os.path.isdir(task["dir"])

    def test_folder_name_matches_prefix(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("baseline", {"lr": 0.01})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("baseline")

    def test_writes_task_info_json(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("exp1", {"lr": 0.01})

        info_path = os.path.join(task["dir"], "task_info.json")
        assert os.path.exists(info_path)
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == "exp1"
        assert info["status"] == "pending"

    def test_writes_config_yaml(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "model": {"name": "resnet"}}
        task = gen.create_task("exp2", cfg)

        cfg_path = os.path.join(task["dir"], "config.yaml")
        assert os.path.exists(cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded["lr"] == 0.01
        assert loaded["model"]["name"] == "resnet"

    def test_writes_run_log(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("exp3", {"x": 1})

        log_path = os.path.join(task["dir"], "run.log")
        assert os.path.exists(log_path)
        with open(log_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "Task initialized" in content

    def test_meta_keys_stripped_from_config(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "_meta_desc": "lr=0.01", "_meta_other": "x"}
        task = gen.create_task("exp4", cfg)

        cfg_path = os.path.join(task["dir"], "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert "_meta_desc" not in loaded
        assert "_meta_other" not in loaded
        assert loaded["lr"] == 0.01

    def test_group_index_in_folder_name(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="[3-of-10]")

        folder = os.path.basename(task["dir"])
        assert "batch-run-[3-of-10]" in folder

    def test_display_name_with_group_index(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="[3-of-10]")
        assert task["name"] == "batch-run-[3-of-10]"

    def test_deduplication_on_name_clash(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        t1 = gen.create_task("same-name", {"x": 1})
        t2 = gen.create_task("same-name", {"x": 2})

        assert t1["dir"] != t2["dir"]
        assert os.path.isdir(t1["dir"])
        assert os.path.isdir(t2["dir"])

    def test_empty_prefix_uses_timestamp(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("", {"x": 1})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("task_")


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator.create_tasks (batch)
# ═══════════════════════════════════════════════════════════════

class TestTaskGeneratorCreateTasks:
    def test_single_config(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        tasks = gen.create_tasks([{"x": 1}], "single")
        assert len(tasks) == 1
        # No index suffix for single task
        assert tasks[0]["name"] == "single"

    def test_multiple_configs_get_index(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = [{"x": i} for i in range(3)]
        tasks = gen.create_tasks(configs, "batch")

        assert len(tasks) == 3
        assert tasks[0]["name"] == "batch-[1-of-3]"
        assert tasks[1]["name"] == "batch-[2-of-3]"
        assert tasks[2]["name"] == "batch-[3-of-3]"

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

    def test_batch_folders_unique(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = [{"x": i} for i in range(5)]
        tasks = gen.create_tasks(configs, "run")

        dirs = [t["dir"] for t in tasks]
        assert len(set(dirs)) == 5  # all unique
