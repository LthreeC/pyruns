"""
Tests for pyruns.utils — parse_utils, log_io, process_utils, sort_utils,
settings, info_io, config_utils, batch_utils, and validation.
"""
import builtins
import importlib
import json
import logging
import os
import re
import signal
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from unittest.mock import patch, MagicMock

import pyruns.utils.batch_utils as batch_utils
import pyruns.utils.log_io as log_io
import pyruns.utils.process_utils as process_utils
import pyruns.utils.settings as settings
from pyruns._config import (
    DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME,
    SETTINGS_FILENAME, SCRIPT_INFO_FILENAME, TASK_INFO_FILENAME, RUN_LOGS_DIR, RECORDS_KEY,
    BATCH_ESCAPE,
)
from pyruns.utils.batch_utils import (
    _parse_pipe_value, _split_by_pipe,
    generate_batch_configs, count_batch_configs, strip_batch_pipes,
)
from pyruns.utils.config_utils import (
    safe_filename, parse_value, flatten_dict, unflatten_dict,
    load_yaml, load_yaml_strict, save_yaml, list_yaml_files, list_template_files,
    preview_config_line, validate_config_types_against_template,
)
from pyruns.utils.log_io import (
    append_log, decode_log_bytes, normalize_log_newlines,
    read_log, read_log_chunk, read_last_bytes, read_last_lines, safe_read_log,
)
from pyruns.utils.parse_utils import (
    detect_config_source_fast, extract_argparse_params,
    argparse_params_to_dict, resolve_config_path, generate_config_file, split_cli_args,
)
from pyruns.utils.process_utils import is_pid_running, kill_process
from pyruns.utils.sort_utils import task_sort_key, filter_tasks, sort_tasks_for_manager
from pyruns.utils.info_io import (
    load_task_info, save_task_info, update_task_info, load_record_data,
    get_log_options, resolve_log_path, validate_task_name, task_info_lock,
)


