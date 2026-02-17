"""
Tests for pyruns.add_monitor — run-aggregated monitor data API.
"""
import os
import json
import pytest

from pyruns._config import INFO_FILENAME, MONITOR_KEY


class TestAddMonitor:
    """Test the public pyruns.add_monitor() function."""

    def _make_task_dir(self, tmp_path, start_times=None):
        """Create a minimal task directory with task_info.json."""
        task_dir = str(tmp_path / "test_task")
        os.makedirs(task_dir, exist_ok=True)
        info = {
            "name": "test",
            "status": "running",
            "start_times": start_times or ["2026-01-01 00:00:00"],
        }
        with open(os.path.join(task_dir, INFO_FILENAME), "w") as f:
            json.dump(info, f)
        return task_dir

    def test_basic_add(self, tmp_path, monkeypatch):
        """add_monitor should write data to monitors array."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()

        monkeypatch.setenv("PYRUNS_CONFIG", config_path)

        import pyruns
        pyruns.add_monitor(epoch=1, loss=0.5)

        with open(os.path.join(task_dir, INFO_FILENAME)) as f:
            info = json.load(f)
        assert MONITOR_KEY in info
        assert len(info[MONITOR_KEY]) == 1
        assert info[MONITOR_KEY][0]["epoch"] == 1
        assert info[MONITOR_KEY][0]["loss"] == 0.5

    def test_merge_same_run(self, tmp_path, monkeypatch):
        """Multiple add_monitor calls in same run should merge into one dict."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv("PYRUNS_CONFIG", config_path)

        import pyruns
        pyruns.add_monitor(epoch=1)
        pyruns.add_monitor(loss=0.5)
        pyruns.add_monitor(acc=92.3)

        with open(os.path.join(task_dir, INFO_FILENAME)) as f:
            info = json.load(f)
        monitors = info[MONITOR_KEY]
        assert len(monitors) == 1  # all merged into run 1
        assert monitors[0] == {"epoch": 1, "loss": 0.5, "acc": 92.3}

    def test_multi_run(self, tmp_path, monkeypatch):
        """add_monitor with multiple start_times should write to correct run slot."""
        task_dir = self._make_task_dir(tmp_path, start_times=[
            "2026-01-01 00:00:00",
            "2026-01-02 00:00:00",
        ])
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv("PYRUNS_CONFIG", config_path)

        import pyruns
        pyruns.add_monitor(epoch=10, loss=0.1)

        with open(os.path.join(task_dir, INFO_FILENAME)) as f:
            info = json.load(f)
        monitors = info[MONITOR_KEY]
        assert len(monitors) == 2  # padded for run 1, data in run 2
        assert monitors[0] == {}  # empty placeholder for run 1
        assert monitors[1] == {"epoch": 10, "loss": 0.1}

    def test_dict_and_kwargs_combined(self, tmp_path, monkeypatch):
        """add_monitor({...}, key=val) should merge both."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv("PYRUNS_CONFIG", config_path)

        import pyruns
        pyruns.add_monitor({"a": 1}, b=2)

        with open(os.path.join(task_dir, INFO_FILENAME)) as f:
            info = json.load(f)
        assert info[MONITOR_KEY][0] == {"a": 1, "b": 2}

    def test_type_error_on_invalid_data(self):
        """add_monitor(non_dict) should raise TypeError."""
        import pyruns
        with pytest.raises(TypeError):
            pyruns.add_monitor("not a dict")
        with pytest.raises(TypeError):
            pyruns.add_monitor(42)

    def test_silent_outside_pyr(self, monkeypatch):
        """add_monitor should silently return when PYRUNS_CONFIG is not set."""
        monkeypatch.delenv("PYRUNS_CONFIG", raising=False)
        import pyruns
        pyruns.add_monitor(epoch=1)  # should not raise

    def test_empty_data_ignored(self, tmp_path, monkeypatch):
        """add_monitor() with no data should not write anything."""
        task_dir = self._make_task_dir(tmp_path)
        config_path = os.path.join(task_dir, "config.yaml")
        open(config_path, "w").close()
        monkeypatch.setenv("PYRUNS_CONFIG", config_path)

        import pyruns
        pyruns.add_monitor()

        with open(os.path.join(task_dir, INFO_FILENAME)) as f:
            info = json.load(f)
        assert MONITOR_KEY not in info
