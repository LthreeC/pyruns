"""
Integration tests for the full batch generation flow:
    config_default.yaml → pipe syntax editing → generate_batch_configs → create_tasks

This tests the end-to-end flow that the Generator UI drives.
"""
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
import yaml

from pyruns._config import (
    DEFAULT_ROOT_NAME, TASK_INFO_FILENAME, RECORDS_KEY, ENV_KEY_CONFIG,
)
from pyruns.utils.config_utils import load_yaml, save_yaml
from pyruns.utils.batch_utils import (
    generate_batch_configs, count_batch_configs, strip_batch_pipes,
)
from pyruns.core.task_generator import TaskGenerator


class TestEndToEndBatchFlow:
    """Simulate what happens when a user edits config_default.yaml with pipe syntax
    and clicks GENERATE."""

    def test_full_product_flow(self, tmp_path):
        """User loads config_default, adds product pipes, generates."""
        # 1. Create a config_default.yaml
        root = tmp_path / DEFAULT_ROOT_NAME
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
            assert task["name"] == f"my-exp_[{i+1}-of-6]"
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


# ═══════════════════════════════════════════════════════════════
#  record
# ═══════════════════════════════════════════════════════════════


class TestAddMonitor:
    """Test the public pyruns.record() function."""

    def _make_task_dir(self, tmp_path, start_times=None):
        """Create a minimal task directory with task_info.json."""
        task_dir = str(tmp_path / "test_task")
        os.makedirs(task_dir, exist_ok=True)
        info = {
            "name": "test",
            "status": "running",
            "start_times": start_times or ["2026-01-01 00:00:00"],
            "records": [{} for _ in (start_times or ["2026-01-01 00:00:00"])],
        }
        with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
            json.dump(info, f)
        return task_dir

    def test_basic_add(self, tmp_path, monkeypatch):
        """record should write data to monitors array."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()

        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        import pyruns
        pyruns.record(epoch=1, loss=0.5)

        with open(os.path.join(task_dir, TASK_INFO_FILENAME)) as f:
            info = json.load(f)
        assert RECORDS_KEY in info
        assert len(info[RECORDS_KEY]) == 1
        assert info[RECORDS_KEY][0]["epoch"] == 1
        assert info[RECORDS_KEY][0]["loss"] == 0.5

    def test_merge_same_run(self, tmp_path, monkeypatch):
        """Multiple record calls in same run should merge into one dict."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        import pyruns
        pyruns.record(epoch=1)
        pyruns.record(loss=0.5)
        pyruns.record(acc=92.3)

        with open(os.path.join(task_dir, TASK_INFO_FILENAME)) as f:
            info = json.load(f)
        records = info[RECORDS_KEY]
        assert len(records) == 1  # all merged into run 1
        assert records[0] == {"epoch": 1, "loss": 0.5, "acc": 92.3}

    def test_multi_run(self, tmp_path, monkeypatch):
        """record with multiple start_times should write to correct run slot."""
        task_dir = self._make_task_dir(tmp_path, start_times=[
            "2026-01-01 00:00:00",
            "2026-01-02 00:00:00",
        ])
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        import pyruns
        pyruns.record(epoch=10, loss=0.1)

        with open(os.path.join(task_dir, TASK_INFO_FILENAME)) as f:
            info = json.load(f)
        records = info[RECORDS_KEY]
        assert len(records) == 2  # padded for run 1, data in run 2
        assert records[0] == {}  # empty placeholder for run 1
        assert records[1] == {"epoch": 10, "loss": 0.1}

    def test_get_run_index_uses_current_run_env(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)
        monkeypatch.setenv("PYRUNS_RUN_INDEX", "3")

        import pyruns

        assert pyruns.get_run_index() == 3

    def test_get_artifact_dir_returns_current_run_directory(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)
        monkeypatch.setenv("PYRUNS_RUN_INDEX", "2")

        import pyruns

        artifact_dir = pyruns.get_artifact_dir()

        assert artifact_dir == os.path.join(task_dir, "artifacts", "run2")
        assert os.path.isdir(artifact_dir)

    def test_artifact_dir_returns_current_run_directory(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)
        monkeypatch.setenv("PYRUNS_RUN_INDEX", "2")

        import pyruns

        artifact_dir = pyruns.artifact_dir()
        output = os.path.join(artifact_dir, "metrics.json")
        with open(output, "w", encoding="utf-8") as f:
            f.write("{}")

        assert output == os.path.join(task_dir, "artifacts", "run2", "metrics.json")
        with open(output, encoding="utf-8") as f:
            assert f.read() == "{}"

    def test_get_artifact_dir_has_no_skip_create_option(self):
        import pyruns

        with pytest.raises(TypeError):
            pyruns.get_artifact_dir(create=False)

    def test_get_artifact_dir_uses_cwd_outside_pyruns(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.delenv(ENV_KEY_CONFIG, raising=False)
        monkeypatch.delenv("PYRUNS_RUN_INDEX", raising=False)
        monkeypatch.chdir(tmp_path)

        import pyruns

        artifact_dir = pyruns.get_artifact_dir()

        assert artifact_dir == os.path.join(str(tmp_path), "artifacts", "run1")
        assert os.path.isdir(artifact_dir)

    def test_dict_and_kwargs_combined(self, tmp_path, monkeypatch):
        """record({...}, key=val) should merge both."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        import pyruns
        pyruns.record({"a": 1}, b=2)

        with open(os.path.join(task_dir, TASK_INFO_FILENAME)) as f:
            info = json.load(f)
        assert info[RECORDS_KEY][0] == {"a": 1, "b": 2}

    def test_record_concurrent_updates_do_not_drop_fields(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        import pyruns

        def _write(idx: int):
            pyruns.record(**{f"k{idx}": idx})

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_write, range(8)))

        with open(os.path.join(task_dir, TASK_INFO_FILENAME), encoding="utf-8") as f:
            info = json.load(f)
        row = info[RECORDS_KEY][0]
        for idx in range(8):
            assert row[f"k{idx}"] == idx

    def test_type_error_on_invalid_data(self):
        """record(non_dict) should raise TypeError."""
        import pyruns
        with pytest.raises(TypeError):
            pyruns.record("not a dict")
        with pytest.raises(TypeError):
            pyruns.record(42)

    def test_silent_outside_pyr(self, monkeypatch):
        """record should silently return when PYRUNS_CONFIG is not set."""
        monkeypatch.delenv(ENV_KEY_CONFIG, raising=False)
        import pyruns
        pyruns.record(epoch=1)  # should not raise

    def test_empty_data_ignored(self, tmp_path, monkeypatch):
        """record() with no data should not write anything."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

    def test_basic_track(self, tmp_path, monkeypatch):
        """track should append data to lists in tracks array."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()

        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        from pyruns._config import TRACKS_KEY
        import pyruns

        pyruns.track(loss=0.5)
        pyruns.track(loss=0.4)
        pyruns.track("acc", 0.9)

        with open(os.path.join(task_dir, TASK_INFO_FILENAME)) as f:
            info = json.load(f)
        
        assert TRACKS_KEY in info
        tracks = info[TRACKS_KEY][0]
        assert tracks["loss"] == [0.5, 0.4]
        assert tracks["acc"] == [0.9]

    def test_record_and_track_concurrent_updates_both_survive(self, tmp_path, monkeypatch):
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()

        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)

        import pyruns
        from pyruns._config import TRACKS_KEY

        def _record(idx: int):
            pyruns.record(**{f"metric_{idx}": idx})

        def _track(idx: int):
            pyruns.track(loss=idx)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(_record, i) for i in range(4)]
            futures += [pool.submit(_track, i) for i in range(4)]
            for fut in futures:
                fut.result()

        with open(os.path.join(task_dir, TASK_INFO_FILENAME), encoding="utf-8") as f:
            info = json.load(f)
        for idx in range(4):
            assert info[RECORDS_KEY][0][f"metric_{idx}"] == idx
        assert sorted(info[TRACKS_KEY][0]["loss"]) == [0, 1, 2, 3]

    def test_default_config_path_uses_current_script(self, tmp_path, monkeypatch):
        import pyruns

        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        monkeypatch.setattr("sys.argv", [str(script)])

        path = pyruns._get_default_config_path()

        assert path.endswith(os.path.join("_pyruns_", "train", "config_default.yaml"))

    def test_default_config_path_rejects_invalid_script(self, monkeypatch):
        import pyruns

        monkeypatch.setattr("sys.argv", ["missing.py"])

        with pytest.raises(FileNotFoundError):
            pyruns._get_default_config_path()

    def test_read_prefers_pyruns_config_env(self, tmp_path, monkeypatch):
        import pyruns

        config_path = tmp_path / "task" / "config.yaml"
        config_path.parent.mkdir()
        config_path.write_text("lr: 0.1\n", encoding="utf-8")
        calls = []

        class DummyConfigManager:
            _root = object()

            def read(self, path):
                calls.append(("read", path))
                return {"path": path}

            def load(self):
                return {"loaded": True}

        monkeypatch.setenv(ENV_KEY_CONFIG, str(config_path))
        monkeypatch.setattr(pyruns, "_global_config_manager_", DummyConfigManager())

        assert pyruns.read() == {"path": str(config_path)}
        assert calls == [("read", str(config_path))]

    def test_read_and_load_report_missing_default_config(self, tmp_path, monkeypatch, capsys):
        import pyruns

        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        calls = []

        class DummyConfigManager:
            _root = None

            def read(self, path):
                calls.append(("read", path))
                return {}

            def load(self):
                calls.append(("load",))
                return {}

        monkeypatch.delenv(ENV_KEY_CONFIG, raising=False)
        monkeypatch.setattr("sys.argv", [str(script)])
        monkeypatch.setattr(pyruns, "_global_config_manager_", DummyConfigManager())

        assert pyruns.read() == {}
        assert pyruns.load() == {}
        output = capsys.readouterr().out
        assert "Config not found" in output
        assert "pyr train.py your_config.yaml" in output
        assert calls[-1] == ("load",)

    def test_load_reads_existing_default_config_when_manager_is_empty(self, tmp_path, monkeypatch):
        import pyruns

        script = tmp_path / "train.py"
        script.write_text("print('train')\n", encoding="utf-8")
        default_dir = tmp_path / "_pyruns_" / "train"
        default_dir.mkdir(parents=True)
        default_path = default_dir / "config_default.yaml"
        default_path.write_text("lr: 0.1\n", encoding="utf-8")
        calls = []

        class DummyConfigManager:
            _root = None

            def read(self, path):
                calls.append(("read", path))

            def load(self):
                calls.append(("load",))
                return {"lr": 0.1}

        monkeypatch.chdir(tmp_path)
        monkeypatch.delenv(ENV_KEY_CONFIG, raising=False)
        monkeypatch.setattr("sys.argv", [str(script)])
        monkeypatch.setattr(pyruns, "ROOT_DIR", str(tmp_path / "_pyruns_"))
        monkeypatch.setattr(pyruns, "_global_config_manager_", DummyConfigManager())

        assert pyruns.load() == {"lr": 0.1}
        assert calls[0] == ("read", os.path.join(str(tmp_path / "_pyruns_"), "train", "config_default.yaml"))

    def test_ensure_config_default_creates_and_reuses_file(self, tmp_path):
        import pyruns

        path = pyruns.ensure_config_default(str(tmp_path))
        assert os.path.exists(path)
        assert open(path, encoding="utf-8").read() == "# task config here"

        open(path, "w", encoding="utf-8").write("existing: true\n")
        assert pyruns.ensure_config_default(str(tmp_path)) == path
        assert open(path, encoding="utf-8").read() == "existing: true\n"

    def test_run_index_env_parser_ignores_invalid_values(self, monkeypatch):
        import pyruns

        for raw in ["", "abc", "0", "-1"]:
            monkeypatch.setenv("PYRUNS_RUN_INDEX", raw)
            assert pyruns._get_env_run_index() is None

    def test_get_task_dir_and_run_index_return_none_outside_pyruns(self, monkeypatch):
        import pyruns

        monkeypatch.delenv(ENV_KEY_CONFIG, raising=False)
        monkeypatch.delenv("PYRUNS_RUN_INDEX", raising=False)

        assert pyruns.get_task_dir() is None
        assert pyruns.get_run_index() is None

    def test_track_ignores_empty_updates_and_outside_pyruns(self, tmp_path, monkeypatch):
        import pyruns

        monkeypatch.delenv(ENV_KEY_CONFIG, raising=False)
        pyruns.track("loss", None)
        pyruns.track()

        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)
        before = open(os.path.join(task_dir, TASK_INFO_FILENAME), encoding="utf-8").read()
        pyruns.track("loss", None)
        pyruns.track()
        after = open(os.path.join(task_dir, TASK_INFO_FILENAME), encoding="utf-8").read()
        assert before == after

    def test_record_and_track_retry_after_transient_io_errors(self, tmp_path, monkeypatch):
        import pyruns

        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv(ENV_KEY_CONFIG, config_path)
        record_calls = {"count": 0}
        track_calls = {"count": 0}
        real_update = pyruns.update_task_info

        def flaky_record_update(*args, **kwargs):
            record_calls["count"] += 1
            if record_calls["count"] == 1:
                raise OSError("busy")
            return real_update(*args, **kwargs)

        monkeypatch.setattr(pyruns, "update_task_info", flaky_record_update)
        pyruns.record(loss=0.2)
        assert record_calls["count"] == 2

        def flaky_track_update(*args, **kwargs):
            track_calls["count"] += 1
            if track_calls["count"] == 1:
                raise OSError("busy")
            return real_update(*args, **kwargs)

        monkeypatch.setattr(pyruns, "update_task_info", flaky_track_update)
        pyruns.track(loss=0.3)
        assert track_calls["count"] == 2
