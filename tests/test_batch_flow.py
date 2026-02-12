"""
Integration tests for the full batch generation flow:
    config_default.yaml → pipe syntax editing → generate_batch_configs → create_tasks

This tests the end-to-end flow that the Generator UI drives.
"""
import os
import json
import yaml
import pytest

from pyruns.utils.config_utils import (
    load_yaml,
    save_yaml,
    generate_batch_configs,
    count_batch_configs,
    strip_batch_pipes,
)
from pyruns.core.task_generator import TaskGenerator


class TestEndToEndBatchFlow:
    """Simulate what happens when a user edits config_default.yaml with pipe syntax
    and clicks GENERATE."""

    def test_full_product_flow(self, tmp_path):
        """User loads config_default, adds product pipes, generates."""
        # 1. Create a config_default.yaml
        root = tmp_path / "_pyruns_"
        root.mkdir()
        original = {"lr": 0.001, "epochs": 100, "model": "resnet"}
        save_yaml(str(root / "config_default.yaml"), original)

        # 2. User edits in the UI (simulated by modifying the dict)
        edited = load_yaml(str(root / "config_default.yaml"))
        edited["lr"] = "0.001 | 0.01 | 0.1"  # product: 3 values
        edited["model"] = "resnet | vgg"       # product: 2 values

        # 3. Verify count preview
        assert count_batch_configs(edited) == 6  # 3 × 2

        # 4. Generate configs
        configs = generate_batch_configs(edited)
        assert len(configs) == 6

        # 5. Create tasks
        gen = TaskGenerator(root_dir=str(root))
        tasks = gen.create_tasks(configs, "my-exp")
        assert len(tasks) == 6

        # 6. Verify each task
        lr_values = set()
        model_values = set()
        for i, task in enumerate(tasks):
            assert task["name"] == f"my-exp-[{i+1}-of-6]"
            cfg_path = os.path.join(task["dir"], "config.yaml")
            cfg = load_yaml(cfg_path)
            lr_values.add(cfg["lr"])
            model_values.add(cfg["model"])
            assert cfg["epochs"] == 100  # fixed value preserved

        assert lr_values == {0.001, 0.01, 0.1}
        assert model_values == {"resnet", "vgg"}

        # 7. Verify config_default.yaml was NOT modified
        reloaded = load_yaml(str(root / "config_default.yaml"))
        assert reloaded == original

    def test_full_zip_flow(self, tmp_path):
        """User uses zip syntax for paired parameters."""
        root = tmp_path / "_pyruns_"
        root.mkdir()

        edited = {
            "seed": "(42 | 123 | 999)",
            "tag": "(exp_a | exp_b | exp_c)",
            "lr": 0.01,
        }

        assert count_batch_configs(edited) == 3
        configs = generate_batch_configs(edited)
        assert len(configs) == 3

        # Verify zip pairing
        assert configs[0]["seed"] == 42
        assert configs[0]["tag"] == "exp_a"
        assert configs[1]["seed"] == 123
        assert configs[1]["tag"] == "exp_b"
        assert configs[2]["seed"] == 999
        assert configs[2]["tag"] == "exp_c"

        # All share the same fixed lr
        assert all(c["lr"] == 0.01 for c in configs)

    def test_full_mixed_flow(self, tmp_path):
        """User combines product and zip syntax."""
        root = tmp_path / "_pyruns_"
        root.mkdir()

        edited = {
            "lr": "0.001 | 0.01",         # product: 2
            "seed": "(1 | 2 | 3)",          # zip: 3
            "tag": "(a | b | c)",           # zip: 3
            "optimizer": "adam",             # fixed
        }

        assert count_batch_configs(edited) == 6  # 2 × 3
        configs = generate_batch_configs(edited)
        assert len(configs) == 6

        gen = TaskGenerator(root_dir=str(root))
        tasks = gen.create_tasks(configs, "grid-search")
        assert len(tasks) == 6

        for task in tasks:
            assert os.path.isdir(task["dir"])
            cfg = load_yaml(os.path.join(task["dir"], "config.yaml"))
            assert cfg["optimizer"] == "adam"
            assert isinstance(cfg["lr"], float)
            assert isinstance(cfg["seed"], int)

    def test_no_pipes_single_task(self, tmp_path):
        """No pipe syntax → generates exactly 1 task, no suffix."""
        root = tmp_path / "_pyruns_"
        root.mkdir()

        cfg = {"lr": 0.01, "epochs": 50}
        configs = generate_batch_configs(cfg)
        assert len(configs) == 1

        gen = TaskGenerator(root_dir=str(root))
        tasks = gen.create_tasks(configs, "single-run")
        assert len(tasks) == 1
        assert tasks[0]["name"] == "single-run"

    def test_yaml_roundtrip_preserves_pipes(self, tmp_path):
        """Pipe syntax survives YAML save → load roundtrip."""
        path = str(tmp_path / "test.yaml")
        cfg = {
            "lr": "0.001 | 0.01 | 0.1",
            "seed": "(1 | 2 | 3)",
            "fixed": 42,
        }
        save_yaml(path, cfg)
        loaded = load_yaml(path)

        # Pipe strings should survive YAML roundtrip
        assert loaded["lr"] == "0.001 | 0.01 | 0.1"
        assert loaded["seed"] == "(1 | 2 | 3)"
        assert loaded["fixed"] == 42

        # And batch generation should still work
        assert count_batch_configs(loaded) == 3 * 3  # 3 product × 3 zip

    def test_nested_config_batch(self, tmp_path):
        """Pipe syntax works inside nested dicts."""
        root = tmp_path / "_pyruns_"
        root.mkdir()

        cfg = {
            "training": {
                "lr": "0.001 | 0.01",
                "epochs": 100,
            },
            "model": {
                "name": "resnet | vgg | efficientnet",
                "dropout": 0.5,
            },
        }

        assert count_batch_configs(cfg) == 6  # 2 × 3
        configs = generate_batch_configs(cfg)

        gen = TaskGenerator(root_dir=str(root))
        tasks = gen.create_tasks(configs, "nested-exp")
        assert len(tasks) == 6

        for task in tasks:
            c = load_yaml(os.path.join(task["dir"], "config.yaml"))
            assert c["training"]["epochs"] == 100
            assert c["model"]["dropout"] == 0.5
            assert c["model"]["name"] in ["resnet", "vgg", "efficientnet"]

    def test_strip_batch_pipes_for_preview(self):
        """strip_batch_pipes takes only the first value from each pipe."""
        cfg = {
            "lr": "0.001 | 0.01 | 0.1",
            "model": {"name": "resnet | vgg"},
            "seed": "(42 | 123)",
            "fixed": "hello",
        }
        stripped = strip_batch_pipes(cfg)
        assert stripped["lr"] == 0.001
        assert stripped["model"]["name"] == "resnet"
        assert stripped["seed"] == 42
        assert stripped["fixed"] == "hello"