def test_detect_config_source_fast(tmp_path):
    # 1. pyruns_load
    p_load = tmp_path / "load.py"
    p_load.write_text("import pyruns\nconfig = pyruns.load()\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_load)) == ("pyruns_load", None)

    # 2. argparse
    p_arg = tmp_path / "arg.py"
    p_arg.write_text("import argparse\nparser.add_argument('--lr', type=float, default=0.01)\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_arg)) == ("argparse", None)

    # 3. hydra
    p_hydra = tmp_path / "hydra_demo.py"
    p_hydra.write_text(
        "import hydra\n@hydra.main(version_base=None, config_path='conf', config_name='config')\ndef main(cfg):\n    pass\n",
        encoding="utf-8",
    )
    assert detect_config_source_fast(str(p_hydra)) == ("hydra", None)

    # 4. unknown
    p_unk = tmp_path / "unk.py"
    p_unk.write_text("print('hello world')", encoding="utf-8")
    assert detect_config_source_fast(str(p_unk)) == ("unknown", None)

    # 5. alias import
    p_alias = tmp_path / "alias.py"
    p_alias.write_text("import pyruns as pyr\nconfig = pyr.load()\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_alias)) == ("pyruns_load", None)

    # 6. from-import alias
    p_from = tmp_path / "from_load.py"
    p_from.write_text("from pyruns import load as cfg_load\nconfig = cfg_load()\n", encoding="utf-8")
    assert detect_config_source_fast(str(p_from)) == ("pyruns_load", None)

    # 7. hydra alias import
    p_hydra_alias = tmp_path / "hydra_alias.py"
    p_hydra_alias.write_text(
        "from hydra import main as hydra_main\n@hydra_main(version_base=None, config_path='conf', config_name='config')\ndef main(cfg):\n    pass\n",
        encoding="utf-8",
    )
    assert detect_config_source_fast(str(p_hydra_alias)) == ("hydra", None)


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


def test_extract_argparse_params_supports_dest_action_choices_and_positional(tmp_path):
    p_script = tmp_path / "advanced_argparse.py"
    code = """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("dataset")
parser.add_argument("-q", dest="quiet_mode", action="store_true")
parser.add_argument("--device", choices=["cpu", "cuda"], nargs="?", default="cpu")
parser.add_argument("--limit", type=int, default=-1)
"""
    p_script.write_text(code, encoding="utf-8")
    params = extract_argparse_params(str(p_script))
    assert params["dataset"]["name"] == "dataset"
    assert params["quiet_mode"]["action"] == "store_true"
    assert params["quiet_mode"]["default"] is False
    assert params["device"]["choices"] == ["cpu", "cuda"]
    assert params["device"]["nargs"] == "?"
    assert params["limit"]["default"] == -1


def test_extract_argparse_params_supports_boolean_optional_action(tmp_path):
    p_script = tmp_path / "boolean_optional.py"
    code = """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--compile", action=argparse.BooleanOptionalAction, default=True)
"""
    p_script.write_text(code, encoding="utf-8")

    params = extract_argparse_params(str(p_script))

    assert params["compile"]["action"] == "argparse.BooleanOptionalAction"
    assert params["compile"]["default"] is True


def test_argparse_params_to_dict():
    params = {
        "lr": {"name": "--lr", "default": 0.01},
        "epochs": {"name": "--epochs", "default": 10},
        "no_default": {"name": "--flag"},
    }
    d = argparse_params_to_dict(params)
    assert d == {"lr": 0.01, "epochs": 10, "no_default": None}


def test_split_cli_args_invalid_quotes():
    with pytest.raises(ValueError, match="Invalid CLI args"):
        split_cli_args('model="vit')


def test_split_cli_args_handles_windows_path_and_kv_args():
    args = split_cli_args('"C:\\Program Files\\Python\\python.exe" -m train model=vit dataset=imagenet')
    assert args[0] == "C:\\Program Files\\Python\\python.exe"
    assert args[1:3] == ["-m", "train"]
    assert "model=vit" in args
    assert "dataset=imagenet" in args


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
    assert read_log(log_file).replace("\r", "") == "Line 1\n"
    
    # Append adds
    append_log(log_file, "Line 2\n")
    assert read_log(log_file).replace("\r", "") == "Line 1\nLine 2\n"


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


def test_normalize_log_newlines_leaves_terminal_stream_unchanged():
    text = "progress 1/3\rprogress 2/3\nfinished\n"

    normalized = normalize_log_newlines(text)

    assert normalized == text
    assert "progress 1/3\r\nprogress 2/3" not in normalized


def test_read_last_bytes(tmp_path):
    log_file = str(tmp_path / "test.log")
    
    assert read_last_bytes(log_file) == ("", 0)
    
    content = "Hello\nWorld\n" * 100 # len=1200
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
        
    text, offset = read_last_bytes(log_file, 12)
    assert len(text.replace("\r", "")) == 12
    assert "World\n" in text.replace("\r", "")
    assert offset == len(content)
    
    # Ask for more than available
    text2, offset2 = read_last_bytes(log_file, 2000)
    assert text2.replace("\r", "") == content
    assert offset2 == len(content)


def test_read_last_lines(tmp_path):
    log_file = str(tmp_path / "test.log")

    assert read_last_lines(log_file, 100) == ("", 0)

    content = "line 1\nline 2\nline 3\nline 4"
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    text, offset = read_last_lines(log_file, 2)
    assert text.replace("\r", "") == "line 3\nline 4"
    assert offset == len(content)

    text2, offset2 = read_last_lines(log_file, 20)
    assert text2.replace("\r", "") == content
    assert offset2 == len(content)

    text3, offset3 = read_last_lines(log_file, 0)
    assert text3 == ""
    assert offset3 == len(content)

    progress_content = "step 1\rstep 2\rstep 3"
    with open(log_file, "w", encoding="utf-8", newline="") as f:
        f.write(progress_content)

    text4, offset4 = read_last_lines(log_file, 2)
    assert text4 == progress_content
    assert offset4 == len(progress_content)


def test_read_last_lines_treats_carriage_return_progress_as_same_terminal_row(tmp_path):
    log_file = str(tmp_path / "progress.log")
    content = "epoch 1 done\nloading 1%\rloading 98%\rloading 100%\nepoch 2 done\n"
    with open(log_file, "w", encoding="utf-8", newline="") as f:
        f.write(content)

    text, offset = read_last_lines(log_file, 3)

    assert text == "epoch 1 done\nloading 1%\rloading 98%\rloading 100%\nepoch 2 done\n"
    assert offset == len(content)


def test_read_last_lines_respects_max_bytes(tmp_path):
    log_file = str(tmp_path / "large-lines.log")
    content = ("A" * 40 + "\n") + ("B" * 40 + "\n")
    with open(log_file, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)

    text, offset = read_last_lines(log_file, max_lines=100, max_bytes=16)

    assert text.replace("\r", "") == "B" * 15 + "\n"
    assert offset == len(content)


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


def test_decode_log_bytes_falls_back_to_gbk_for_windows_logs(monkeypatch):
    text = "测试 PowerShell 输出"
    encoded = text.encode("gbk")
    monkeypatch.setattr("pyruns.utils.log_io.os.name", "nt", raising=False)
    assert decode_log_bytes(encoded) == text


# ═══════════════════════════════════════════════════════════════
#  process_utils
# ═══════════════════════════════════════════════════════════════


def test_decode_log_bytes_handles_invalid_encodings_and_best_replacement(monkeypatch):
    assert log_io._decode_with_encoding(b"\xff", "not-a-real-encoding") is None

    def fake_decode(data, encoding, *, errors="strict"):
        if errors == "strict":
            return None
        if encoding == "utf-8":
            return "\ufffd" * len(data)
        if encoding == "noisy":
            return "\ufffd\ufffdok"
        if encoding == "cleaner":
            return "\ufffdok"
        return None

    monkeypatch.setattr(log_io, "_log_decode_candidates", lambda: ["noisy", "cleaner"])
    monkeypatch.setattr(log_io, "_decode_with_encoding", fake_decode)

    assert decode_log_bytes(b"\xff" * 8) == "\ufffdok"


def test_decode_log_bytes_uses_utf8_replace_when_no_fallback_candidate(monkeypatch):
    monkeypatch.setattr(log_io, "_log_decode_candidates", lambda: ["not-a-real-encoding"])

    data = b"\xff" * 8

    assert decode_log_bytes(data) == data.decode("utf-8", errors="replace")


def test_log_io_error_and_empty_read_edges(tmp_path, monkeypatch):
    assert log_io._split_lf_lines_keepends(b"") == []

    empty_log = tmp_path / "empty.log"
    empty_log.write_bytes(b"")
    assert read_last_lines(str(empty_log), max_lines=5) == ("", 0)

    log_file = tmp_path / "edge.log"
    log_file.write_text("abc", encoding="utf-8")
    assert safe_read_log(str(log_file), 3) == ("", 3)
    assert safe_read_log(str(log_file), 99) == ("", 3)

    monkeypatch.setattr(log_io.os.path, "exists", lambda _path: True)

    def raise_stat_error(_path):
        raise OSError("stat failed")

    monkeypatch.setattr(log_io.os.path, "getsize", raise_stat_error)
    assert read_log_chunk("missing.log", 7) == ("", 7)
    assert read_last_bytes("missing.log", 10) == ("", 0)
    assert read_last_lines("missing.log", max_lines=5) == ("", 0)


def test_safe_read_log_keeps_offset_when_read_returns_no_bytes(monkeypatch):
    class EmptyReader:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def seek(self, _offset):
            return None

        def read(self, _max_bytes):
            return b""

    monkeypatch.setattr(log_io.os.path, "exists", lambda _path: True)
    monkeypatch.setattr(log_io.os.path, "getsize", lambda _path: 10)
    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: EmptyReader())

    assert safe_read_log("edge.log", 4, max_bytes=8) == ("", 4)


def test_is_pid_running_invalid():
    assert not is_pid_running(None)
    assert not is_pid_running("not_a_pid")
    assert not is_pid_running(0) # Depending on OS, 0 might mean something, but usually handled as invalid or special. We just test logic.

def test_is_pid_running_self():
    # The current process should definitely be running
    my_pid = os.getpid()
    assert is_pid_running(my_pid)


def test_process_utils_import_falls_back_without_psutil(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "psutil":
            raise ImportError("no psutil")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    reloaded = importlib.reload(process_utils)
    assert reloaded._psutil is None

    monkeypatch.setattr(builtins, "__import__", real_import)
    importlib.reload(process_utils)


def test_is_pid_running_uses_os_fallback_when_psutil_errors(monkeypatch):
    class RaisingPsutil:
        def pid_exists(self, _pid):
            raise RuntimeError("psutil unavailable")

    calls = []
    monkeypatch.setattr(process_utils, "_psutil", RaisingPsutil())
    monkeypatch.setattr(process_utils.os, "name", "posix", raising=False)
    monkeypatch.setattr(process_utils.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    assert process_utils.is_pid_running("123") is True
    assert calls == [(123, 0)]


@patch("pyruns.utils.process_utils.os.name", "posix")
@patch("pyruns.utils.process_utils.os.kill")
@patch("pyruns.utils.process_utils._psutil", None)
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
@patch("pyruns.utils.process_utils._psutil", None)
def test_is_pid_running_mock_nt_false(mock_open):
    # If OpenProcess returns 0, it should return False
    mock_open.return_value = 0 # Handle is 0/None -> not running
    assert is_pid_running(99999) is False
    mock_open.assert_called_with(0x00100000 | 0x1000, False, 99999)


@pytest.mark.skipif(os.name != "nt", reason="ctypes.windll only available on Windows")
@patch("pyruns.utils.process_utils.os.name", "nt")
@patch("ctypes.windll.kernel32.CloseHandle")
@patch("ctypes.windll.kernel32.GetExitCodeProcess")
@patch("ctypes.windll.kernel32.OpenProcess")
@patch("pyruns.utils.process_utils._psutil", None)
def test_is_pid_running_mock_nt_true(mock_open, mock_get_exit, mock_close):
    mock_open.return_value = 1234 # Got a handle

    def mock_get_exit_code(handle, lpExitCode):
        lpExitCode._obj.value = 259 # STILL_ACTIVE
        return 1
    mock_get_exit.side_effect = mock_get_exit_code

    assert is_pid_running(99999) is True
    mock_close.assert_called_with(1234)


@pytest.mark.skipif(os.name != "nt", reason="ctypes.windll only available on Windows")
def test_is_pid_running_nt_treats_handle_as_running_when_exit_code_unavailable(monkeypatch):
    import ctypes

    class Kernel32:
        def __init__(self):
            self.closed = []

        def OpenProcess(self, _access, _inherit, _pid):
            return 1234

        def GetExitCodeProcess(self, _handle, _lp_exit_code):
            return 0

        def CloseHandle(self, handle):
            self.closed.append(handle)

    kernel32 = Kernel32()
    monkeypatch.setattr(process_utils, "_psutil", None)
    monkeypatch.setattr(process_utils.os, "name", "nt", raising=False)
    monkeypatch.setattr(ctypes, "windll", type("Windll", (), {"kernel32": kernel32})(), raising=False)

    assert process_utils.is_pid_running(99999) is True
    assert kernel32.closed == [1234]


@pytest.mark.skipif(os.name != "nt", reason="ctypes.windll only available on Windows")
def test_is_pid_running_nt_ctypes_error_returns_false(monkeypatch):
    import ctypes

    class Kernel32:
        def OpenProcess(self, _access, _inherit, _pid):
            raise OSError("open failed")

    monkeypatch.setattr(process_utils, "_psutil", None)
    monkeypatch.setattr(process_utils.os, "name", "nt", raising=False)
    monkeypatch.setattr(ctypes, "windll", type("Windll", (), {"kernel32": Kernel32()})(), raising=False)

    assert process_utils.is_pid_running(99999) is False


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


def test_get_now_str_us_includes_six_digit_microseconds():
    from pyruns.utils.time_utils import get_now_str_us

    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_\d{6}", get_now_str_us())


def test_task_sort_key():
    # Priority 1: last element of start_times
    task1 = {
        "start_times": ["2023-10-01", "2023-10-05"],
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task1) == (0, 20231005, 1)

    # Priority 2: created_at, if start_times is empty or missing
    task2 = {
        "start_times": [],
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task2) == (0, 20231002, 1)

    task3 = {
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task3) == (0, 20231002, 1)

    # Default: empty string if neither are present
    task4 = {}
    assert task_sort_key(task4) == (0, 0, 1)

    # Bad data type fallback
    task5 = {
        "start_times": "not_a_list",
        "created_at": "2023-10-02"
    }
    # It checks isinstance(list), so it should fallback to created_at
    assert task_sort_key(task5) == (0, 20231002, 1)
    
    # Priority Top: Running/Queued tasks return inverted int
    task6 = {
        "status": "running",
        "start_times": ["2023-10-06"],
        "created_at": "2023-10-02"
    }
    assert task_sort_key(task6) == (1, 20231006, 0)
    
    task7 = {
        "status": "completed",
        "start_times": ["2023-10-07"]
    }
    assert task_sort_key(task7) == (0, 20231007, 2)


def test_sort_tasks_for_manager_keeps_pinned_active_and_fresh_tasks_first():
    tasks = [
        {
            "name": "manual-completed",
            "status": "completed",
            "created_at": "2026-05-28_02-25-46",
            "task_order": 0,
        },
        {
            "name": "manual-pending",
            "status": "pending",
            "created_at": "2026-05-28_02-25-47",
            "task_order": 1,
        },
        {
            "name": "running-manual",
            "status": "running",
            "created_at": "2026-05-28_02-25-48",
            "start_times": ["2026-05-28_02-25-48"],
            "task_order": 2,
        },
        {
            "name": "fresh-new",
            "status": "pending",
            "created_at": "2026-05-31_22-50-00",
        },
        {
            "name": "pinned-fresh",
            "status": "pending",
            "created_at": "2026-05-31_22-55-00",
            "pinned": True,
        },
        {
            "name": "pinned-manual",
            "status": "completed",
            "created_at": "2026-05-29_10-00-00",
            "task_order": 0,
            "pinned": True,
        },
    ]

    assert [task["name"] for task in sort_tasks_for_manager(tasks)] == [
        "pinned-fresh",
        "pinned-manual",
        "running-manual",
        "fresh-new",
        "manual-completed",
        "manual-pending",
    ]


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
    assert cfg["ui_port"] == settings.SETTINGS_DEFAULTS["ui_port"]
    assert cfg == settings._cached
    
    # Custom values
    with open(file_path, "w", encoding="utf-8") as f:
        yaml.dump({"ui_port": 9999, "new_key": "abc"}, f)
        
    cfg2 = settings.load_settings(root_dir)
    assert cfg2["ui_port"] == 9999
    assert cfg2["new_key"] == "abc"
    # Defaults still present
    assert cfg2["manager_columns"] == settings.SETTINGS_DEFAULTS["manager_columns"]


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
        assert settings.get("ui_port") == settings.SETTINGS_DEFAULTS["ui_port"]


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
            # Accept both formats: YAML dump may use "key:\n- val" or "key: \n- val"
            assert "pinned_params" in text
            assert "- a\n- b" in text

        assert settings._cached["pinned_params"] == ["a", "b"]

        # 4. Update the list again
        settings.save_setting("pinned_params", ["c"])
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            assert "pinned_params" in text
            assert "- c" in text
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
        assert loaded["name"] == info["name"]
        assert loaded["status"] == info["status"]
        assert loaded["extra"] == info["extra"]

    def test_save_normalizes_run_slots(self, tmp_path):
        task_dir = str(tmp_path)
        info = {
            "name": "slot-test",
            "start_times": ["2026-01-01 00:00:00"],
            "records": [{"loss": 0.5}, {"loss": 0.1}],
        }
        save_task_info(task_dir, info)
        loaded = load_task_info(task_dir)
        assert loaded["run_index"] == 2
        assert loaded["start_times"] == ["2026-01-01 00:00:00", ""]
        assert loaded["finish_times"] == ["", ""]
        assert loaded["records"][1] == {"loss": 0.1}

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
        assert loaded["name"] == info["name"]
        assert loaded["description"] == info["description"]

    def test_update_retries_transient_replace_permission_error(self, tmp_path):
        task_dir = str(tmp_path)
        save_task_info(task_dir, {"name": "retry-test", "status": "pending"})
        real_replace = os.replace
        calls = {"count": 0}

        def flaky_replace(src, dst):
            if os.path.basename(dst) == TASK_INFO_FILENAME and calls["count"] == 0:
                calls["count"] += 1
                raise PermissionError("temporarily locked")
            return real_replace(src, dst)

        def mark_completed(info):
            info["status"] = "completed"

        with patch("pyruns.utils.info_io.os.replace", side_effect=flaky_replace), patch("pyruns.utils.info_io.time.sleep") as sleep:
            updated = update_task_info(task_dir, mark_completed)

        assert calls["count"] == 1
        sleep.assert_called()
        assert updated["status"] == "completed"
        assert load_task_info(task_dir)["status"] == "completed"

    def test_task_info_lock_recovers_stale_process_lock_file(self, tmp_path):
        task_dir = str(tmp_path)
        lock_path = tmp_path / f".{TASK_INFO_FILENAME}.lock"
        lock_path.write_text("0 0", encoding="utf-8")

        with task_info_lock(task_dir, timeout_sec=0.01):
            assert lock_path.exists()

        assert not lock_path.exists()


class TestLoadRecordData:
    def test_with_records(self, tmp_path):
        task_dir = str(tmp_path)
        info = {RECORDS_KEY: [{"loss": 0.5}, {"loss": 0.1}]}
        save_task_info(task_dir, info)
        data = load_record_data(task_dir)
        assert len(data) == 2
        assert data[0]["loss"] == 0.5

    def test_without_records(self, tmp_path):
        task_dir = str(tmp_path)
        save_task_info(task_dir, {"name": "test"})
        assert load_record_data(task_dir) == []

    def test_missing_file(self, tmp_path):
        assert load_record_data(str(tmp_path)) == []


class TestGetLogOptions:
    def test_run_logs(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
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
        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
        os.makedirs(log_dir)
        path = os.path.join(log_dir, "run1.log")
        open(path, "w").close()

        result = resolve_log_path(task_dir, "run1.log")
        assert result == path

    def test_resolve_latest(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
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

    def test_resolve_default_prefers_run_log_over_error_log(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
        os.makedirs(log_dir)
        run_log = os.path.join(log_dir, "run1.log")
        error_log = os.path.join(log_dir, "error.log")
        open(run_log, "w").close()
        open(error_log, "w").close()
        os.utime(run_log, (1000000, 1000000))
        os.utime(error_log, (2000000, 2000000))

        result = resolve_log_path(task_dir)
        assert result == run_log

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
    
    def test_bool_passthrough(self):
        assert parse_value(True) is True
        assert parse_value(False) is False

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

    def test_load_yaml_strict_raises_on_non_mapping(self, tmp_dir):
        path = str(tmp_dir / "list.yaml")
        with open(path, "w") as f:
            f.write("- a\n- b\n")
        with pytest.raises(ValueError, match="mapping"):
            load_yaml_strict(path)

    def test_load_yaml_strict_raises_on_invalid_yaml(self, tmp_dir):
        path = str(tmp_dir / "bad.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("a: [1, 2\n")
        with pytest.raises(Exception):
            load_yaml_strict(path)

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
        assert loaded["name"] == info["name"]
        assert loaded["status"] == info["status"]
        assert loaded["env"] == info["env"]

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

    def test_config_default_is_first_workspace_template(self, tmp_path):
        run_root = str(tmp_path)
        save_yaml(os.path.join(run_root, "config_default.yaml"), {"lr": 0.01})
        task_dir = os.path.join(run_root, "tasks", "generated-task")
        os.makedirs(task_dir, exist_ok=True)
        save_yaml(os.path.join(task_dir, "config.yaml"), {"lr": 0.02})

        result = list_template_files(run_root)

        assert list(result.items())[0][1] == "config_default.yaml"
        assert list(result.keys())[1] == "tasks/generated-task/config.yaml"

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

    def test_range_colon_syntax(self):
        result = _parse_pipe_value("30:40:1")
        assert result is not None
        parts, mode = result
        assert mode == "product"
        assert list(parts) == list(range(30, 40))


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

    def test_range_string_generates_batch(self):
        cfg = {"epochs": "30:40:1", "optimizer": "adam"}
        configs = generate_batch_configs(cfg)
        assert len(configs) == 10
        assert [item["epochs"] for item in configs] == list(range(30, 40))
        assert all(item["optimizer"] == "adam" for item in configs)

    def test_generation_rejects_oversized_batches_before_iterating_ranges(self, monkeypatch):
        class HugeRange:
            def __len__(self):
                return 1_000_000

            def __iter__(self):
                raise AssertionError("oversized range should not be expanded")

        monkeypatch.setattr(batch_utils, "range", lambda *_args: HugeRange(), raising=False)

        with pytest.raises(ValueError, match="Batch expansion would create 1000000 tasks"):
            generate_batch_configs({"epochs": "0:1000000:1"}, max_configs=999)


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

    def test_range_colon_count(self):
        assert count_batch_configs({"epochs": "30:40:1"}) == 10

    def test_large_range_count_does_not_expand_values(self, monkeypatch):
        class HugeRange:
            def __len__(self):
                return 1_000_000

            def __iter__(self):
                raise AssertionError("counting should not iterate range values")

        monkeypatch.setattr(batch_utils, "range", lambda *_args: HugeRange(), raising=False)

        assert count_batch_configs({"epochs": "0:1000000:1"}) == 1_000_000


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

    def test_search_text_space_normalization(self):
        tasks = [
            {"name": "t1", "status": "running", "search_text": "device : null\nbatch_size: 32"},
            {"name": "t2", "status": "running", "search_text": "device: cuda:0\nbatch_size: 32"},
        ]
        filtered = filter_tasks(tasks, "device:null\nbatch_size:32")
        assert len(filtered) == 1
        assert filtered[0]["name"] == "t1"



class TestPipeEscaping:
    def test_escaped_pipe_not_split(self):
        txt = f"a {BATCH_ESCAPE} b | c"
        parts = _split_by_pipe(txt)
        assert len(parts) == 2
        assert parts[0].strip() == "a | b"
        assert parts[1].strip() == "c"


def test_log_decode_candidates_include_preferred_and_windows_fallbacks(monkeypatch):
    from pyruns.utils import log_io

    monkeypatch.setattr(log_io.locale, "getpreferredencoding", lambda _do_setlocale=False: "cp1252")
    monkeypatch.setattr(log_io.os, "name", "nt", raising=False)

    candidates = log_io._log_decode_candidates()

    assert candidates[:2] == ["utf-8-sig", "utf-8"]
    assert "cp1252" in candidates
    assert "gbk" in candidates
    assert "cp936" in candidates


def test_decode_log_bytes_prefers_replacement_when_only_small_utf8_damage(monkeypatch):
    from pyruns.utils import log_io

    monkeypatch.setattr(log_io, "_log_decode_candidates", lambda: ["ascii"])

    assert decode_log_bytes(b"ok\xff") == "ok\ufffd"


def test_decode_log_bytes_chooses_best_replacement_fallback(monkeypatch):
    from pyruns.utils import log_io

    data = "训练".encode("utf-16le")
    monkeypatch.setattr(log_io, "_log_decode_candidates", lambda: ["ascii", "utf-16le"])

    assert decode_log_bytes(data) == "训练"


def test_append_and_read_log_ignore_io_errors(tmp_path, monkeypatch):
    log_path = tmp_path / "run.log"

    append_log(str(log_path), "hello\n")
    append_log(str(log_path), "world\n")
    assert read_log(str(log_path)).replace("\r", "") == "hello\nworld\n"

    def fail_open(*_args, **_kwargs):
        raise OSError("blocked")

    monkeypatch.setattr("builtins.open", fail_open)
    append_log(str(log_path), "ignored")
    assert read_log(str(log_path)) == ""


def test_read_log_chunk_handles_missing_offsets_and_empty_reads(tmp_path):
    log_path = tmp_path / "run.log"

    assert read_log_chunk(str(log_path), 10) == ("", 0)
    log_path.write_bytes(b"alpha\nbeta\n")

    assert read_log_chunk(str(log_path), 999) == ("alpha\nbeta\n", len("alpha\nbeta\n"))
    text, offset = read_log_chunk(str(log_path), len("alpha\n"))
    assert text == "beta\n"
    assert offset == len("alpha\nbeta\n")
    assert read_log_chunk(str(log_path), offset) == ("", offset)


def test_read_last_bytes_empty_and_tail(tmp_path):
    log_path = tmp_path / "run.log"

    log_path.write_text("", encoding="utf-8")
    assert read_last_bytes(str(log_path), 5) == ("", 0)
    log_path.write_text("abcdefghij", encoding="utf-8")
    assert read_last_bytes(str(log_path), 4) == ("ghij", 10)


def test_safe_read_log_handles_missing_complete_and_partial_lines(tmp_path):
    log_path = tmp_path / "run.log"

    assert safe_read_log(str(log_path), 5) == ("", 5)
    log_path.write_bytes(b"line1\nline2\npartial")

    text, offset = safe_read_log(str(log_path), 0, max_bytes=12)
    assert text == "line1\nline2\n"
    assert offset == 12

    text, offset = safe_read_log(str(log_path), 12, max_bytes=100)
    assert text == "partial"
    assert offset == len("line1\nline2\npartial")


def test_logger_configuration_can_disable_or_attach_file_handler(tmp_path, monkeypatch):
    from pyruns.utils import log_utils

    root_logger = logging.getLogger(log_utils.get_library_root())
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_propagate = root_logger.propagate
    original_library_logger = log_utils._LIBRARY_ROOT_LOGGER

    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

        monkeypatch.setattr(log_utils, "_LIBRARY_ROOT_LOGGER", None)
        monkeypatch.setattr("pyruns.utils.settings.get", lambda key, default=None: False if key == "log_enabled" else default)

        log_utils.configure_project_root_logger()
        disabled = log_utils._LIBRARY_ROOT_LOGGER
        assert disabled.level > 50

        monkeypatch.setattr(log_utils, "_LIBRARY_ROOT_LOGGER", None)
        monkeypatch.setattr("pyruns.utils.settings.get", lambda key, default=None: "DEBUG" if key == "log_level" else default)
        logger = log_utils.get_logger("__main__")
        assert logger.name.endswith(".__main__")
        assert log_utils._LIBRARY_ROOT_LOGGER.handlers

        log_file = tmp_path / "pyruns.log"
        log_utils.attach_file_handler(str(log_file))
        log_utils._LIBRARY_ROOT_LOGGER.debug("written")
        assert log_file.exists()
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
        root_logger.propagate = original_propagate
        log_utils._LIBRARY_ROOT_LOGGER = original_library_logger


def test_info_io_lock_helpers_handle_invalid_stale_and_failed_cleanup(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    lock_path = task_dir / info_io._LOCK_FILENAME

    assert info_io._read_lock_owner_pid(str(lock_path)) is None
    lock_path.write_text("not-a-pid extra", encoding="utf-8")
    assert info_io._read_lock_owner_pid(str(lock_path)) is None

    lock_path.write_text("999999 extra", encoding="utf-8")
    monkeypatch.setattr(info_io, "is_pid_running", lambda pid: False)
    assert info_io._lock_file_is_stale(str(lock_path), min_age_sec=999999) is True
    assert info_io._remove_stale_lock_file(str(lock_path)) is True

    lock_path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(info_io.os, "remove", lambda path: (_ for _ in ()).throw(OSError("locked")))
    assert info_io._remove_stale_lock_file(str(lock_path)) is False

    monkeypatch.setattr(info_io.os, "remove", lambda path: (_ for _ in ()).throw(FileNotFoundError(path)))
    assert info_io._remove_stale_lock_file(str(lock_path)) is True


def test_task_info_lock_times_out_when_live_lock_persists(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    lock_path = task_dir / info_io._LOCK_FILENAME
    lock_path.write_text(str(os.getpid()), encoding="utf-8")
    monkeypatch.setattr(info_io, "_remove_stale_lock_file", lambda path: False)
    monkeypatch.setattr(info_io.time, "sleep", lambda delay: None)

    with pytest.raises(TimeoutError, match="file lock"):
        with task_info_lock(str(task_dir), timeout_sec=0):
            pass


def test_info_io_load_and_update_error_modes(tmp_path):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    info_path = task_dir / TASK_INFO_FILENAME

    info_path.write_text("{not json", encoding="utf-8")
    assert load_task_info(str(task_dir)) == {}
    with pytest.raises(json.JSONDecodeError):
        load_task_info(str(task_dir), raise_error=True)

    with pytest.raises(json.JSONDecodeError):
        update_task_info(str(task_dir), lambda info: info.update(name="x"), raise_error=True)

    missing_dir = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        update_task_info(str(missing_dir), lambda info: None, raise_error=True)


def test_script_info_roundtrip_and_invalid_json(tmp_path):
    import pyruns.utils.info_io as info_io

    run_root = tmp_path / "run"
    run_root.mkdir()
    script_info = run_root / SCRIPT_INFO_FILENAME
    script_info.write_text("{bad json", encoding="utf-8")
    assert info_io.load_script_info(str(run_root)) == {}

    info_io.save_script_info(str(run_root), {"script_name": "train", "params": {"lr": 0.1}})
    assert info_io.load_script_info(str(run_root))["params"]["lr"] == 0.1


def test_atomic_info_writers_remove_temp_files_after_replace_failure(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    task_dir = tmp_path / "task"
    task_dir.mkdir()

    monkeypatch.setattr(info_io, "_replace_with_retry", lambda src, dst: (_ for _ in ()).throw(RuntimeError("replace failed")))
    with pytest.raises(RuntimeError):
        info_io.save_script_info(str(task_dir), {"script_name": "train"})
    assert not list(task_dir.glob(f".{SCRIPT_INFO_FILENAME}.*.tmp"))

    with pytest.raises(RuntimeError):
        with task_info_lock(str(task_dir)):
            info_io._write_task_info_unlocked(str(task_dir / TASK_INFO_FILENAME), str(task_dir), {"name": "alpha"})
    assert not list(task_dir.glob(f".{TASK_INFO_FILENAME}.*.tmp"))


def test_settings_load_get_and_scalar_text_edges(tmp_path, monkeypatch):
    root = tmp_path / "_pyruns_" / "script"
    root.mkdir(parents=True)
    settings_path = Path(settings._settings_path(str(root)))
    settings_path.write_text("ui_port: [unterminated", encoding="utf-8")

    loaded = settings.load_settings(str(root))
    assert loaded["ui_port"] == settings.SETTINGS_DEFAULTS["ui_port"]
    assert settings.reload_settings(str(root))["ui_port"] == settings.SETTINGS_DEFAULTS["ui_port"]

    monkeypatch.setattr(settings, "_cached", {})
    monkeypatch.setattr(settings, "load_settings", lambda root_dir=settings.ROOT_DIR: (_ for _ in ()).throw(RuntimeError("boom")))
    assert settings.get("ui_port") == settings.SETTINGS_DEFAULTS["ui_port"]
    assert settings.get("unknown", "fallback") == "fallback"

    assert settings._yaml_scalar_to_text(True) == "true"
    assert settings._yaml_scalar_to_text(False) == "false"
    assert settings._yaml_scalar_to_text(None) == "null"
    assert settings._yaml_scalar_to_text([]) == "[]"
    assert settings._yaml_scalar_to_text({}) == "{}"
    assert "\n- a" in settings._yaml_scalar_to_text(["a"])
    assert "\na: 1" in settings._yaml_scalar_to_text({"a": 1})


def test_save_setting_for_root_preserves_or_appends_structured_values(tmp_path, monkeypatch):
    root = tmp_path / "_pyruns_" / "script"
    root.mkdir(parents=True)
    path = Path(settings._settings_path(str(root)))

    path.write_text("global_env:\n  OLD: '1'\n", encoding="utf-8")
    settings.save_setting_for_root(str(root), "global_env", {"NEW": "2"})
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["global_env"] == {"NEW": "2"}

    path.write_text("[]\n", encoding="utf-8")
    settings.save_setting_for_root(str(root), "pinned_params", ["lr"])
    assert "pinned_params:" in path.read_text(encoding="utf-8")

    path.write_text("pinned_params: []\n", encoding="utf-8")
    monkeypatch.setattr(settings.yaml, "safe_load", lambda text: (_ for _ in ()).throw(yaml.YAMLError("bad yaml")))
    settings.save_setting_for_root(str(root), "pinned_params", ["batch_size"])
    assert "- batch_size" in path.read_text(encoding="utf-8")


def test_save_setting_for_root_creates_file_and_swallows_write_errors(tmp_path, monkeypatch):
    root = tmp_path / "new-root"
    settings.save_setting_for_root(str(root), "ui_port", 8123)
    assert "ui_port: 8123" in (root / SETTINGS_FILENAME).read_text(encoding="utf-8")

    broken_root = tmp_path / "broken-root"
    monkeypatch.setattr(settings.os, "makedirs", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("readonly")))
    settings.save_setting_for_root(str(broken_root), "ui_port", 9999)
    assert not (broken_root / SETTINGS_FILENAME).exists()


def test_log_emitter_dispatches_loop_direct_and_error_callbacks():
    from pyruns.utils.events import LogEmitter

    emitter = LogEmitter()
    received = []

    class RunningLoop:
        def __init__(self):
            self.calls = 0

        def is_running(self):
            return True

        def call_soon_threadsafe(self, callback, *args):
            self.calls += 1
            callback(*args)

    loop = RunningLoop()
    def record(chunk):
        received.append(chunk)

    emitter.subscribe("task", record, loop=loop)
    emitter.subscribe("task", lambda chunk: (_ for _ in ()).throw(RuntimeError("callback failed")))
    emitter.subscribe("other", lambda chunk: received.append("other"))
    emitter.bind_loop()

    emitter.emit("missing", "ignored")
    emitter.emit("task", "chunk")
    emitter.unsubscribe("task", record)
    emitter.emit("task", "after")

    assert loop.calls == 1
    assert received == ["chunk"]


def test_simple_event_bus_handles_sync_async_and_failing_callbacks():
    from pyruns.utils.events import SimpleEventBus

    bus = SimpleEventBus()
    received = []

    async def async_listener(value):
        received.append(("async", value))

    def sync_listener(value):
        received.append(("sync", value))

    def failing_listener(value):
        raise RuntimeError("listener failed")

    bus.on("go", sync_listener)
    bus.on("go", sync_listener)
    bus.on("go", async_listener)
    bus.on("go", failing_listener)
    bus.emit("go", "value")
    bus.off("go", sync_listener)
    bus.emit("go", "again")

    assert received == [("sync", "value")]


def test_shell_runtime_resolves_classifies_and_probes_edges(tmp_path, monkeypatch):
    import pyruns.utils.shell_runtime as shell_runtime

    shell_runtime._probe_shell_executable.cache_clear()
    executable = tmp_path / "pwsh.exe"
    executable.write_text("", encoding="utf-8")

    assert shell_runtime.normalize_shell_mode("custom") == shell_runtime.SHELL_MODE_CUSTOM
    assert shell_runtime.normalize_shell_mode("anything") == shell_runtime.SHELL_MODE_FOLLOW
    assert shell_runtime.classify_shell_executable("pwsh.exe") == ("powershell", "PowerShell")
    assert shell_runtime.classify_shell_executable("unknown-shell") == ("unknown", "unknown-shell")
    assert shell_runtime._resolve_candidate_path("") == ""
    assert shell_runtime._resolve_candidate_path(str(tmp_path / "missing.exe")) == ""

    monkeypatch.setattr(shell_runtime.shutil, "which", lambda value: str(executable) if value == "pwsh" else None)
    assert shell_runtime._resolve_candidate_path("pwsh") == str(executable)

    assert shell_runtime._probe_shell_executable("", "cmd") is False
    assert shell_runtime._probe_shell_executable(str(tmp_path / "missing.exe"), "cmd") is False
    assert shell_runtime._probe_shell_executable(str(executable), "unknown") is False

    class Result:
        def __init__(self, returncode):
            self.returncode = returncode

    monkeypatch.setattr(shell_runtime.subprocess, "run", lambda *args, **kwargs: Result(0))
    shell_runtime._probe_shell_executable.cache_clear()
    assert shell_runtime._probe_shell_executable(str(executable), "powershell") is True

    monkeypatch.setattr(shell_runtime.subprocess, "run", lambda *args, **kwargs: Result(1))
    shell_runtime._probe_shell_executable.cache_clear()
    assert shell_runtime._probe_shell_executable(str(executable), "cmd") is False

    monkeypatch.setattr(shell_runtime.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")))
    shell_runtime._probe_shell_executable.cache_clear()
    assert shell_runtime._probe_shell_executable(str(executable), "cmd") is False

    monkeypatch.setattr(shell_runtime.os, "name", "nt")
    monkeypatch.setattr(shell_runtime, "_probe_windows_posix_script_execution", lambda candidate: True)
    shell_runtime._probe_shell_executable.cache_clear()
    assert shell_runtime._probe_shell_executable(str(executable), "bash") is True


def test_shell_runtime_workspace_and_follow_fallback_branches(tmp_path, monkeypatch):
    import pyruns.utils.shell_runtime as shell_runtime

    executable = tmp_path / "bash.exe"
    executable.write_text("", encoding="utf-8")

    monkeypatch.setattr(shell_runtime, "load_settings", lambda root=None: {
        "shell_mode": "custom",
        "shell_executable": str(executable),
    })
    monkeypatch.setattr(shell_runtime, "_probe_shell_executable", lambda candidate, kind: True)
    runtime = shell_runtime.get_shell_runtime_for_workspace(str(tmp_path))
    assert runtime["mode"] == "custom"
    assert runtime["terminal_kind"] == "bash"
    assert runtime["available"] is True

    monkeypatch.setattr(shell_runtime, "load_settings", lambda root=None: {
        "shell_mode": "follow",
        "shell_executable": "",
    })
    monkeypatch.setattr(shell_runtime, "get_follow_shell_runtime", lambda: {
        "source": "follow_terminal",
        "terminal_kind": "unknown",
        "display_name": "Unknown",
        "executable": str(executable),
        "available": False,
    })
    runtime = shell_runtime.get_shell_runtime_for_workspace(str(tmp_path))
    assert runtime["mode"] == "follow"
    assert runtime["terminal_kind"] == "bash"
    assert runtime["display_name"] == "Bash"
    assert shell_runtime.get_shell_config_filename_for_workspace(str(tmp_path)).endswith(".sh")
    assert shell_runtime.get_shell_config_filename_for_task(str(tmp_path / "tasks" / "alpha")).endswith(".sh")


def test_shell_runtime_process_tree_and_fallback_edges(tmp_path, monkeypatch):
    import pyruns.utils.shell_runtime as shell_runtime

    monkeypatch.setattr(shell_runtime, "psutil", None)
    assert shell_runtime._find_shell_in_process_tree() is None

    class RaisingPsutil:
        @staticmethod
        def Process(pid):
            raise RuntimeError("process unavailable")

    monkeypatch.setattr(shell_runtime, "psutil", RaisingPsutil)
    assert shell_runtime._find_shell_in_process_tree() is None

    fallback = tmp_path / "sh"
    fallback.write_text("", encoding="utf-8")
    monkeypatch.setattr(shell_runtime.os, "name", "posix")
    monkeypatch.delenv("SHELL", raising=False)
    monkeypatch.setattr(shell_runtime, "_resolve_candidate_path", lambda value: str(fallback) if value == "sh" else "")
    runtime = shell_runtime._fallback_follow_shell()
    assert runtime["terminal_kind"] == "sh"
    assert runtime["available"] is True
