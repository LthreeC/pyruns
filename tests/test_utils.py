"""
Tests for pyruns.utils — parse_utils, log_io, process_utils, sort_utils,
settings, info_io, config_utils, batch_utils, and validation.
"""
import builtins
import codecs
import io
import importlib
import json
import logging
import os
import re
import signal
from pathlib import Path

import pytest
import yaml
from omegaconf import DictConfig, OmegaConf
from unittest.mock import patch

import pyruns.utils.batch_utils as batch_utils
import pyruns.utils.config_utils as config_utils
import pyruns.utils.log_io as log_io
import pyruns.utils.process_utils as process_utils
import pyruns.utils.settings as settings
from pyruns._config import (
    DEFAULT_ROOT_NAME, CONFIG_DEFAULT_FILENAME,
    SETTINGS_FILENAME, SCRIPT_INFO_FILENAME, TASK_INFO_FILENAME, RUN_LOGS_DIR, RECORDS_KEY,
    BATCH_ESCAPE,
    MAX_CONFIG_FILE_BYTES,
    CONFIG_FILENAME, POWERSHELL_CONFIG_FILENAME, SHELL_CONFIG_FILENAME,
    TASK_KIND_CONFIG, TASK_KIND_SHELL, WORKSPACE_KIND_SCRIPT, WORKSPACE_KIND_SHELL,
)
from pyruns.utils.batch_utils import (
    _parse_pipe_value, _split_by_pipe,
    generate_batch_configs, count_batch_configs, strip_batch_pipes,
)
from pyruns.utils.config_utils import (
    safe_filename, parse_value, flatten_dict, unflatten_dict,
    load_yaml, load_yaml_strict, save_yaml, list_yaml_files, list_template_files,
    preview_config_line, build_config_preview_and_search_text,
    validate_config_types_against_template,
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
    MAX_RUN_HISTORY_SLOTS, MAX_TASK_INFO_BYTES,
)
from pyruns.utils.task_files import (
    build_task_preview_and_search,
    build_task_search_matches,
    build_task_search_result,
    is_known_task_kind,
    normalize_task_kind,
    normalize_workspace_kind,
    read_task_payload,
    resolve_task_config_file,
    write_task_payload,
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


def test_detect_config_source_fast_accepts_utf8_bom_argparse_script(tmp_path):
    p_bom = tmp_path / "bom_argparse.py"
    p_bom.write_bytes(
        codecs.BOM_UTF8
        + b"import argparse\n"
        + b"parser = argparse.ArgumentParser()\n"
        + b"parser.add_argument('--lr', type=float, default=0.01)\n"
    )

    assert detect_config_source_fast(str(p_bom)) == ("argparse", None)
    assert extract_argparse_params(str(p_bom))["lr"]["default"] == 0.01


def test_parse_utils_handles_missing_invalid_and_multiline_cli_edges(tmp_path, monkeypatch):
    import pyruns.utils.parse_utils as parse_utils

    missing_script = tmp_path / "missing.py"
    assert parse_utils._cache_key(str(missing_script))[1:] == (0, 0)
    assert detect_config_source_fast(str(missing_script)) == ("unknown", None)
    assert extract_argparse_params(str(missing_script)) == {}

    invalid_script = tmp_path / "invalid.py"
    invalid_script.write_text("def bad(:\n", encoding="utf-8")
    assert detect_config_source_fast(str(invalid_script)) == ("unknown", None)

    assert split_cli_args("") == []
    assert split_cli_args("  \n") == []
    assert split_cli_args("--name 'quoted value' \\\n  --flag") == ["--name", "quoted value", "--flag"]
    with pytest.raises(ValueError, match="unmatched quotes"):
        split_cli_args("--name 'unterminated")

    monkeypatch.setattr(parse_utils.os, "name", "nt")
    assert split_cli_args('"--literal"') == ["--literal"]


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


def test_extract_argparse_params_literal_edges_and_store_false_default(tmp_path):
    p_script = tmp_path / "literal_edges.py"
    p_script.write_text(
        """
import argparse
parser = argparse.ArgumentParser()
parser.add_argument("--no-cache", action="store_false")
parser.add_argument("--scale", default=+2)
parser.add_argument("--shape", default=(1, 2))
parser.add_argument("--mapping", default={"a": -1})
parser.add_argument(*["--dynamic"])
""",
        encoding="utf-8",
    )

    params = extract_argparse_params(str(p_script))

    assert params["no_cache"]["default"] is True
    assert params["scale"]["default"] == 2
    assert params["shape"]["default"] == (1, 2)
    assert params["mapping"]["default"] == {"a": -1}
    assert "dynamic" not in params


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


def test_task_file_helpers_cover_shell_and_config_payload_edges(tmp_path, monkeypatch):
    task_dir = tmp_path / "task"
    task_dir.mkdir()

    assert normalize_workspace_kind(WORKSPACE_KIND_SHELL) == WORKSPACE_KIND_SHELL
    assert normalize_workspace_kind("bad") == WORKSPACE_KIND_SCRIPT
    assert normalize_task_kind("py") == TASK_KIND_CONFIG
    assert normalize_task_kind(TASK_KIND_SHELL) == TASK_KIND_SHELL
    assert is_known_task_kind("") is True
    assert is_known_task_kind("bad") is False

    assert resolve_task_config_file({"task_kind": TASK_KIND_SHELL}, TASK_KIND_SHELL, str(task_dir)) == SHELL_CONFIG_FILENAME
    (task_dir / POWERSHELL_CONFIG_FILENAME).write_text("Write-Host hi\n", encoding="utf-8")
    assert resolve_task_config_file({"task_kind": TASK_KIND_SHELL}, TASK_KIND_SHELL, str(task_dir)) == POWERSHELL_CONFIG_FILENAME
    assert resolve_task_config_file({"config_file": "custom.yaml"}) == "custom.yaml"

    kind, config, text, error = read_task_payload(str(task_dir), {"task_kind": TASK_KIND_CONFIG})
    assert kind == TASK_KIND_CONFIG
    assert isinstance(config, DictConfig)
    assert config == {}
    assert text == ""
    assert error == f"{CONFIG_FILENAME} is missing"

    kind, config, text, error = read_task_payload(str(task_dir), {"task_kind": TASK_KIND_SHELL})
    assert kind == TASK_KIND_SHELL
    assert isinstance(config, DictConfig)
    assert config == {}
    assert "Write-Host hi" in text
    assert error == ""

    def fail_open(*args, **kwargs):
        raise OSError("cannot read")

    monkeypatch.setattr("builtins.open", fail_open)
    kind, config, text, error = read_task_payload(str(task_dir), {"task_kind": TASK_KIND_SHELL})
    assert kind == TASK_KIND_SHELL
    assert isinstance(config, DictConfig)
    assert config == {}
    assert text == ""
    assert "cannot read" in error
    monkeypatch.undo()

    config_dir = tmp_path / "config-task"
    write_task_payload(str(config_dir), task_kind=TASK_KIND_CONFIG, config_file=CONFIG_FILENAME, config={"lr": 0.1})
    assert load_yaml_strict(str(config_dir / CONFIG_FILENAME)) == {"lr": 0.1}

    invalid_dir = tmp_path / "invalid-config-task"
    invalid_dir.mkdir()
    (invalid_dir / CONFIG_FILENAME).write_text("broken: [\n", encoding="utf-8")
    kind, config, text, error = read_task_payload(
        str(invalid_dir),
        {"task_kind": TASK_KIND_CONFIG},
    )
    assert kind == TASK_KIND_CONFIG
    assert isinstance(config, DictConfig)
    assert config == {}
    assert text == ""
    assert error

    shell_dir = tmp_path / "shell-task"
    write_task_payload(
        str(shell_dir),
        task_kind=TASK_KIND_SHELL,
        config_file=SHELL_CONFIG_FILENAME,
        config_text="echo hi\r\n",
    )
    assert (shell_dir / SHELL_CONFIG_FILENAME).read_text(encoding="utf-8") == "echo hi\n"

    long_line = "echo " + "x" * 140
    preview, search = build_task_preview_and_search(
        task_kind=TASK_KIND_SHELL,
        config_text=f"# ignored\n{long_line}\necho second\n",
        task_name="shell-task",
        notes="note",
    )
    assert preview.endswith("...")
    assert len(preview) == 120
    assert "shell-task" in search


def test_task_payload_rejects_config_file_escape(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task_dir = tasks_dir / "safe"
    task_dir.mkdir(parents=True)
    outside = tasks_dir / "secret.yaml"
    outside.write_text("token: do-not-read\n", encoding="utf-8")

    kind, config, text, error = read_task_payload(
        str(task_dir),
        {"task_kind": TASK_KIND_CONFIG, "config_file": "../secret.yaml"},
    )

    assert kind == TASK_KIND_CONFIG
    assert isinstance(config, DictConfig)
    assert config == {}
    assert text == ""
    assert "outside the task directory" in error


def test_task_info_rejects_symlinked_task_directory_escape(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    outside = tmp_path / "outside-task"
    outside.mkdir()
    (outside / TASK_INFO_FILENAME).write_text('{"name":"outside"}', encoding="utf-8")
    link = tasks_dir / "linked"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert load_task_info(str(link)) == {}
    with pytest.raises(ValueError, match="outside the tasks directory"):
        load_task_info(str(link), raise_error=True)


def test_task_info_rejects_symlinked_tasks_root(tmp_path):
    outside = tmp_path / "outside-tasks"
    task_dir = outside / "safe"
    task_dir.mkdir(parents=True)
    (task_dir / TASK_INFO_FILENAME).write_text('{"name":"outside"}', encoding="utf-8")
    tasks_link = tmp_path / "tasks"
    try:
        tasks_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    linked_task = tasks_link / "safe"
    assert load_task_info(str(linked_task)) == {}
    with pytest.raises(ValueError, match="Tasks directory must not be"):
        load_task_info(str(linked_task), raise_error=True)


def test_task_info_rejects_symlinked_workspace_ancestor(tmp_path):
    import pyruns.utils.info_io as info_io

    managed_root = tmp_path / DEFAULT_ROOT_NAME
    managed_root.mkdir()
    outside_workspace = tmp_path / "outside-workspace"
    task_dir = outside_workspace / "tasks" / "safe"
    task_dir.mkdir(parents=True)
    (task_dir / TASK_INFO_FILENAME).write_text('{"name":"outside"}', encoding="utf-8")
    workspace_link = managed_root / "train"
    try:
        workspace_link.symlink_to(outside_workspace, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    linked_task = workspace_link / "tasks" / "safe"
    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        info_io.validate_task_directory(str(linked_task))
    assert load_task_info(str(linked_task)) == {}


def test_task_info_rejects_simulated_reparse_workspace_ancestor(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    task_dir = workspace / "tasks" / "safe"
    task_dir.mkdir(parents=True)
    (task_dir / TASK_INFO_FILENAME).write_text('{"name":"safe"}', encoding="utf-8")
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(workspace)):
            return True
        return real_check(path)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        info_io.validate_task_directory(str(task_dir))


def test_task_info_rejects_reparse_tasks_root_without_following_it(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    tasks_dir = tmp_path / "tasks"
    task_dir = tasks_dir / "safe"
    task_dir.mkdir(parents=True)
    (task_dir / TASK_INFO_FILENAME).write_text('{"name":"safe"}', encoding="utf-8")

    class ReparseStat:
        st_file_attributes = 0x400

    with monkeypatch.context() as patcher:
        patcher.setattr(info_io.os.path, "islink", lambda _path: False)
        if hasattr(info_io.os.path, "isjunction"):
            patcher.setattr(info_io.os.path, "isjunction", lambda _path: False)
        patcher.setattr(info_io.os, "lstat", lambda _path: ReparseStat())

        assert info_io._path_is_link_or_reparse(str(tasks_dir)) is True
        with pytest.raises(ValueError, match="reparse point"):
            load_task_info(str(task_dir), raise_error=True)


def test_task_info_and_logs_reject_nested_symlink_escapes(tmp_path):
    task_dir = tmp_path / "tasks" / "safe"
    task_dir.mkdir(parents=True)
    outside_info = tmp_path / "outside-info.json"
    outside_info.write_text('{"name":"outside"}', encoding="utf-8")
    info_link = task_dir / TASK_INFO_FILENAME
    outside_logs = tmp_path / "outside-logs"
    outside_logs.mkdir()
    (outside_logs / "run1.log").write_text("secret\n", encoding="utf-8")
    logs_link = task_dir / RUN_LOGS_DIR
    try:
        info_link.symlink_to(outside_info)
        logs_link.symlink_to(outside_logs, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    assert load_task_info(str(task_dir)) == {}
    with pytest.raises(ValueError, match="workspace boundary"):
        load_task_info(str(task_dir), raise_error=True)
    assert get_log_options(str(task_dir)) == {}
    empty_preview, _ = build_task_preview_and_search(task_kind=TASK_KIND_SHELL, config_text="# only comments\n")
    assert empty_preview == "(empty shell script)"


def test_normalize_log_newlines_leaves_terminal_stream_unchanged():
    text = "progress 1/3\rprogress 2/3\nfinished\n"

    normalized = normalize_log_newlines(text)

    assert normalized == text
    assert "progress 1/3\r\nprogress 2/3" not in normalized


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


def test_decode_log_bytes_falls_back_to_gbk_for_windows_logs(monkeypatch):
    text = "测试 PowerShell 输出"
    encoded = text.encode("gbk")
    monkeypatch.setattr(log_io.locale, "getpreferredencoding", lambda _do_setlocale=False: "ascii")
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
    assert safe_read_log("missing.log", 7) == ("", 7)

    monkeypatch.setattr(log_io.os.path, "getsize", lambda _path: 10)
    monkeypatch.setattr(builtins, "open", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("open failed")))
    assert safe_read_log("missing.log", 7) == ("", 7)


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


@pytest.mark.parametrize("terminal_status", ["zombie", "dead"])
def test_process_utils_treats_terminal_psutil_status_as_exited(monkeypatch, terminal_status):
    class TerminalProcess:
        def status(self):
            return terminal_status

    class TerminalPsutil:
        STATUS_ZOMBIE = "zombie"
        STATUS_DEAD = "dead"

        @staticmethod
        def Process(_pid):
            return TerminalProcess()

    monkeypatch.setattr(process_utils, "_psutil", TerminalPsutil())

    assert process_utils.is_pid_running(4242) is False
    assert process_utils._process_identity_is_alive((4242, 123.0)) is False


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


def test_kill_process_posix(monkeypatch):
    calls = []
    monkeypatch.setattr(process_utils.os, "name", "posix", raising=False)
    monkeypatch.setattr(process_utils.os, "getpgrp", lambda: 1, raising=False)
    monkeypatch.setattr(process_utils.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        process_utils.os,
        "killpg",
        lambda pid, sig: calls.append(("pg", pid, sig)),
        raising=False,
    )
    monkeypatch.setattr(
        process_utils,
        "_process_tree_identities",
        lambda _pid: [(99999, 1.0)],
    )
    monkeypatch.setattr(process_utils, "_process_identity_is_alive", lambda _identity: True)
    monkeypatch.setattr(process_utils, "_wait_for_process_tree_exit", lambda *_args, **_kwargs: True)

    assert kill_process(99999) is True

    assert calls == [("pg", 99999, signal.SIGTERM)]


def test_kill_process_posix_escalates_process_group(monkeypatch):
    calls = []
    waits = iter([False, True])
    monkeypatch.setattr(process_utils.os, "name", "posix", raising=False)
    monkeypatch.setattr(process_utils, "_POSIX_KILL_GRACE_SEC", 0)
    monkeypatch.setattr(process_utils.os, "getpgrp", lambda: 1, raising=False)
    monkeypatch.setattr(process_utils.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(
        process_utils.os,
        "killpg",
        lambda pid, sig: calls.append(("pg", pid, sig)),
        raising=False,
    )
    monkeypatch.setattr(
        process_utils,
        "_process_tree_identities",
        lambda _pid: [(99999, 1.0)],
    )
    monkeypatch.setattr(process_utils, "_process_identity_is_alive", lambda _identity: True)
    monkeypatch.setattr(
        process_utils,
        "_wait_for_process_tree_exit",
        lambda *_args, **_kwargs: next(waits),
    )

    assert kill_process(99999) is True

    assert calls == [
        ("pg", 99999, signal.SIGTERM),
        ("pg", 99999, getattr(signal, "SIGKILL", signal.SIGTERM)),
    ]


@patch(
    "pyruns.utils.process_utils.hidden_subprocess_kwargs",
    return_value={"creationflags": 0x08000000},
)
@patch("pyruns.utils.process_utils.os.name", "nt")
@patch("subprocess.run")
def test_kill_process_nt(mock_run, mock_hidden_subprocess_kwargs):
    kill_process(99999)
    mock_hidden_subprocess_kwargs.assert_called_once_with()
    mock_run.assert_called_with(
        ["taskkill", "/F", "/T", "/PID", "99999"],
        capture_output=True, timeout=5,
        creationflags=0x08000000,
    )


def test_kill_process_nt_falls_back_to_snapshotted_tree(monkeypatch):
    killed = []
    create_times = {111: 1.0, 222: 2.0}

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return create_times[self.pid]

        def kill(self):
            killed.append(self.pid)

    class FakePsutil:
        Process = FakeProcess

    waits = iter([False, True])
    monkeypatch.setattr(process_utils.os, "name", "nt", raising=False)
    monkeypatch.setattr(process_utils, "_psutil", FakePsutil())
    monkeypatch.setattr(process_utils, "hidden_subprocess_kwargs", lambda: {})
    monkeypatch.setattr(
        process_utils,
        "_process_tree_identities",
        lambda _pid: [(111, 1.0), (222, 2.0)],
    )
    monkeypatch.setattr(
        process_utils,
        "_wait_for_process_tree_exit",
        lambda *_args, **_kwargs: next(waits),
    )
    monkeypatch.setattr(
        process_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: type("Result", (), {"returncode": 5})(),
    )

    assert kill_process(111) is True
    assert killed == [222, 111]


def test_windows_force_kill_skips_reused_process_identity(monkeypatch):
    killed = []

    class FakeProcess:
        def __init__(self, pid):
            self.pid = pid

        def create_time(self):
            return 20.0 if self.pid == 222 else 1.0

        def kill(self):
            killed.append(self.pid)

    class FakePsutil:
        Process = FakeProcess

    monkeypatch.setattr(process_utils, "_psutil", FakePsutil())

    process_utils._force_kill_windows_process_tree([(111, 1.0), (222, 2.0)])

    assert killed == [111]


def test_kill_process_exception_caught():
    # Just to ensure it doesn't raise if the underlying call fails
    with patch("pyruns.utils.process_utils.os.name", "posix"):
        with patch("os.kill", side_effect=Exception("mock error")):
            # Should not raise exception
            kill_process(99999)


def test_kill_process_posix_returns_when_group_or_fallback_process_is_missing(monkeypatch):
    calls = []
    monkeypatch.setattr(process_utils.os, "name", "posix", raising=False)
    monkeypatch.setattr(process_utils.os, "getpgrp", lambda: 1, raising=False)
    monkeypatch.setattr(process_utils.os, "getpgid", lambda pid: pid, raising=False)
    monkeypatch.setattr(process_utils, "_process_identity_is_alive", lambda _identity: True)
    monkeypatch.setattr(process_utils, "_wait_for_process_tree_exit", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        process_utils,
        "_process_tree_identities",
        lambda pid: [(pid, 1.0)],
    )

    def missing_group(pid, sig):
        calls.append(("pg", pid, sig))
        raise ProcessLookupError()

    def process_signal(pid, sig):
        calls.append(("pid", pid, sig))

    monkeypatch.setattr(process_utils.os, "killpg", missing_group, raising=False)
    monkeypatch.setattr(process_utils.os, "kill", process_signal)
    assert kill_process(111) is True
    assert calls == [("pg", 111, signal.SIGTERM), ("pid", 111, signal.SIGTERM)]

    calls.clear()

    def group_fails(pid, sig):
        calls.append(("pg", pid, sig))
        raise OSError("no process group")

    def process_missing(pid, sig):
        calls.append(("pid", pid, sig))
        raise ProcessLookupError()

    monkeypatch.setattr(process_utils.os, "killpg", group_fails, raising=False)
    monkeypatch.setattr(process_utils.os, "kill", process_missing)
    assert kill_process(222) is True
    assert calls == [("pg", 222, signal.SIGTERM), ("pid", 222, signal.SIGTERM)]


def test_kill_process_posix_without_killpg_escalates_live_process(monkeypatch):
    calls = []
    waits = iter([False, False])
    monkeypatch.setattr(process_utils.os, "name", "posix", raising=False)
    monkeypatch.setattr(process_utils.os, "killpg", None, raising=False)
    monkeypatch.setattr(process_utils, "_POSIX_KILL_GRACE_SEC", 0)
    monkeypatch.setattr(
        process_utils,
        "_process_tree_identities",
        lambda _pid: [(333, 1.0)],
    )
    monkeypatch.setattr(process_utils, "_process_identity_is_alive", lambda _identity: True)
    monkeypatch.setattr(
        process_utils,
        "_wait_for_process_tree_exit",
        lambda *_args, **_kwargs: next(waits),
    )
    monkeypatch.setattr(process_utils.os, "kill", lambda pid, sig: calls.append((pid, sig)))

    assert kill_process(333) is False

    assert calls == [
        (333, signal.SIGTERM),
        (333, getattr(signal, "SIGKILL", signal.SIGTERM)),
    ]


def test_signal_posix_process_tree_covers_independent_descendant_groups(monkeypatch):
    calls = []
    identities = [(100, 1.0), (200, 2.0), (201, 3.0)]
    groups = {100: 100, 200: 200, 201: 200}
    monkeypatch.setattr(process_utils, "_process_identity_is_alive", lambda _identity: True)
    monkeypatch.setattr(process_utils.os, "getpgrp", lambda: 50, raising=False)
    monkeypatch.setattr(
        process_utils.os,
        "getpgid",
        lambda pid: groups[pid],
        raising=False,
    )
    monkeypatch.setattr(
        process_utils.os,
        "killpg",
        lambda pgid, sig: calls.append(("pg", pgid, sig)),
        raising=False,
    )
    monkeypatch.setattr(
        process_utils.os,
        "kill",
        lambda pid, sig: calls.append(("pid", pid, sig)),
    )

    process_utils._signal_posix_process_tree(identities, signal.SIGTERM)

    assert calls == [
        ("pg", 100, signal.SIGTERM),
        ("pg", 200, signal.SIGTERM),
        ("pid", 201, signal.SIGTERM),
    ]


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


def test_sort_tasks_for_manager_uses_natural_name_tiebreaker():
    tasks = [
        {
            "name": "task_2026-05-28_02-25-46_10-of-18",
            "status": "pending",
            "created_at": "2026-05-28_02-25-46",
        },
        {
            "name": "task_2026-05-28_02-25-46_2-of-18",
            "status": "pending",
            "created_at": "2026-05-28_02-25-46",
        },
        {
            "name": "task_2026-05-28_02-25-46_1-of-18",
            "status": "pending",
            "created_at": "2026-05-28_02-25-46",
        },
    ]

    assert [task["name"] for task in sort_tasks_for_manager(tasks)] == [
        "task_2026-05-28_02-25-46_1-of-18",
        "task_2026-05-28_02-25-46_2-of-18",
        "task_2026-05-28_02-25-46_10-of-18",
    ]


def test_sort_tasks_for_manager_supports_explicit_card_orders():
    tasks = [
        {
            "name": "alpha",
            "status": "completed",
            "created_at": "2026-06-03_10-00-00",
            "task_order": 2,
        },
        {
            "name": "task10",
            "status": "pending",
            "created_at": "2026-06-01_10-00-00",
            "task_order": 0,
        },
        {
            "name": "task2",
            "status": "failed",
            "created_at": "2026-06-02_10-00-00",
            "task_order": 1,
        },
        {
            "name": "pinned",
            "status": "pending",
            "created_at": "2026-05-01_10-00-00",
            "pinned": True,
        },
    ]

    assert [task["name"] for task in sort_tasks_for_manager(tasks, "manual")] == [
        "pinned", "task10", "task2", "alpha",
    ]
    assert [task["name"] for task in sort_tasks_for_manager(tasks, "activity_desc")] == [
        "pinned", "alpha", "task2", "task10",
    ]
    assert [task["name"] for task in sort_tasks_for_manager(tasks, "activity_asc")] == [
        "pinned", "task10", "task2", "alpha",
    ]
    assert [task["name"] for task in sort_tasks_for_manager(tasks, "name_asc")] == [
        "pinned", "alpha", "task2", "task10",
    ]
    assert [task["name"] for task in sort_tasks_for_manager(tasks, "name_desc")] == [
        "pinned", "task10", "task2", "alpha",
    ]

    with pytest.raises(ValueError, match="Unknown task sort mode"):
        sort_tasks_for_manager(tasks, "unsupported")


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
    with open(file_path, "r", encoding="utf-8") as f:
        assert "monitor_line_height:" in f.read()
    
    # 2. File exists (should not overwrite)
    with open(file_path, "w", encoding="utf-8") as f:
        f.write("custom_key: 123")
    settings.ensure_settings_file(root_dir)
    with open(file_path, "r", encoding="utf-8") as f:
        assert f.read() == "custom_key: 123"


def test_ensure_settings_file_publishes_only_complete_content(tmp_path, monkeypatch):
    path = tmp_path / SETTINGS_FILENAME
    real_link = os.link
    observed = []

    def inspect_atomic_publish(source, destination):
        assert Path(destination) == path
        assert not path.exists()
        assert Path(source).read_text(encoding="utf-8") == settings.SETTINGS_TEMPLATE
        observed.append(Path(source))
        return real_link(source, destination)

    monkeypatch.setattr(settings.os, "link", inspect_atomic_publish)

    assert settings.ensure_settings_file(str(tmp_path)) == str(path)
    assert observed
    assert path.read_text(encoding="utf-8") == settings.SETTINGS_TEMPLATE
    assert not list(tmp_path.glob(f".{SETTINGS_FILENAME}.*.tmp"))


def test_ensure_settings_file_preserves_concurrent_winner(tmp_path, monkeypatch):
    path = tmp_path / SETTINGS_FILENAME
    winner = "ui_port: 9001\n"

    def publish_competing_file(_source, destination):
        Path(destination).write_text(winner, encoding="utf-8")
        raise FileExistsError(destination)

    monkeypatch.setattr(settings.os, "link", publish_competing_file)

    assert settings.ensure_settings_file(str(tmp_path)) == str(path)
    assert path.read_text(encoding="utf-8") == winner
    assert not list(tmp_path.glob(f".{SETTINGS_FILENAME}.*.tmp"))


def test_ensure_config_default_publishes_only_complete_content(tmp_path, monkeypatch):
    import pyruns

    path = tmp_path / CONFIG_DEFAULT_FILENAME
    real_link = os.link
    observed = []

    def inspect_atomic_publish(source, destination):
        assert Path(destination) == path
        assert not path.exists()
        assert Path(source).read_text(encoding="utf-8") == "# task config here"
        observed.append(Path(source))
        return real_link(source, destination)

    monkeypatch.setattr(pyruns.os, "link", inspect_atomic_publish)

    assert pyruns.ensure_config_default(str(tmp_path)) == str(path)
    assert observed
    assert path.read_text(encoding="utf-8") == "# task config here"
    assert not list(tmp_path.glob(f".{CONFIG_DEFAULT_FILENAME}.*.tmp"))


def test_ensure_config_default_cleans_up_when_sync_fails(tmp_path, monkeypatch):
    import pyruns

    path = tmp_path / CONFIG_DEFAULT_FILENAME
    monkeypatch.setattr(
        pyruns.os,
        "fsync",
        lambda _fd: (_ for _ in ()).throw(OSError("sync failed")),
    )

    with pytest.raises(OSError, match="sync failed"):
        pyruns.ensure_config_default(str(tmp_path))

    assert not path.exists()
    assert not list(tmp_path.glob(f".{CONFIG_DEFAULT_FILENAME}.*.tmp"))


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
    assert "new_key" not in cfg2
    # Defaults still present
    assert cfg2["header_refresh_interval"] == settings.SETTINGS_DEFAULTS["header_refresh_interval"]


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
        settings.save_setting("gpu_scheduler_device_ids", [0, 1])
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            # Accept both formats: YAML dump may use "key:\n- val" or "key: \n- val"
            assert "gpu_scheduler_device_ids" in text
            assert "- 0\n- 1" in text

        assert settings._cached["gpu_scheduler_device_ids"] == [0, 1]

        # 4. Update the list again
        settings.save_setting("gpu_scheduler_device_ids", [2])
        with open(file_path, "r", encoding="utf-8") as f:
            text = f.read()
            assert "gpu_scheduler_device_ids" in text
            assert "- 2" in text
            assert "- 0" not in text


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

    def test_load_missing_file_raises_when_requested(self, tmp_path):
        with pytest.raises(FileNotFoundError, match=TASK_INFO_FILENAME):
            load_task_info(str(tmp_path), raise_error=True)

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

    def test_rejects_run_index_that_would_expand_history_without_bound(self, tmp_path):
        path = tmp_path / TASK_INFO_FILENAME
        path.write_text(json.dumps({"name": "bomb", "run_index": MAX_RUN_HISTORY_SLOTS + 1}), encoding="utf-8")

        assert path.stat().st_size < 128
        assert MAX_RUN_HISTORY_SLOTS <= 1_000
        assert load_task_info(str(tmp_path)) == {}
        with pytest.raises(ValueError, match="run history"):
            load_task_info(str(tmp_path), raise_error=True)

    def test_rejects_oversized_task_info_before_json_decode(self, tmp_path, monkeypatch):
        path = tmp_path / TASK_INFO_FILENAME
        path.write_bytes(b" " * (MAX_TASK_INFO_BYTES + 1))
        monkeypatch.setattr(json, "loads", lambda _value: (_ for _ in ()).throw(AssertionError("must not decode")))

        with pytest.raises(ValueError, match="too large"):
            load_task_info(str(tmp_path), raise_error=True)

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

    def test_load_retries_transient_read_permission_error(self, tmp_path):
        task_dir = str(tmp_path)
        save_task_info(task_dir, {"name": "retry-test", "status": "pending"})
        from pyruns.utils import info_io

        real_load = info_io._load_json_object
        calls = {"count": 0}

        def flaky_load(path, *, max_bytes, label):
            if calls["count"] == 0:
                calls["count"] += 1
                raise PermissionError("temporarily locked")
            return real_load(path, max_bytes=max_bytes, label=label)

        with patch("pyruns.utils.info_io._load_json_object", side_effect=flaky_load), patch(
            "pyruns.utils.info_io.time.sleep"
        ) as sleep:
            loaded = load_task_info(task_dir, raise_error=True)

        assert calls["count"] == 1
        sleep.assert_called_once()
        assert loaded["status"] == "pending"

    def test_task_info_lock_recovers_stale_process_lock_file(self, tmp_path):
        task_dir = str(tmp_path)
        lock_path = tmp_path / f".{TASK_INFO_FILENAME}.lock"
        lock_path.write_text("0 0", encoding="utf-8")

        with task_info_lock(task_dir, timeout_sec=0.01):
            assert lock_path.exists()

        assert not lock_path.exists()


def test_load_record_data_handles_records_empty_payloads_and_missing_files(tmp_path):
    missing_dir = tmp_path / "missing"
    assert load_record_data(str(missing_dir)) == []

    empty_dir = tmp_path / "empty"
    empty_dir.mkdir()
    save_task_info(str(empty_dir), {"name": "test"})
    assert load_record_data(str(empty_dir)) == []

    records_dir = tmp_path / "records"
    records_dir.mkdir()
    save_task_info(str(records_dir), {RECORDS_KEY: [{"loss": 0.5}, {"loss": 0.1}]})
    assert load_record_data(str(records_dir)) == [{"loss": 0.5}, {"loss": 0.1}]


def test_get_log_options_handles_empty_and_naturally_sorted_run_logs(tmp_path):
    task_dir = str(tmp_path)
    assert get_log_options(task_dir) == {}

    log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
    os.makedirs(log_dir)
    for name in ["run1.log", "run2.log", "run10.log"]:
        Path(log_dir, name).touch()

    options = get_log_options(task_dir)
    assert list(options) == ["run1.log", "run2.log", "run10.log"]
    assert all(os.path.isfile(path) for path in options.values())


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

    def test_resolve_failed_run_without_run_log_falls_back_to_error_log(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
        os.makedirs(log_dir)
        error_log = os.path.join(log_dir, "error.log")
        Path(error_log).write_text("startup failed", encoding="utf-8")
        save_task_info(
            task_dir,
            {
                "name": "broken",
                "status": "failed",
                "run_index": 2,
                "start_times": ["old", ""],
                "finish_times": ["old", "now"],
            },
        )

        assert resolve_log_path(task_dir) == error_log

    def test_resolve_queued_rerun_prefers_queue_log(self, tmp_path):
        task_dir = str(tmp_path)
        log_dir = os.path.join(task_dir, RUN_LOGS_DIR)
        os.makedirs(log_dir)
        Path(log_dir, "run1.log").write_text("old run", encoding="utf-8")
        queue_log = Path(log_dir, "queue.log")
        queue_log.write_text("waiting", encoding="utf-8")
        save_task_info(task_dir, {"name": "queued", "status": "queued", "run_index": 1})

        assert resolve_log_path(task_dir) == str(queue_log)

    def test_resolve_no_logs(self, tmp_path):
        assert resolve_log_path(str(tmp_path)) is None


# ═══════════════════════════════════════════════════════════════
#  config_utils — core config & batch generation logic
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  safe_filename
# ═══════════════════════════════════════════════════════════════

def test_safe_filename_normalizes_supported_fallback_cases():
    cases = {
        "hello-world": "hello-world",
        "my file name": "my_file_name",
        "a/b:c*d": "abcd",
        "": "config",
        "***": "config",
    }
    for value, expected in cases.items():
        assert safe_filename(value) == expected, value


# ═══════════════════════════════════════════════════════════════
#  parse_value
# ═══════════════════════════════════════════════════════════════

def test_parse_value_preserves_expected_scalar_and_collection_types():
    cases = [
        ("42", 42),
        ("3.14", 3.14),
        ("True", True),
        ("true", True),
        ("False", False),
        ("false", False),
        (True, True),
        (False, False),
        ("[1, 2, 3]", [1, 2, 3]),
        ("hello world", "hello world"),
        ("foo: bar", "foo: bar"),
        ("", None),
        ("0.001 | 0.01 | 0.1", "0.001 | 0.01 | 0.1"),
    ]
    for value, expected in cases:
        result = parse_value(value)
        assert result == expected, value
        assert type(result) is type(expected), value


def test_parse_value_preserves_unresolved_interpolations(monkeypatch):
    monkeypatch.setenv("PYRUNS_TEST_SECRET", "must-not-leak")

    assert parse_value("${oc.env:PYRUNS_TEST_SECRET}") == (
        "${oc.env:PYRUNS_TEST_SECRET}"
    )
    assert parse_value(
        "['${oc.env:PYRUNS_TEST_SECRET}', '${missing.reference}']"
    ) == ["${oc.env:PYRUNS_TEST_SECRET}", "${missing.reference}"]


def test_parse_value_preserves_multiline_text_that_looks_like_extra_yaml_keys():
    value = "foo\nother: x"

    assert parse_value(value) == value


# ═══════════════════════════════════════════════════════════════
#  flatten / unflatten
# ═══════════════════════════════════════════════════════════════

class TestFlattenUnflatten:
    def test_flatten_unflatten_handles_flat_nested_and_roundtrip_data(self):
        assert flatten_dict({"a": 1, "b": 2}) == {"a": 1, "b": 2}

        nested = {"model": {"name": "resnet", "layers": 50}, "lr": 0.01}
        assert flatten_dict(nested) == {
            "model.name": "resnet",
            "model.layers": 50,
            "lr": 0.01,
        }

        original = {"a": {"b": {"c": 1}}, "x": 2}
        assert unflatten_dict(flatten_dict(original)) == original
        assert unflatten_dict({"a.b": 1, "a.c": 2, "d": 3}) == {
            "a": {"b": 1, "c": 2},
            "d": 3,
        }

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

    def test_dictconfig_interpolations_remain_unresolved(self, monkeypatch):
        monkeypatch.setenv("PYRUNS_TEST_SECRET", "must-not-leak")
        config = OmegaConf.create(
            {
                "root": "/tmp/run",
                "output": "${root}/results",
                "secret": "${oc.env:PYRUNS_TEST_SECRET}",
            }
        )

        assert flatten_dict(config) == {
            "root": "/tmp/run",
            "output": "${root}/results",
            "secret": "${oc.env:PYRUNS_TEST_SECRET}",
        }
        preview, search_text = build_config_preview_and_search_text(config)
        assert "must-not-leak" not in preview
        assert "must-not-leak" not in search_text
        assert "${oc.env:pyruns_test_secret}" in search_text

    def test_listconfig_interpolations_remain_unresolved(self, monkeypatch):
        monkeypatch.setenv("PYRUNS_TEST_SECRET", "must-not-leak")
        config = OmegaConf.create(
            {
                "items": [
                    "${oc.env:PYRUNS_TEST_SECRET}",
                    {"nested": "${oc.env:PYRUNS_TEST_SECRET}"},
                ]
            }
        )

        flat = flatten_dict(config)
        assert "must-not-leak" not in str(flat["items"])
        preview, search_text = build_config_preview_and_search_text(config)
        assert "must-not-leak" not in preview
        assert "must-not-leak" not in search_text
        assert "${oc.env:pyruns_test_secret}" in search_text

    def test_batch_candidate_interpolation_remains_unresolved(self, monkeypatch):
        monkeypatch.setenv("PYRUNS_TEST_SECRET", "must-not-leak")
        config = OmegaConf.create(
            {"choice": "${oc.env:PYRUNS_TEST_SECRET} | public"}
        )

        generated = generate_batch_configs(config)
        values = [
            OmegaConf.to_container(item, resolve=False)["choice"]
            for item in generated
        ]
        assert values == ["${oc.env:PYRUNS_TEST_SECRET}", "public"]
        assert all("must-not-leak" not in str(value) for value in values)

    def test_missing_environment_interpolation_does_not_break_preview_or_batch(self, monkeypatch):
        monkeypatch.delenv("PYRUNS_TEST_MISSING", raising=False)
        config = OmegaConf.create(
            {
                "learning_rate": "0.1 | 0.2",
                "output": "${oc.env:PYRUNS_TEST_MISSING}",
            }
        )

        assert "output=${oc.env:" in preview_config_line(config)
        assert count_batch_configs(config) == 2
        generated = generate_batch_configs(config)
        assert [item["learning_rate"] for item in generated] == [0.1, 0.2]
        assert [
            OmegaConf.to_container(item, resolve=False)["output"] for item in generated
        ] == ["${oc.env:PYRUNS_TEST_MISSING}"] * 2


# ═══════════════════════════════════════════════════════════════
#  YAML / JSON I/O
# ═══════════════════════════════════════════════════════════════

class TestYamlIO:
    def test_save_and_load(self, tmp_path):
        data = {"lr": 0.01, "model": {"name": "vgg"}}
        path = str(tmp_path / "test.yaml")
        save_yaml(path, data)
        loaded = load_yaml(path)
        assert loaded == data

    def test_load_nonexistent(self, tmp_path):
        assert load_yaml(str(tmp_path / "nope.yaml")) == {}

    def test_load_non_dict_yaml(self, tmp_path):
        path = str(tmp_path / "list.yaml")
        with open(path, "w") as f:
            f.write("- a\n- b\n")
        assert load_yaml(path) == {}

    def test_load_yaml_strict_raises_on_non_mapping(self, tmp_path):
        path = str(tmp_path / "list.yaml")
        with open(path, "w") as f:
            f.write("- a\n- b\n")
        with pytest.raises(ValueError, match="mapping"):
            load_yaml_strict(path)

    def test_load_yaml_strict_raises_on_invalid_yaml(self, tmp_path):
        path = str(tmp_path / "bad.yaml")
        with open(path, "w", encoding="utf-8") as f:
            f.write("a: [1, 2\n")
        with pytest.raises(ValueError, match=r"Invalid YAML.*bad\.yaml"):
            load_yaml_strict(path)

    def test_yaml_io_rejects_oversized_documents_before_parse_or_publish(self, tmp_path):
        oversized = tmp_path / "oversized.yaml"
        oversized.write_bytes(b"x" * (MAX_CONFIG_FILE_BYTES + 1))
        assert load_yaml(str(oversized)) == {}
        with pytest.raises(ValueError, match="too large"):
            load_yaml_strict(str(oversized))

        output = tmp_path / "output.yaml"
        with pytest.raises(ValueError, match="too large"):
            save_yaml(str(output), {"value": "x" * MAX_CONFIG_FILE_BYTES})
        assert not output.exists()

    def test_save_yaml_is_atomic_when_replace_fails(self, tmp_path, monkeypatch):
        path = tmp_path / "config.yaml"
        path.write_text("value: old\n", encoding="utf-8")
        monkeypatch.setattr(
            config_utils,
            "_replace_with_retry",
            lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
        )

        with pytest.raises(OSError, match="replace failed"):
            save_yaml(str(path), {"value": "new"})

        assert path.read_text(encoding="utf-8") == "value: old\n"
        assert not list(tmp_path.glob(".config.yaml.*.tmp"))

    def test_list_yaml_files(self, tmp_path):
        for name in ["a.yaml", "b.yml", "c.txt"]:
            (tmp_path / name).write_text("x: 1")
        result = list_yaml_files(str(tmp_path))
        assert "a.yaml" in result
        assert "b.yml" in result
        assert "c.txt" not in result

    def test_list_yaml_files_missing_dir(self):
        assert list_yaml_files("/nonexistent/dir") == []


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

    def test_task_templates_follow_manager_order_after_default(self, tmp_path):
        run_root = str(tmp_path)
        save_yaml(os.path.join(run_root, "config_default.yaml"), {"lr": 0.01})
        tasks_dir = os.path.join(run_root, "tasks")

        def add_task(name, **info):
            task_dir = os.path.join(tasks_dir, name)
            os.makedirs(task_dir, exist_ok=True)
            save_yaml(os.path.join(task_dir, "config.yaml"), {"name": name})
            save_task_info(
                task_dir,
                {
                    "name": name,
                    "status": "pending",
                    "created_at": "2026-05-28_02-25-46",
                    **info,
                },
            )

        add_task("task_2026-05-28_02-25-46_10-of-18")
        add_task("task_2026-05-28_02-25-46_2-of-18")
        add_task("task_2026-05-28_02-25-46_1-of-18")
        add_task("running-old", status="running", start_times=["2026-05-28_02-25-00"])

        result = list_template_files(run_root)

        assert list(result.values())[:5] == [
            "config_default.yaml",
            "running-old",
            "task_2026-05-28_02-25-46_1-of-18",
            "task_2026-05-28_02-25-46_2-of-18",
            "task_2026-05-28_02-25-46_10-of-18",
        ]

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

def test_preview_config_line_formats_scalars_and_applies_display_limits():
    line = preview_config_line({"lr": 0.01, "bs": 32, "opt": "adam"})
    assert "lr=0.01" in line
    assert "bs=32" in line

    nested = preview_config_line({"lr": 0.01, "model": {"name": "resnet"}})
    assert "model" not in nested

    limited = preview_config_line({f"k{i}": i for i in range(10)}, max_items=2)
    assert limited.count("=") == 2
    assert preview_config_line("not a dict") == ""


# ═══════════════════════════════════════════════════════════════
#  _parse_pipe_value
# ═══════════════════════════════════════════════════════════════

class TestParsePipeValue:
    def test_supported_product_zip_and_range_syntax(self):
        cases = [
            ("0.001 | 0.01 | 0.1", ["0.001", "0.01", "0.1"], "product"),
            ("(a | b | c)", ["a", "b", "c"], "zip"),
            ("  a | b | c  ", ["a", "b", "c"], "product"),
            ("30:40:1", list(range(30, 40)), "product"),
        ]
        for value, expected_parts, expected_mode in cases:
            result = _parse_pipe_value(value)
            assert result is not None, value
            parts, mode = result
            assert list(parts) == expected_parts, value
            assert mode == expected_mode, value

    def test_rejects_non_batch_values_and_invalid_ranges(self):
        assert _split_by_pipe("") == []
        invalid = [
            "hello",
            "0.001",
            42,
            [1, 2],
            True,
            "(only_one)",
            "(5, 1)",
            "(1, 5, 0)",
            "(1, nope)",
            "1:5:0",
            "1:nope",
        ]
        for value in invalid:
            assert _parse_pipe_value(value) is None, value


# ═══════════════════════════════════════════════════════════════
#  generate_batch_configs
# ═══════════════════════════════════════════════════════════════

class TestGenerateBatchConfigs:
    def test_no_pipes_returns_original(self, sample_config):
        """No pipe syntax → returns [original_config]."""
        configs = generate_batch_configs(sample_config)
        assert len(configs) == 1
        assert isinstance(configs[0], DictConfig)
        assert OmegaConf.to_container(configs[0], resolve=False) == sample_config

    def test_product_values_metadata_and_fixed_fields(self, sample_config_with_pipes):
        configs = generate_batch_configs(sample_config_with_pipes)
        assert len(configs) == 6
        assert sorted({c["lr"] for c in configs}) == [0.001, 0.01, 0.1]
        assert sorted({c["batch_size"] for c in configs}) == [32, 64]
        for c in configs:
            assert isinstance(c["lr"], (int, float))
            assert isinstance(c["batch_size"], (int, float))
            assert c["optimizer"] == "adam"
            assert c["model"] == {"name": "resnet", "layers": 50}
            assert isinstance(c["_meta_desc"], str)
            assert c["_meta_desc"]

    def test_mixed_product_and_zip(self, sample_config_mixed):
        configs = generate_batch_configs(sample_config_mixed)
        assert len(configs) == 18
        assert count_batch_configs(sample_config_mixed) == len(configs)

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
    def test_counts_plain_product_zip_range_and_mismatch_configs(
        self,
        sample_config,
        sample_config_with_pipes,
    ):
        assert count_batch_configs(sample_config) == 1
        assert count_batch_configs(sample_config_with_pipes) == 6  # 3 × 2

        cases = [
            ({"a": "(1 | 2 | 3)", "b": "(x | y | z)"}, 3),
            ({"a": "(1 | 2 | 3)", "b": "(x | y)"}, 0),
            ({"epochs": "30:40:1"}, 10),
        ]
        for config, expected in cases:
            assert count_batch_configs(config) == expected, config

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
    def test_keeps_first_batch_values_and_preserves_plain_nested_data(self, sample_config):
        product = strip_batch_pipes({"lr": "0.001 | 0.01 | 0.1", "bs": "32 | 64"})
        assert product == {"lr": 0.001, "bs": 32}

        zipped = strip_batch_pipes({"seed": "(1 | 2 | 3)", "tag": "(a | b | c)"})
        assert zipped == {"seed": 1, "tag": "a"}

        assert strip_batch_pipes(sample_config) == sample_config
        nested = strip_batch_pipes({"model": {"name": "resnet | vgg"}, "lr": 0.01})
        assert nested == {"model": {"name": "resnet"}, "lr": 0.01}



# ═══════════════════════════════════════════════════════════════
#  validate_task_name
# ═══════════════════════════════════════════════════════════════


class TestValidateTaskName:
    def test_accepts_supported_names_and_length_boundary(self):
        valid = ["hello", "my-experiment", "run_001", "中文任务名", "test 123", "a" * 200]
        for name in valid:
            assert validate_task_name(name) is None, name

    def test_rejects_empty_long_hidden_and_invalid_names(self):
        invalid_with_message = [
            ("", "empty"),
            ("   ", "empty"),
            ("a" * 201, "long"),
            (".hidden", "start with '.'"),
            ("..", "start with '.'"),
        ]
        for name, message in invalid_with_message:
            assert message in validate_task_name(name).lower(), name
        for name in ['a<b', 'a>b', 'a:b', 'a"b', 'a/b', 'a\\b', 'a|b', 'a?b', 'a*b']:
            assert validate_task_name(name) is not None, name

    @pytest.mark.parametrize(
        "name",
        [
            "-dangerous-option",
            " leading-space",
            "\tleading-tab",
            "trailing.",
            "trailing ",
            "trailing\t",
            "CON",
            "con.txt",
            "PRN",
            "AUX.log",
            "NUL",
            "COM1",
            "com9.json",
            "LPT1",
            "lpt9.txt",
        ],
    )
    def test_rejects_cross_platform_unsafe_names(self, name):
        assert validate_task_name(name) is not None


# ═══════════════════════════════════════════════════════════════
#  Type Validation, Multiline Search, Pipe Escaping
# ═══════════════════════════════════════════════════════════════


class TestTypeValidation:
    def test_accepts_exact_types_and_int_values_for_float_templates(self):
        cases = [
            ({"lr": 0.01, "name": "resnet"}, [{"lr": 0.05, "name": "vgg"}]),
            ({"lr": 0.01}, [{"lr": 1}]),
        ]
        for template, configs in cases:
            assert validate_config_types_against_template(template, configs) is None

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

    def test_search_matches_report_field_context_and_highlight_offsets(self):
        task = {
            "name": "alpha-train",
            "notes": "Owner: Research\nNeeds REVIEW before launch",
            "task_kind": TASK_KIND_CONFIG,
            "config": {"model": {"name": "ResNet50"}, "batch_size": 32},
        }

        matches = build_task_search_matches(task, "review\nname: resnet50")

        assert [(match["field"], match["location"]) for match in matches] == [
            ("notes", "Line 2"),
            ("config", "model.name"),
        ]
        for match in matches:
            highlighted = match["snippet"][match["match_start"]:match["match_end"]]
            assert highlighted
        assert matches[0]["snippet"][matches[0]["match_start"]:matches[0]["match_end"]] == "REVIEW"
        assert matches[1]["snippet"][matches[1]["match_start"]:matches[1]["match_end"]] == "name: ResNet50"

    def test_shell_search_matches_use_script_line_numbers(self):
        task = {
            "name": "shell-task",
            "notes": "",
            "task_kind": TASK_KIND_SHELL,
            "config_text": "# setup\necho TOKEN_A\nprintf TOKEN_B\necho TOKEN_C\nprintf TOKEN_D\necho TOKEN_E\n",
        }

        matches = build_task_search_matches(
            task,
            "token_a\ntoken_b\ntoken_c\ntoken_d\ntoken_e",
        )

        assert len(matches) == 5
        assert all(match["field"] == "script" for match in matches)
        assert matches[0]["location"] == "Line 2"

    def test_search_result_counts_repeated_matches_beyond_context_limit(self):
        task = {
            "name": "repeated-task",
            "notes": "\n".join(f"token on row {index}" for index in range(1, 11)),
            "task_kind": TASK_KIND_CONFIG,
            "config": {},
        }

        result = build_task_search_result(task, "token", limit=4)

        assert result["match_count"] == 10
        assert len(result["matches"]) == 4
        assert [match["location"] for match in result["matches"]] == [
            "Line 1",
            "Line 2",
            "Line 3",
            "Line 4",
        ]

    def test_search_result_reports_multiple_occurrences_on_one_line(self):
        task = {
            "name": "repeat-line",
            "notes": "token then TOKEN then token",
            "task_kind": TASK_KIND_CONFIG,
            "config": {},
        }

        result = build_task_search_result(task, "token")

        assert result["match_count"] == 3
        assert len(result["matches"]) == 3
        assert [
            match["snippet"][match["match_start"]:match["match_end"]]
            for match in result["matches"]
        ] == ["token", "TOKEN", "token"]

    def test_search_result_keeps_large_repeated_source_context_bounded(self):
        task = {
            "name": "large-line",
            "notes": "token " * 20_000,
            "task_kind": TASK_KIND_CONFIG,
            "config": {},
        }

        result = build_task_search_result(task, "token", limit=4)

        assert result["match_count"] == 20_000
        assert len(result["matches"]) == 4



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

    assert read_log(str(log_path)) == ""
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
    assert read_last_bytes(str(log_path), 20) == ("abcdefghij", 10)


def test_safe_read_log_handles_missing_complete_and_partial_lines(tmp_path):
    log_path = tmp_path / "run.log"

    assert safe_read_log(str(log_path), 5) == ("", 5)
    log_path.write_bytes(b"line1\nline2\npartial")

    text, offset = safe_read_log(str(log_path), 0, max_bytes=13)
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


def test_logger_configuration_is_idempotent_during_settings_import(monkeypatch):
    from pyruns.utils import log_utils

    root_logger = logging.getLogger(log_utils.get_library_root())
    original_handlers = list(root_logger.handlers)
    original_level = root_logger.level
    original_propagate = root_logger.propagate
    original_library_logger = log_utils._LIBRARY_ROOT_LOGGER
    reentered = False

    def reentrant_setting(key, default=None):
        nonlocal reentered
        if not reentered:
            reentered = True
            log_utils.configure_project_root_logger()
        return True if key == "log_enabled" else default

    try:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)

        monkeypatch.setattr(log_utils, "_LIBRARY_ROOT_LOGGER", None)
        monkeypatch.setattr("pyruns.utils.settings.get", reentrant_setting)

        log_utils.configure_project_root_logger()

        console_handlers = [
            handler
            for handler in root_logger.handlers
            if bool(getattr(handler, "_pyruns_console_handler", False))
        ]
        assert reentered is True
        assert len(console_handlers) == 1
    finally:
        for handler in list(root_logger.handlers):
            root_logger.removeHandler(handler)
            handler.close()
        for handler in original_handlers:
            root_logger.addHandler(handler)
        root_logger.setLevel(original_level)
        root_logger.propagate = original_propagate
        log_utils._LIBRARY_ROOT_LOGGER = original_library_logger


def test_console_logger_ignores_only_an_already_closed_output_stream(capsys):
    from pyruns.utils import log_utils

    stream = io.StringIO()
    handler = log_utils._CloseAwareStreamHandler(stream)
    logger = logging.Logger("pyruns.closed-stream-test")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

    logger.info("visible before close")
    assert stream.getvalue() == "visible before close\n"

    stream.close()
    logger.error("cannot be written")

    assert capsys.readouterr().err == ""


def test_console_logger_uses_ansi_only_for_tty_streams():
    from pyruns.utils import log_utils

    configured = log_utils._LOG_CONFIG["console"]["format"]
    redirected = io.StringIO()

    class TtyStream(io.StringIO):
        def isatty(self):
            return True

    assert "\x1b[" not in log_utils._console_format_for_stream(redirected, configured)
    assert "\x1b[" in log_utils._console_format_for_stream(TtyStream(), configured)


def test_info_io_lock_helpers_handle_invalid_stale_and_failed_cleanup(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    lock_path = task_dir / info_io._LOCK_FILENAME

    assert info_io._read_lock_owner(str(lock_path)) == (None, "", None)
    lock_path.write_text("not-a-pid extra", encoding="utf-8")
    assert info_io._read_lock_owner(str(lock_path)) == (None, "", None)

    lock_path.write_text("999999 extra", encoding="utf-8")
    monkeypatch.setattr(info_io, "is_pid_running", lambda pid: False)
    assert info_io._lock_file_is_stale(str(lock_path), min_age_sec=999999) is True
    assert info_io._remove_stale_lock_file(str(lock_path)) is True

    lock_path.write_text("999999", encoding="utf-8")
    monkeypatch.setattr(info_io.os, "remove", lambda path: (_ for _ in ()).throw(OSError("locked")))
    assert info_io._remove_stale_lock_file(str(lock_path)) is False

    monkeypatch.setattr(info_io.os, "remove", lambda path: (_ for _ in ()).throw(FileNotFoundError(path)))
    assert info_io._remove_stale_lock_file(str(lock_path)) is True


def test_stale_lock_cleanup_does_not_remove_replaced_live_lock(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    lock_path = tmp_path / info_io._LOCK_FILENAME
    stale_owner = "999999"
    live_owner = str(os.getpid())
    lock_path.write_text(stale_owner, encoding="utf-8")
    monkeypatch.setattr(info_io, "is_pid_running", lambda pid: pid == os.getpid())
    real_replace = os.replace
    replaced = False

    def replace_after_race(src, dst):
        nonlocal replaced
        if Path(src) == lock_path and not replaced:
            replaced = True
            lock_path.write_text(live_owner, encoding="utf-8")
        return real_replace(src, dst)

    monkeypatch.setattr(info_io.os, "replace", replace_after_race)

    assert info_io._remove_stale_lock_file(str(lock_path)) is False
    assert lock_path.read_text(encoding="utf-8") == live_owner


def test_task_info_lock_detects_reused_live_pid(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    lock_path = tmp_path / info_io._LOCK_FILENAME
    acquired_at = 1000.0
    lock_path.write_text(
        f"4242 1 {info_io._LOCK_OWNER_HOST} {acquired_at}",
        encoding="utf-8",
    )
    monkeypatch.setattr(info_io, "is_pid_running", lambda _pid: True)
    monkeypatch.setattr(info_io, "get_process_create_time", lambda _pid: acquired_at + 10)

    assert info_io._lock_file_is_stale(str(lock_path)) is True


def test_task_info_lock_keeps_valid_foreign_owner_even_when_old(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    lock_path = tmp_path / info_io._LOCK_FILENAME
    foreign_host = f"{info_io._LOCK_OWNER_HOST}-foreign"
    lock_path.write_text(f"4242 1 {foreign_host} 1", encoding="utf-8")
    monkeypatch.setattr(info_io.time, "time", lambda: 1_000_000.0)

    assert info_io._lock_file_is_stale(str(lock_path), min_age_sec=0) is False


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

    broken_payload = "{not json"
    info_path.write_text(broken_payload, encoding="utf-8")
    assert load_task_info(str(task_dir)) == {}
    with pytest.raises(json.JSONDecodeError):
        load_task_info(str(task_dir), raise_error=True)

    with pytest.raises(json.JSONDecodeError):
        update_task_info(str(task_dir), lambda info: info.update(name="x"))
    assert info_path.read_text(encoding="utf-8") == broken_payload

    missing_dir = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        update_task_info(str(missing_dir), lambda info: None)
    assert not missing_dir.exists()


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

    with pytest.raises(ValueError, match="Could not parse settings file"):
        settings.load_settings(str(root))
    with pytest.raises(ValueError, match="Could not parse settings file"):
        settings.reload_settings(str(root))

    monkeypatch.setattr(settings, "_cached", {})
    monkeypatch.setattr(settings, "load_settings", lambda root_dir=settings.ROOT_DIR: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        settings.get("ui_port")

    assert settings._yaml_scalar_to_text(True) == "true"
    assert settings._yaml_scalar_to_text(False) == "false"
    assert settings._yaml_scalar_to_text(None) == "null"
    assert settings._yaml_scalar_to_text([]) == "[]"
    assert settings._yaml_scalar_to_text({}) == "{}"
    assert "\n- a" in settings._yaml_scalar_to_text(["a"])
    assert "\na: 1" in settings._yaml_scalar_to_text({"a": 1})
    injected = settings._yaml_scalar_to_text("safe\nunknown_key: injected")
    assert yaml.safe_load(f"value: {injected}\n") == {"value": "safe\nunknown_key: injected"}


def test_load_settings_rejects_empty_non_mapping_and_unreadable_files(tmp_path, monkeypatch):
    path = tmp_path / SETTINGS_FILENAME

    path.write_text("", encoding="utf-8")
    with pytest.raises(ValueError, match="Settings file is empty"):
        settings.load_settings(str(tmp_path))

    path.write_text("- not\n- a mapping\n", encoding="utf-8")
    with pytest.raises(ValueError, match="root must be a mapping"):
        settings.load_settings(str(tmp_path))

    path.write_text("ui_port: 8099\n", encoding="utf-8")
    real_open = builtins.open

    def deny_settings_read(candidate, *args, **kwargs):
        if os.path.abspath(os.fspath(candidate)) == os.path.abspath(path):
            raise PermissionError("access denied")
        return real_open(candidate, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", deny_settings_read)
    with pytest.raises(ValueError, match="Could not read settings file"):
        settings.load_settings(str(tmp_path))


def test_settings_reject_simulated_reparse_file_before_read_or_write(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    root = tmp_path / DEFAULT_ROOT_NAME
    root.mkdir()
    path = root / SETTINGS_FILENAME
    path.write_text("ui_port: 8099\n", encoding="utf-8")
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(candidate):
        if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(str(path)):
            return True
        return real_check(candidate)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    for operation in (
        lambda: settings.ensure_settings_file(str(root)),
        lambda: settings.load_settings(str(root)),
        lambda: settings.save_setting_for_root(str(root), "ui_port", 8123),
        lambda: settings.unset_setting_for_root(str(root), "ui_port"),
    ):
        with pytest.raises(ValueError, match="Settings file must not be"):
            operation()
    assert path.read_text(encoding="utf-8") == "ui_port: 8099\n"


def test_settings_reject_simulated_reparse_managed_root(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    root = tmp_path / DEFAULT_ROOT_NAME
    root.mkdir()
    (root / SETTINGS_FILENAME).write_text("ui_port: 8099\n", encoding="utf-8")
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(candidate):
        if os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(str(root)):
            return True
        return real_check(candidate)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        settings.load_settings(str(root))


def test_save_setting_for_root_preserves_or_appends_structured_values(tmp_path, monkeypatch):
    root = tmp_path / "_pyruns_" / "script"
    root.mkdir(parents=True)
    path = Path(settings._settings_path(str(root)))

    path.write_text("global_env:\n  OLD: '1'\n", encoding="utf-8")
    settings.save_setting_for_root(str(root), "global_env", {"NEW": "2"})
    assert yaml.safe_load(path.read_text(encoding="utf-8"))["global_env"] == {"NEW": "2"}

    path.write_text("[]\n", encoding="utf-8")
    settings.save_setting_for_root(str(root), "gpu_scheduler_device_ids", [0])
    assert "gpu_scheduler_device_ids:" in path.read_text(encoding="utf-8")

    path.write_text("gpu_scheduler_device_ids: []\n", encoding="utf-8")
    monkeypatch.setattr(
        settings,
        "load_config_text",
        lambda text: (_ for _ in ()).throw(yaml.YAMLError("bad yaml")),
    )
    with pytest.raises(ValueError, match="Could not parse settings file.*bad yaml"):
        settings.save_setting_for_root(str(root), "gpu_scheduler_device_ids", [1])
    assert path.read_text(encoding="utf-8") == "gpu_scheduler_device_ids: []\n"
    assert not Path(f"{path}.lock").exists()


def test_save_and_unset_empty_structured_setting_remove_unindented_yaml_items(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    original = "gpu_scheduler_device_ids:\n- 0\n- 1\nui_port: 8099\n"

    path.write_text(original, encoding="utf-8")
    settings.save_setting_for_root(str(root), "gpu_scheduler_device_ids", [])
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["gpu_scheduler_device_ids"] == []
    assert saved["ui_port"] == 8099

    path.write_text(original, encoding="utf-8")
    settings.unset_setting_for_root(str(root), "gpu_scheduler_device_ids")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "gpu_scheduler_device_ids" not in saved
    assert saved["ui_port"] == 8099


def test_save_settings_for_root_commits_batch_with_one_file_replace(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    path.write_text("ui_port: 8099\nconda_env: ''\n", encoding="utf-8")
    real_replace = settings.os.replace
    settings_replaces = []

    def track_replace(source, destination):
        if Path(destination) == path:
            settings_replaces.append((source, destination))
        return real_replace(source, destination)

    monkeypatch.setattr(settings.os, "replace", track_replace)
    settings.save_settings_for_root(
        str(root),
        {"ui_port": 8123, "conda_env": "training", "log_enabled": True},
    )

    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["ui_port"] == 8123
    assert saved["conda_env"] == "training"
    assert saved["log_enabled"] is True
    assert len(settings_replaces) == 1


def test_save_settings_for_root_validates_complete_batch_before_writing(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    original = "ui_port: 8099\n"
    path.write_text(original, encoding="utf-8")

    with pytest.raises(KeyError, match="Unknown setting"):
        settings.save_settings_for_root(
            str(root),
            {"ui_port": 8123, "removed_setting": True},
        )

    assert path.read_text(encoding="utf-8") == original
    assert not Path(f"{path}.lock").exists()


def test_settings_writes_remove_unknown_keys_and_unset_removes_override(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    path.write_text(
        "ui_port: 8099\nmanager_max_workers: 8\nmonitor_scrollback: 12345\n",
        encoding="utf-8",
    )

    settings.save_setting_for_root(str(root), "ui_port", 8123)
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert saved["ui_port"] == 8123
    assert "manager_max_workers" not in saved

    settings.unset_setting_for_root(str(root), "monitor_scrollback")
    saved = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert "monitor_scrollback" not in saved
    assert settings.reload_settings(str(root))["monitor_scrollback"] == settings.SETTINGS_DEFAULTS["monitor_scrollback"]


def test_unset_setting_for_root_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    original = "ui_port: 8099\n"
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(
        settings.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        settings.unset_setting_for_root(str(root), "ui_port")

    assert path.read_text(encoding="utf-8") == original
    assert not Path(f"{path}.lock").exists()


def test_save_setting_for_root_creates_file_and_reports_write_errors(tmp_path, monkeypatch):
    root = tmp_path / "new-root"
    settings.save_setting_for_root(str(root), "ui_port", 8123)
    assert "ui_port: 8123" in (root / SETTINGS_FILENAME).read_text(encoding="utf-8")

    broken_root = tmp_path / "broken-root"
    monkeypatch.setattr(settings.os, "makedirs", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("readonly")))
    with pytest.raises(OSError, match="readonly"):
        settings.save_setting_for_root(str(broken_root), "ui_port", 9999)
    assert not (broken_root / SETTINGS_FILENAME).exists()


def test_save_setting_for_root_is_atomic_when_replace_fails(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    path.write_text("ui_port: 8099\n", encoding="utf-8")
    monkeypatch.setattr(
        settings.os,
        "replace",
        lambda *_args: (_ for _ in ()).throw(OSError("replace failed")),
    )

    with pytest.raises(OSError, match="replace failed"):
        settings.save_setting_for_root(str(root), "ui_port", 9000)

    assert path.read_text(encoding="utf-8") == "ui_port: 8099\n"
    assert not Path(f"{path}.lock").exists()


def test_settings_lock_recovers_dead_owner_without_touching_live_owner(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    path = root / SETTINGS_FILENAME
    path.write_text("ui_port: 8099\n", encoding="utf-8")
    lock_path = Path(f"{path}.lock")

    dead_owner = json.loads(settings._settings_lock_owner_bytes().decode("utf-8"))
    dead_owner["pid"] = 999_999_999
    dead_owner["process_create_time"] = 1.0
    lock_path.write_text(json.dumps(dead_owner), encoding="utf-8")
    monkeypatch.setattr(settings, "is_pid_running", lambda pid: pid != dead_owner["pid"])

    settings.save_setting_for_root(str(root), "ui_port", 8123)

    assert yaml.safe_load(path.read_text(encoding="utf-8"))["ui_port"] == 8123
    assert not lock_path.exists()

    live_owner = settings._settings_lock_owner_bytes()
    lock_path.write_bytes(live_owner)
    with pytest.raises(TimeoutError, match="locked by another process"):
        settings._open_settings_lock(str(path), timeout_sec=0)
    assert lock_path.read_bytes() == live_owner


def test_settings_stale_lock_cleanup_restores_racing_live_lock(tmp_path, monkeypatch):
    path = tmp_path / SETTINGS_FILENAME
    lock_path = Path(f"{path}.lock")
    stale_owner = json.loads(settings._settings_lock_owner_bytes().decode("utf-8"))
    stale_owner["pid"] = 999_999_999
    stale_owner["process_create_time"] = 1.0
    lock_path.write_text(json.dumps(stale_owner), encoding="utf-8")
    live_owner = settings._settings_lock_owner_bytes()

    real_replace = settings.os.replace
    replaced = False

    def replace_after_live_owner_arrives(src, dst):
        nonlocal replaced
        if Path(src) == lock_path and not replaced:
            replaced = True
            lock_path.unlink()
            lock_path.write_bytes(live_owner)
        return real_replace(src, dst)

    monkeypatch.setattr(settings, "is_pid_running", lambda pid: pid != stale_owner["pid"])
    monkeypatch.setattr(settings.os, "replace", replace_after_live_owner_arrives)

    assert settings._remove_stale_settings_lock(str(lock_path)) is False
    assert lock_path.read_bytes() == live_owner


def test_settings_lock_only_recovers_invalid_owner_after_safe_age(tmp_path, monkeypatch):
    path = tmp_path / SETTINGS_FILENAME
    lock_path = Path(f"{path}.lock")
    lock_path.write_bytes(b"")
    snapshot = settings._settings_lock_snapshot(str(lock_path))
    assert snapshot is not None
    modified_at = snapshot[0][2] / 1_000_000_000

    monkeypatch.setattr(settings.time, "time", lambda: modified_at + 1)
    assert settings._settings_lock_is_stale(snapshot, min_age_sec=30) is False
    assert settings._remove_stale_settings_lock(str(lock_path)) is False
    assert lock_path.exists()

    monkeypatch.setattr(settings.time, "time", lambda: modified_at + 31)
    assert settings._settings_lock_is_stale(snapshot, min_age_sec=30) is True
    assert settings._remove_stale_settings_lock(str(lock_path)) is True
    assert not lock_path.exists()


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

    monkeypatch.setattr(shell_runtime.os, "name", "posix")
    monkeypatch.setattr(shell_runtime.subprocess, "run", lambda *args, **kwargs: Result(0))
    shell_runtime._probe_shell_executable.cache_clear()
    assert shell_runtime._probe_shell_executable(str(executable), "sh") is True


def test_shell_runtime_windows_posix_script_arg_distinguishes_wsl_and_git_bash():
    import pyruns.utils.shell_runtime as shell_runtime

    script_path = r"C:\Users\me\project with spaces\run.sh"

    assert shell_runtime._windows_posix_script_arg(
        r"C:\Windows\System32\bash.exe",
        script_path,
    ) == "/mnt/c/Users/me/project with spaces/run.sh"
    assert shell_runtime._windows_posix_script_arg(
        r"C:\Program Files\Git\bin\bash.exe",
        script_path,
    ) == "C:/Users/me/project with spaces/run.sh"


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


def test_shell_runtime_probe_cleanup_and_process_tree_edge_branches(tmp_path, monkeypatch):
    import pyruns.utils.shell_runtime as shell_runtime

    executable = tmp_path / "bash"
    executable.write_text("", encoding="utf-8")
    probe_path = tmp_path / "probe.sh"
    removed = []

    class Result:
        returncode = 0

    def fake_mkstemp(**_kwargs):
        fd = os.open(probe_path, os.O_CREAT | os.O_RDWR)
        return fd, str(probe_path)

    monkeypatch.setattr(shell_runtime.tempfile, "mkstemp", fake_mkstemp)
    monkeypatch.setattr(shell_runtime.subprocess, "run", lambda *args, **kwargs: Result())
    monkeypatch.setattr(shell_runtime.os, "remove", lambda path: removed.append(path) or (_ for _ in ()).throw(OSError("locked")))

    assert shell_runtime._probe_windows_posix_script_execution(str(executable)) is True
    assert removed == [str(probe_path)]

    class BadNameProcess:
        def parents(self):
            return []

        def name(self):
            raise RuntimeError("name unavailable")

    class NoShellProcess:
        def parents(self):
            return []

        def name(self):
            return "python.exe"

    class ExeFailProcess:
        def parents(self):
            return []

        def name(self):
            return "bash.exe"

        def exe(self):
            raise RuntimeError("exe unavailable")

    class Psutil:
        def __init__(self, proc):
            self.proc = proc

        def Process(self, _pid):
            return self.proc

    monkeypatch.setattr(shell_runtime, "psutil", Psutil(BadNameProcess()))
    assert shell_runtime._find_shell_in_process_tree() is None

    monkeypatch.setattr(shell_runtime, "psutil", Psutil(NoShellProcess()))
    assert shell_runtime._find_shell_in_process_tree() is None

    monkeypatch.setattr(shell_runtime, "psutil", Psutil(ExeFailProcess()))
    monkeypatch.setattr(shell_runtime, "_resolve_candidate_path", lambda value: str(executable) if value == "bash.exe" else "")
    runtime = shell_runtime._find_shell_in_process_tree()
    assert runtime is not None
    assert runtime["terminal_kind"] == "bash"
    assert runtime["executable"] == str(executable)

    assert shell_runtime._shell_settings_root_for_task(None) is None


def test_shell_runtime_windows_and_unknown_fallbacks(monkeypatch):
    import pyruns.utils.shell_runtime as shell_runtime

    monkeypatch.setattr(shell_runtime.os, "name", "nt")
    monkeypatch.setenv("COMSPEC", "weird-shell.exe")
    monkeypatch.setattr(shell_runtime, "_resolve_candidate_path", lambda value: "")
    runtime = shell_runtime._fallback_follow_shell()
    assert runtime["terminal_kind"] == "cmd"
    assert runtime["display_name"] == "Command Prompt"
    assert runtime["executable"] == "weird-shell.exe"

    monkeypatch.setattr(shell_runtime.os, "name", "posix")
    monkeypatch.setenv("SHELL", "/opt/custom-shell")
    monkeypatch.setattr(shell_runtime, "_resolve_candidate_path", lambda value: "/opt/custom-shell")
    runtime = shell_runtime._fallback_follow_shell()
    assert runtime["terminal_kind"] == "sh"
    assert runtime["display_name"] == "Shell"
