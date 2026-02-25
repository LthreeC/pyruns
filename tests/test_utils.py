"""
Tests for pyruns.utils — parse_utils, log_io, process_utils, sort_utils,
settings, info_io, config_utils, batch_utils, and validation.
"""
import json
import os
import signal
import sys

import pytest
import yaml
from unittest.mock import patch, MagicMock

import pyruns.utils.settings as settings
from pyruns._config import (
    DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME,
    SETTINGS_FILENAME, TASK_INFO_FILENAME, RUN_LOG_DIR, MONITOR_KEY,
    BATCH_ESCAPE,
)
from pyruns.utils.batch_utils import (
    _parse_pipe_value, _split_by_pipe,
    generate_batch_configs, count_batch_configs, strip_batch_pipes,
)
from pyruns.utils.config_utils import (
    safe_filename, parse_value, flatten_dict, unflatten_dict,
    load_yaml, save_yaml, list_yaml_files, list_template_files,
    preview_config_line, validate_config_types_against_template,
)
from pyruns.utils.log_io import (
    append_log, read_log, read_log_chunk, read_last_bytes, safe_read_log,
)
from pyruns.utils.parse_utils import (
    detect_config_source_fast, extract_argparse_params,
    argparse_params_to_dict, resolve_config_path, generate_config_file,
)
from pyruns.utils.process_utils import is_pid_running, kill_process
from pyruns.utils.sort_utils import task_sort_key, filter_tasks
from pyruns.utils.info_io import (
    load_task_info, save_task_info, load_monitor_data,
    get_log_options, resolve_log_path, validate_task_name,
)


def test_detect_config_source_fast(tmp_path):
    # 1. pyruns_read
    p_read = tmp_path / "read.py"
    p_read.write_text("import pyruns\nconfig = pyruns.read('my_cfg.yaml')\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_read)) == ("pyruns_read", "my_cfg.yaml")

    # 1b. pyruns_read default
    p_read2 = tmp_path / "read2.py"
    p_read2.write_text("import pyruns\nconfig = pyruns.read()\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_read2)) == ("pyruns_read", None)

    # 2. pyruns_load
    p_load = tmp_path / "load.py"
    p_load.write_text("import pyruns\nconfig = pyruns.load()\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_load)) == ("pyruns_load", None)

    # 3. argparse
    p_arg = tmp_path / "arg.py"
    p_arg.write_text("import argparse\nparser.add_argument('--lr', type=float, default=0.01)\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_arg)) == ("argparse", None)

    # 4. unknown
    p_unk = tmp_path / "unk.py"
    p_unk.write_text("print('hello world')", encoding="utf-8")
    assert detect_config_source_fast(str(p_unk)) == ("unknown", None)


def test_extract_argparse_params(tmp_path):
    p_script = tmp_path / "demo.py"
    code = """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument('--lr', type=float, default=0.01, help='learning rate')
parser.add_argument('--epochs', type=int, default=10)
parser.add_argument('-b', '--batch-size', default=32)
"""
    p_script.write_text(code, encoding="utf-8")
    
    params = extract_argparse_params(str(p_script))
    assert list(params.keys()) == ["lr", "epochs", "batch_size"]
    
    assert params["lr"]["name"] == "--lr"
    assert params["lr"]["default"] == 0.01
    assert params["lr"]["help"] == "learning rate"
    
    assert params["batch_size"]["name"] == "--batch-size"
    assert params["batch_size"]["default"] == 32


def test_argparse_params_to_dict():
    params = {
        "lr": {"name": "--lr", "default": 0.01},
        "epochs": {"name": "--epochs", "default": 10},
        "no_default": {"name": "--flag"},
    }
    d = argparse_params_to_dict(params)
    assert d == {"lr": 0.01, "epochs": 10, "no_default": None}


def test_resolve_config_path(tmp_path):
    cwd = os.getcwd()
    script_dir = str(tmp_path)
    
    # Setup test structure
    cfg_script = tmp_path / "cfg_script.yaml"
    cfg_cwd = os.path.join(cwd, "cfg_cwd.yaml")
    
    cfg_script.write_text("foo: 1")
    with open(cfg_cwd, "w") as f:
        f.write("bar: 1")
    
    try:
        # Relative to script dir
        res_script = resolve_config_path("cfg_script.yaml", script_dir)
        assert res_script == str(cfg_script)
        
        # Absolute path
        res_abs = resolve_config_path(str(cfg_script), script_dir)
        assert res_abs == str(cfg_script)
        
        # Relative to cwd
        res_cwd = resolve_config_path("cfg_cwd.yaml", script_dir)
        assert res_cwd == cfg_cwd
        
        # Not found
        assert resolve_config_path("not_exist.yaml", script_dir) is None
        
    finally:
        if os.path.exists(cfg_cwd):
            os.remove(cfg_cwd)


def test_generate_config_file(tmp_path):
    p_script = tmp_path / "my_script.py"
    params = {
        "lr": {"name": "--lr", "default": 0.01, "help": "learning rate"},
        "epochs": {"name": "--epochs", "default": 10},
    }
    
    pyruns_dir = os.path.join(str(tmp_path), DEFAULT_ROOT_NAME, "my_script")
    pyruns_dir_res = generate_config_file(pyruns_dir, str(p_script), params)
    assert pyruns_dir_res == pyruns_dir
    assert os.path.basename(pyruns_dir_res) == "my_script"
    assert os.path.basename(os.path.dirname(pyruns_dir_res)) == DEFAULT_ROOT_NAME
    
    cfg_path = os.path.join(pyruns_dir_res, CONFIG_DEFAULT_FILENAME)
    assert os.path.exists(cfg_path)
    
    with open(cfg_path, "r", encoding="utf-8") as f:
        text = f.read()
    
    assert "lr: 0.01  # learning rate" in text
    assert "epochs: 10" in text
    assert "Auto-generated for my_script.py" in text


# ═══════════════════════════════════════════════════════════════
#  log_io
# ═══════════════════════════════════════════════════════════════


def test_append_read_log(tmp_path):
    log_file = str(tmp_path / "test.log")
    
    # Read non-existent
    assert read_log(log_file) == ""
    
    # Append creates file
    append_log(log_file, "Line 1\n")
    assert read_log(log_file) == "Line 1\n"
    
    # Append adds
    append_log(log_file, "Line 2\n")
    assert read_log(log_file) == "Line 1\nLine 2\n"


def test_read_log_chunk(tmp_path):
    log_file = str(tmp_path / "test.log")
    
    # Non existent
    assert read_log_chunk(log_file, 0) == ("", 0)
    
    # Write some data
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("A" * 10)
    
    # Read chunk from offset 0
    text1, off1 = read_log_chunk(log_file, 0)
    assert text1.replace("\r", "") == "A" * 10
    assert off1 == 10
    
    # Append more data
    with open(log_file, "a", encoding="utf-8", newline="\n") as f:
        f.write("B" * 5)
    
    # Read from previous offset
    text2, off2 = read_log_chunk(log_file, off1)
    assert text2.replace("\r", "") == "B" * 5
    assert off2 == 15
    
    # Truncate file simulating log rotation/overwrite
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("C" * 3)
        
    # Read from offset 15 -> should reset to 0 because size is now 3
    text3, off3 = read_log_chunk(log_file, off2)
    assert text3.replace("\r", "") == "C" * 3
    assert off3 == 3


def test_read_last_bytes(tmp_path):
    log_file = str(tmp_path / "test.log")
    
    assert read_last_bytes(log_file) == ("", 0)
    
    content = "Hello\nWorld\n" * 100 # len=1200
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
        
    text, offset = read_last_bytes(log_file, 12)
    assert len(text) == 12
    assert "World\n" in text.replace("\r", "")
    assert offset == len(content)
    
    # Ask for more than available
    text2, offset2 = read_last_bytes(log_file, 2000)
    assert text2.replace("\r", "") == content
    assert offset2 == len(content)


def test_safe_read_log(tmp_path):
    log_file = str(tmp_path / "test.log")
    assert safe_read_log(log_file, 0) == ("", 0)
    
    # safe_read_log reads max_bytes and falls back to last newline
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("Line 1\nLine 2\nLine 3 no newline yet")
        
    text, off = safe_read_log(log_file, 0, max_bytes=100)
    # If the file doesn't exceed max_bytes, it just returns all of it
    assert text.replace("\r", "") == "Line 1\nLine 2\nLine 3 no newline yet"
    assert off == 35
    
    # Now write a long string to trigger fallback
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write("A" * 100 + "\n" + "B" * 100)
        
    text2, off2 = safe_read_log(log_file, 0, max_bytes=150)
    # It reads 150 bytes, which lands in the B's.
    # Because there is a newline at index 100, it should fallback to there.
    # Return string will be up to the newline.
    assert text2.replace("\r", "") == "A" * 100 + "\n"
    assert off2 == 101 # exactly after the newline


# ═══════════════════════════════════════════════════════════════
#  process_utils
# ═══════════════════════════════════════════════════════════════


def test_is_pid_running_invalid():
    assert not is_pid_running(None)
    assert not is_pid_running("not_a_pid")
    assert not is_pid_running(0) # Depending on OS, 0 might mean something, but usually handled as invalid or special. We just test logic.

def test_is_pid_running_self():
    # The current process should definitely be running
    my_pid = os.getpid()
    assert is_pid_running(my_pid)


@patch("pyruns.utils.process_utils.os.name", "posix")
@patch("pyruns.utils.process_utils.os.kill")
def test_is_pid_running_mock_posix(mock_kill):
    # Test True
    mock_kill.return_value = None
    assert is_pid_running(99999) is True
    mock_kill.assert_called_with(99999, 0)
    
    # Test False (Exception)
    mock_kill.side_effect = ProcessLookupError()
    assert is_pid_running(99999) is False


@pytest.mark.skipif(os.name != "nt", reason="ctypes.windll only available on Windows")
@patch("pyruns.utils.process_utils.os.name", "nt")
@patch("ctypes.windll.kernel32.OpenProcess")
def test_is_pid_running_mock_nt_false(mock_open):
    # If OpenProcess returns 0, it should return False
    mock_open.return_value = 0 # Handle is 0/None -> not running
    assert is_pid_running(99999) is False
    mock_open.assert_called_with(0x00100000, False, 99999)


@pytest.mark.skipif(os.name != "nt", reason="ctypes.windll only available on Windows")
@patch("pyruns.utils.process_utils.os.name", "nt")
@patch("ctypes.windll.kernel32.CloseHandle")
@patch("ctypes.windll.kernel32.OpenProcess")
def test_is_pid_running_mock_nt_true(mock_open, mock_close):
    mock_open.return_value = 1234 # Got a handle
    assert is_pid_running(99999) is True
    mock_close.assert_called_with(1234)


@patch("pyruns.utils.process_utils.os.name", "posix")
@patch("os.kill")
def test_kill_process_posix(mock_kill):
    kill_process(99999)
    mock_kill.assert_called_with(99999, signal.SIGTERM)


@patch("pyruns.utils.process_utils.os.name", "nt")
@patch("subprocess.run")
def test_kill_process_nt(mock_run):
    kill_process(99999)
    mock_run.assert_called_with(
        ["taskkill", "/F", "/T", "/PID", "99999"],
        capture_output=True, timeout=5,
    )


def test_kill_process_exception_caught():
    # Just to ensure it doesn't raise if the underlying call fails
    with patch("pyruns.utils.process_utils.os.name", "posix"):
        with patch("os.kill", side_effect=Exception("mock error")):
            # Should not raise exception
            kill_process(99999)


# ═══════════════════════════════════════════════════════════════
#  sort_utils
# ═══════════════════════════════════════════════════════════════


def test_task_sort_key():
    # Priority 1: last element of start_times
    task1 = {
        "start_times": ["2023-10-01", "2023-10-05"],
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task1) == (10, "2023-10-05")  # default status is pending (10), ts is string

    # Priority 2: created_at, if start_times is empty or missing
    task2 = {
        "start_times": [],
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task2) == (10, "2023-10-02")

    task3 = {
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task3) == (10, "2023-10-02")

    # Default: empty string if neither are present
    task4 = {}
    assert task_sort_key(task4) == (10, "")

    # Bad data type fallback
    task5 = {
        "start_times": "not_a_list",
        "created_at": "2023-10-02"
    }
    # It checks isinstance(list), so it should fallback to created_at
    assert task_sort_key(task5) == (10, "2023-10-02")
    
    # Priority Top: Running/Queued tasks return inverted int
    task6 = {
        "status": "running",
        "start_times": ["2023-10-06"],
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task6) == (50, -20231006)
    
    task7 = {
        "status": "completed",
        "start_times": ["2023-10-07"]
    }
    assert task_sort_key(task7) == (20, "2023-10-07")


# ═══════════════════════════════════════════════════════════════
#  settings
# ═══════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def clean_cache():
    # Before each test
    settings._cached.clear()
    yield
    # After each test
    settings._cached.clear()


def test_ensure_settings_file(tmp_path):
    root_dir = str(tmp_path)
    file_path = os.path.join(root_dir, SETTINGS_FILENAME)
    
    # 1. File doesn't exist
    assert not os.path.exists(file_path)
    res_path = settings.ensure_settings_file(root_dir)
    assert res_path == file_path
    assert os.path.exists(file_path)
    
    # 2. File exists (should not overwrite)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("custom_key: 123")
    settings.ensure_settings_file(root_dir)
    with open(file_path, "r", encoding="utf-8") as f:
        assert f.read() == "custom_key: 123"


def test_load_settings(tmp_path):
    root_dir = str(tmp_path)
    file_path = os.path.join(root_dir, SETTINGS_FILENAME)
    
    # Fallback to defaults if file doesn't exist
    cfg = settings.load_settings(root_dir)
    assert cfg["ui_port"] == settings._DEFAULTS["ui_port"]
    assert cfg == settings._cached
    
    # Custom values
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump({"ui_port": 9999, "new_key": "abc"}, f)
        
    cfg2 = settings.load_settings(root_dir)
    assert cfg2["ui_port"] == 9999
    assert cfg2["new_key"] == "abc"
    # Defaults still present
    assert cfg2["manager_columns"] == settings._DEFAULTS["manager_columns"]


def test_get():
    # Requires an active mock of _config or just passing the cache
    settings._cached = {"ui_port": 1234, "foo": "bar"}
    assert settings.get("ui_port") == 1234
    assert settings.get("foo") == "bar"
    assert settings.get("missing_key", "default_val") == "default_val"
    
    # Fallback when cached is empty
    settings._cached.clear()
    # It attempts to load from ROOT_DIR. To avoid actual IO in tests that mock ROOT_DIR,
    # we just intercept ROOT_DIR or let it fallback to _DEFAULTS silently
    with patch("pyruns.utils.settings.ROOT_DIR", "/fake/dir"):
        assert settings.get("ui_port") == settings._DEFAULTS["ui_port"]


def test_save_setting(tmp_path):
    root_dir = str(tmp_path)
    file_path = os.path.join(root_dir, SETTINGS_FILENAME)
    
    with patch("pyruns.utils.settings.ROOT_DIR", root_dir):
        # 1. New file
        settings.save_setting("ui_port", 7777)
        assert os.path.exists(file_path)
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            assert "ui_port: 7777" in text
            
        # 2. Existing file update single value
        settings.save_setting("ui_port", 8888)
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            assert "ui_port: 8888" in text
            assert "7777" not in text
            
        # 3. List serialization
        settings.save_setting("pinned_params", ["a", "b"])
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            assert "pinned_params: \n- a\n- b" in text
            
        assert settings._cached["pinned_params"] == ["a", "b"]
        
        # 4. Update the list again
        settings.save_setting("pinned_params", ["c"])
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            assert "pinned_params: \n- c" in text
            assert "- a" not in text


# ═══════════════════════════════════════════════════════════════
#  info_io — task_info/script_info I/O, monitor data, log options
# ═══════════════════════════════════════════════════════════════


class TestLoadSaveTaskInfo:
    def test_roundtrip(self, tmp_path):
        task_dir = str(tmp_path)
        info = {"name": "test", "status": "pending", "extra": [1, 2, 3]}
        save_task_info(task_dir, info)
        loaded = load_task_info(task_dir)
        assert loaded == info

    def test_load_missing_file(self, tmp_path):
        assert load_task_info(str(tmp_path)) == {}

    def test_load_corrupt_json(self, tmp_path):
        path = os.path.join(str(tmp_path), TASK_INFO_FILENAME)
        with open(path, "w") as f:
            f.write("{invalid json")
        assert load_task_info(str(tmp_path)) == {}

    def test_load_corrupt_raises_when_requested(self, tmp_path):
        path = os.path.join(str(tmp_path), TASK_INFO_FILENAME)
        with open(path, "w") as f:
            f.write("{invalid json")
        with pytest.raises(json.JSONDecodeError):
            load_task_info(str(tmp_path), raise_error=True)

    def test_unicode_support(self, tmp_path):
        task_dir = str(tmp_path)
        info = {"name": "测试任务", "description": "中文描述 🧪"}
        save_task_info(task_dir, info)
        loaded = load_task_info(task_dir)
        assert loaded == info


class TestLoadMonitorData:
    def test_with_monitors(self, tmp_path):
        task_dir = str(tmp_path)
        info = {MONITOR_KEY: [{"loss": 0.5}, {"loss": 0.1}]}
        save_task_info(task_dir, info)
        data = load_monitor_data(task_dir)
        assert len(data) == 2
        assert data[0]["loss"] == 0.5

    def test_without_monitors(self, tmp_path):
        task_dir = str(tmp_path)
        save_task_info(task_dir, {"name": "test"})
        assert load_monitor_data(task_dir) == []

    def test_missing_file(self, tmp_path):
        assert load_monitor_data(str(tmp_path)) == []


class TestGetLogOptions:
    def test_run_logs(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOG_DIR)
        os.makedirs(log_dir)
        for name in ["run1.log", "run2.log", "run10.log"]:
            open(os.path.join(log_dir, name), "w").close()

        opts = get_log_options(task_dir)
        keys = list(opts.keys())
        assert keys == ["run1.log", "run2.log", "run10.log"]
        assert all(os.path.isfile(p) for p in opts.values())

    def test_no_logs(self, tmp_path):
        assert get_log_options(str(tmp_path)) == {}


class TestResolveLogPath:
    def test_resolve_named(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOG_DIR)
        os.makedirs(log_dir)
        path = os.path.join(log_dir, "run1.log")
        open(path, "w").close()

        result = resolve_log_path(task_dir, "run1.log")
        assert result == path

    def test_resolve_latest(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOG_DIR)
        os.makedirs(log_dir)
        # Create run1.log with an older timestamp
        p1 = os.path.join(log_dir, "run1.log")
        open(p1, "w").close()
        old_time = 1000000
        os.utime(p1, (old_time, old_time))

        # Create run2.log with a newer timestamp
        p2 = os.path.join(log_dir, "run2.log")
        open(p2, "w").close()
        new_time = 2000000
        os.utime(p2, (new_time, new_time))

        result = resolve_log_path(task_dir)
        assert result.endswith("run2.log")

    def test_resolve_no_logs(self, tmp_path):
        assert resolve_log_path(str(tmp_path)) is None


# ═══════════════════════════════════════════════════════════════
#  config_utils — core config & batch generation logic
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  safe_filename
# ═══════════════════════════════════════════════════════════════

class TestSafeFilename:
    def test_normal(self):
        assert safe_filename("hello-world") == "hello-world"

    def test_spaces_replaced(self):
        assert safe_filename("my file name") == "my_file_name"

    def test_special_chars_stripped(self):
        assert safe_filename("a/b:c*d") == "abcd"

    def test_empty_string(self):
        assert safe_filename("") == "config"

    def test_only_bad_chars(self):
        assert safe_filename("***") == "config"


# ═══════════════════════════════════════════════════════════════
#  parse_value
# ═══════════════════════════════════════════════════════════════

class TestParseValue:
    def test_int(self):
        assert parse_value("42") == 42

    def test_float(self):
        assert parse_value("3.14") == 3.14

    def test_bool_true(self):
        assert parse_value("True") is True
        assert parse_value("true") is True

    def test_bool_false(self):
        assert parse_value("False") is False
        assert parse_value("false") is False

    def test_list(self):
        assert parse_value("[1, 2, 3]") == [1, 2, 3]

    def test_string(self):
        assert parse_value("hello world") == "hello world"

    def test_pipe_string_stays_string(self):
        """Pipe syntax should NOT be parsed as a value — stays as string."""
        result = parse_value("0.001 | 0.01 | 0.1")
        assert isinstance(result, str)
        assert result == "0.001 | 0.01 | 0.1"


# ═══════════════════════════════════════════════════════════════
#  flatten / unflatten
# ═══════════════════════════════════════════════════════════════

class TestFlattenUnflatten:
    def test_flat_dict(self):
        d = {"a": 1, "b": 2}
        assert flatten_dict(d) == {"a": 1, "b": 2}

    def test_nested(self):
        d = {"model": {"name": "resnet", "layers": 50}, "lr": 0.01}
        flat = flatten_dict(d)
        assert flat == {"model.name": "resnet", "model.layers": 50, "lr": 0.01}

    def test_roundtrip(self):
        original = {"a": {"b": {"c": 1}}, "x": 2}
        assert unflatten_dict(flatten_dict(original)) == original

    def test_unflatten_simple(self):
        flat = {"a.b": 1, "a.c": 2, "d": 3}
        assert unflatten_dict(flat) == {"a": {"b": 1, "c": 2}, "d": 3}

    def test_get_nested(self):
        from pyruns.utils.config_utils import get_nested
        data = {"a": {"b": {"c": 1}}, "x": 2}
        
        # Exact match
        pd, k, v = get_nested(data, "a.b.c")
        assert k == "c"
        assert v == 1
        assert pd == {"c": 1}
        
        # Exact match single
        pd, k, v = get_nested(data, "x")
        assert k == "x"
        assert v == 2
        
        # Missing key
        pd, k, v = get_nested(data, "a.b.d")
        assert pd is None

        # Missing parent
        pd, k, v = get_nested(data, "a.z.c")
        assert pd is None

        # Parent is not a dict
        pd, k, v = get_nested(data, "x.y")
        assert pd is None


# ═══════════════════════════════════════════════════════════════
#  YAML / JSON I/O
# ═══════════════════════════════════════════════════════════════

class TestYamlIO:
    def test_save_and_load(self, tmp_dir):
        data = {"lr": 0.01, "model": {"name": "vgg"}}
        path = str(tmp_dir / "test.yaml")
        save_yaml(path, data)
        loaded = load_yaml(path)
        assert loaded == data

    def test_load_nonexistent(self, tmp_dir):
        assert load_yaml(str(tmp_dir / "nope.yaml")) == {}

    def test_load_non_dict_yaml(self, tmp_dir):
        path = str(tmp_dir / "list.yaml")
        with open(path, "w") as f:
            f.write("- a\n- b\n")
        assert load_yaml(path) == {}

    def test_list_yaml_files(self, tmp_dir):
        for name in ["a.yaml", "b.yml", "c.txt"]:
            (tmp_dir / name).write_text("x: 1")
        result = list_yaml_files(str(tmp_dir))
        assert "a.yaml" in result
        assert "b.yml" in result
        assert "c.txt" not in result

    def test_list_yaml_files_missing_dir(self):
        assert list_yaml_files("/nonexistent/dir") == []


class TestTaskInfoIO:
    def test_save_and_load(self, tmp_dir):
        info = {"name": "test", "status": "pending", "env": {"CUDA": "0"}}
        save_task_info(str(tmp_dir), info)
        loaded = load_task_info(str(tmp_dir))
        assert loaded == info

    def test_load_missing(self, tmp_dir):
        assert load_task_info(str(tmp_dir)) == {}


# ═══════════════════════════════════════════════════════════════
#  list_template_files
# ═══════════════════════════════════════════════════════════════

class TestListTemplateFiles:
    def test_with_task_subfolder(self, tmp_path):
        run_root = str(tmp_path)
        tasks_dir = os.path.join(run_root, "tasks")
        
        # Create a task subfolder with config.yaml inside `tasks/`
        task_dir = os.path.join(tasks_dir, "my-task")
        os.makedirs(task_dir, exist_ok=True)
        save_yaml(os.path.join(task_dir, "config.yaml"), {"x": 1})

        result = list_template_files(run_root)
        # the key is now tasks/my-task/config.yaml using forward slashes
        assert "tasks/my-task/config.yaml" in result

    def test_skips_dot_dirs(self, tmp_path):
        run_root = str(tmp_path)
        tasks_dir = os.path.join(run_root, "tasks")
        
        trash = os.path.join(tasks_dir, ".trash")
        os.makedirs(trash, exist_ok=True)
        save_yaml(os.path.join(trash, "config.yaml"), {"x": 1})

        result = list_template_files(run_root)
        # .trash should be skipped
        for key in result:
            assert ".trash" not in key

    def test_nonexistent_dir(self):
        assert list_template_files("/nonexistent") == {}


# ═══════════════════════════════════════════════════════════════
#  preview_config_line
# ═══════════════════════════════════════════════════════════════

class TestPreviewConfigLine:
    def test_basic(self):
        cfg = {"lr": 0.01, "bs": 32, "opt": "adam"}
        line = preview_config_line(cfg)
        assert "lr=0.01" in line
        assert "bs=32" in line

    def test_skips_dicts(self):
        cfg = {"lr": 0.01, "model": {"name": "resnet"}}
        line = preview_config_line(cfg)
        assert "model" not in line

    def test_max_items(self):
        cfg = {f"k{i}": i for i in range(10)}
        line = preview_config_line(cfg, max_items=2)
        assert line.count("=") == 2

    def test_non_dict_input(self):
        assert preview_config_line("not a dict") == ""


# ═══════════════════════════════════════════════════════════════
#  _parse_pipe_value
# ═══════════════════════════════════════════════════════════════

class TestParsePipeValue:
    def test_product_syntax(self):
        result = _parse_pipe_value("0.001 | 0.01 | 0.1")
        assert result is not None
        parts, mode = result
        assert mode == "product"
        assert parts == ["0.001", "0.01", "0.1"]

    def test_zip_syntax(self):
        result = _parse_pipe_value("(a | b | c)")
        assert result is not None
        parts, mode = result
        assert mode == "zip"
        assert parts == ["a", "b", "c"]

    def test_no_pipe(self):
        assert _parse_pipe_value("hello") is None
        assert _parse_pipe_value("0.001") is None

    def test_non_string(self):
        assert _parse_pipe_value(42) is None
        assert _parse_pipe_value([1, 2]) is None
        assert _parse_pipe_value(True) is None

    def test_single_value_in_parens(self):
        """(single_value) with no pipe should return None."""
        assert _parse_pipe_value("(only_one)") is None

    def test_single_value_with_pipe_in_parens(self):
        result = _parse_pipe_value("(a | b)")
        assert result is not None
        assert result[1] == "zip"

    def test_whitespace(self):
        result = _parse_pipe_value("  a | b | c  ")
        assert result is not None
        parts, mode = result
        assert mode == "product"
        assert parts == ["a", "b", "c"]


# ═══════════════════════════════════════════════════════════════
#  generate_batch_configs
# ═══════════════════════════════════════════════════════════════

class TestGenerateBatchConfigs:
    def test_no_pipes_returns_original(self, sample_config):
        """No pipe syntax → returns [original_config]."""
        configs = generate_batch_configs(sample_config)
        assert len(configs) == 1
        assert configs[0] is sample_config

    def test_product_only(self, sample_config_with_pipes):
        """Product: 3 × 2 = 6 configs."""
        configs = generate_batch_configs(sample_config_with_pipes)
        assert len(configs) == 6
        # Each config should have typed values, not pipe strings
        for c in configs:
            assert isinstance(c["lr"], (int, float))
            assert isinstance(c["batch_size"], (int, float))
            assert c["optimizer"] == "adam"  # fixed value preserved

    def test_product_values_correct(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        lr_values = sorted(set(c["lr"] for c in configs))
        bs_values = sorted(set(c["batch_size"] for c in configs))
        assert lr_values == [0.001, 0.01, 0.1]
        assert bs_values == [32, 64]

    def test_mixed_product_and_zip(self, sample_config_mixed):
        """Product(3 × 2) × Zip(3) = 18."""
        configs = generate_batch_configs(sample_config_mixed)
        assert len(configs) == 18

    def test_zip_only(self):
        cfg = {
            "seed": "(1 | 2 | 3)",
            "tag": "(a | b | c)",
            "fixed": "hello",
        }
        configs = generate_batch_configs(cfg)
        assert len(configs) == 3
        seeds = [c["seed"] for c in configs]
        tags = [c["tag"] for c in configs]
        assert seeds == [1, 2, 3]
        assert tags == ["a", "b", "c"]
        assert all(c["fixed"] == "hello" for c in configs)

    def test_zip_length_mismatch_raises(self):
        cfg = {
            "a": "(1 | 2 | 3)",
            "b": "(x | y)",  # length 2 != 3
        }
        with pytest.raises(ValueError, match="equal length"):
            generate_batch_configs(cfg)

    def test_nested_dict_product(self):
        cfg = {
            "model": {"name": "resnet | vgg", "layers": 50},
            "lr": "0.01 | 0.1",
        }
        configs = generate_batch_configs(cfg)
        assert len(configs) == 4  # 2 × 2
        names = set(c["model"]["name"] for c in configs)
        assert names == {"resnet", "vgg"}

    def test_meta_desc_present(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        for c in configs:
            assert "_meta_desc" in c
            assert isinstance(c["_meta_desc"], str)
            assert len(c["_meta_desc"]) > 0

    def test_fixed_values_preserved(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        for c in configs:
            assert c["optimizer"] == "adam"
            assert c["model"]["name"] == "resnet"
            assert c["model"]["layers"] == 50


# ═══════════════════════════════════════════════════════════════
#  count_batch_configs
# ═══════════════════════════════════════════════════════════════

class TestCountBatchConfigs:
    def test_no_pipes(self, sample_config):
        assert count_batch_configs(sample_config) == 1

    def test_product_only(self, sample_config_with_pipes):
        assert count_batch_configs(sample_config_with_pipes) == 6  # 3 × 2

    def test_mixed(self, sample_config_mixed):
        assert count_batch_configs(sample_config_mixed) == 18  # 3 × 2 × 3

    def test_zip_only(self):
        cfg = {"a": "(1 | 2 | 3)", "b": "(x | y | z)"}
        assert count_batch_configs(cfg) == 3

    def test_zip_mismatch_returns_zero(self):
        cfg = {"a": "(1 | 2 | 3)", "b": "(x | y)"}
        assert count_batch_configs(cfg) == 0

    def test_count_matches_generate_length(self, sample_config_mixed):
        """count should exactly match len(generate)."""
        n = count_batch_configs(sample_config_mixed)
        configs = generate_batch_configs(sample_config_mixed)
        assert n == len(configs)


# ═══════════════════════════════════════════════════════════════
#  strip_batch_pipes
# ═══════════════════════════════════════════════════════════════

class TestStripBatchPipes:
    def test_keeps_first_product_value(self):
        cfg = {"lr": "0.001 | 0.01 | 0.1", "bs": "32 | 64"}
        result = strip_batch_pipes(cfg)
        assert result["lr"] == 0.001
        assert result["bs"] == 32

    def test_keeps_first_zip_value(self):
        cfg = {"seed": "(1 | 2 | 3)", "tag": "(a | b | c)"}
        result = strip_batch_pipes(cfg)
        assert result["seed"] == 1
        assert result["tag"] == "a"

    def test_no_pipes_unchanged(self, sample_config):
        result = strip_batch_pipes(sample_config)
        assert result == sample_config

    def test_nested_pipes(self):
        cfg = {"model": {"name": "resnet | vgg"}, "lr": 0.01}
        result = strip_batch_pipes(cfg)
        assert result["model"]["name"] == "resnet"
        assert result["lr"] == 0.01



# ═══════════════════════════════════════════════════════════════
#  validate_task_name
# ═══════════════════════════════════════════════════════════════


class TestValidateTaskName:
    def test_valid_names(self):
        assert validate_task_name("hello") is None
        assert validate_task_name("my-experiment") is None
        assert validate_task_name("run_001") is None
        assert validate_task_name("中文任务名") is None
        assert validate_task_name("test 123") is None

    def test_empty_name(self):
        err = validate_task_name("")
        assert err is not None
        assert "empty" in err.lower()

    def test_whitespace_only(self):
        err = validate_task_name("   ")
        assert err is not None

    def test_too_long(self):
        err = validate_task_name("a" * 201)
        assert err is not None
        assert "long" in err.lower()

    def test_exactly_200(self):
        assert validate_task_name("a" * 200) is None

    def test_invalid_chars(self):
        for bad in ['a<b', 'a>b', 'a:b', 'a"b', 'a/b', 'a\\b', 'a|b', 'a?b', 'a*b']:
            err = validate_task_name(bad)
            assert err is not None, f"Should reject: {bad}"

    def test_starts_with_dot(self):
        err = validate_task_name(".hidden")
        assert err is not None
        assert "start with '.'" in err
        
        err2 = validate_task_name("..")
        assert err2 is not None


# ═══════════════════════════════════════════════════════════════
#  Type Validation, Multiline Search, Pipe Escaping
# ═══════════════════════════════════════════════════════════════


class TestTypeValidation:
    def test_exact_match(self):
        orig = {"lr": 0.01, "name": "resnet"}
        new = [{"lr": 0.05, "name": "vgg"}]
        assert validate_config_types_against_template(orig, new) is None

    def test_float_int_coercion_allowed(self):
        orig = {"lr": 0.01}
        new = [{"lr": 1}]
        assert validate_config_types_against_template(orig, new) is None

    def test_type_mismatch_returns_error(self):
        orig = {"epochs": 100}
        new = [{"epochs": "many"}]
        err = validate_config_types_against_template(orig, new)
        assert err is not None
        assert "输入类型错误" in err
        assert "int" in err
        assert "str" in err


class TestFilterTasksMultiline:
    def test_multiline_yaml_subset(self):
        tasks = [
            {"name": "task1", "config": {"device": None, "batch_size": 32, "lr": 0.01}, "status": "running"},
            {"name": "task2", "config": {"device": "cuda:0", "batch_size": 32}, "status": "completed"}
        ]
        query = "device: null\nbatch_size: 32"
        filtered = filter_tasks(tasks, query)
        assert len(filtered) == 1
        assert filtered[0]["name"] == "task1"

    def test_spaces_around_colons(self):
        tasks = [{"name": "t1", "config": {"device": None}, "status": "queued"}]
        # Should match despite strange spaces
        filtered = filter_tasks(tasks, "device:null\n")
        assert len(filtered) == 1



class TestPipeEscaping:
    def test_escaped_pipe_not_split(self):
        txt = f"a {BATCH_ESCAPE} b | c"
        parts = _split_by_pipe(txt)
        assert len(parts) == 2
        assert parts[0].strip() == "a | b"
        assert parts[1].strip() == "c"
