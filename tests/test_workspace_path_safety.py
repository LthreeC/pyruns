"""Managed-workspace path safety regressions that do not require symlink privileges."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import pyruns
import pyruns.launcher as launcher
import pyruns.utils.info_io as info_io
import pyruns.utils.parse_utils as parse_utils
import pyruns.utils.settings as settings
from pyruns._config import (
    ACTIVE_WORKSPACE_FILENAME,
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_ROOT_NAME,
    SCRIPT_INFO_FILENAME,
    SETTINGS_FILENAME,
    TASK_KIND_CONFIG,
)
from pyruns.utils.task_files import read_task_payload, write_task_payload


def _simulate_reparse(monkeypatch, *paths: Path) -> None:
    targets = {os.path.normcase(os.path.abspath(path)) for path in paths}
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(candidate):
        if os.path.normcase(os.path.abspath(candidate)) in targets:
            return True
        return real_check(candidate)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)


def test_script_info_rejects_simulated_reparse_workspace_before_io(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    workspace.mkdir(parents=True)
    script_info = workspace / SCRIPT_INFO_FILENAME
    original = '{"script_name": "keep"}\n'
    script_info.write_text(original, encoding="utf-8")
    _simulate_reparse(monkeypatch, workspace)

    assert info_io.load_script_info(str(workspace)) == {}
    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        info_io.save_script_info(str(workspace), {"script_name": "replace"})

    assert script_info.read_text(encoding="utf-8") == original
    assert not list(workspace.glob(f".{SCRIPT_INFO_FILENAME}.*.tmp"))


def test_script_info_revalidates_workspace_after_creation(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    real_validate = info_io.validate_workspace_directory
    calls = 0

    def simulate_reparse_after_create(path):
        nonlocal calls
        calls += 1
        real_validate(path)
        if calls == 2:
            raise ValueError("simulated reparse after create")

    monkeypatch.setattr(
        info_io,
        "validate_workspace_directory",
        simulate_reparse_after_create,
    )

    with pytest.raises(ValueError, match="simulated reparse after create"):
        info_io.save_script_info(str(workspace), {"script_name": "train"})

    assert workspace.is_dir()
    assert not (workspace / SCRIPT_INFO_FILENAME).exists()
    assert not list(workspace.glob(f".{SCRIPT_INFO_FILENAME}.*.tmp"))


def test_bootstrap_rejects_simulated_reparse_workspace_before_initialization(
    tmp_path,
    monkeypatch,
):
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    workspace = Path(launcher.workspace_root_for_script(str(script)))
    workspace.mkdir(parents=True)
    _simulate_reparse(monkeypatch, workspace)

    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        launcher.bootstrap_workspace(str(script))

    assert list(workspace.iterdir()) == []


def test_active_marker_rejects_simulated_reparse_file_before_write(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    workspace.mkdir(parents=True)
    marker = workspace.parent / ACTIVE_WORKSPACE_FILENAME
    marker.write_text("keep", encoding="utf-8")
    _simulate_reparse(monkeypatch, marker)

    with pytest.raises(ValueError, match="must not be a symlink"):
        launcher.mark_workspace_active(str(workspace))

    assert marker.read_text(encoding="utf-8") == "keep"
    assert not list(marker.parent.glob(f".{ACTIVE_WORKSPACE_FILENAME}.*.tmp"))


def test_argparse_config_rejects_simulated_reparse_workspace_before_write(
    tmp_path,
    monkeypatch,
):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    workspace.mkdir(parents=True)
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    _simulate_reparse(monkeypatch, workspace)

    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        parse_utils.generate_config_file(str(workspace), str(script), {})

    assert not (workspace / CONFIG_DEFAULT_FILENAME).exists()
    assert not list(workspace.glob(f".{CONFIG_DEFAULT_FILENAME}.*.tmp"))


def test_task_payload_rejects_simulated_reparse_file_before_read_or_write(
    tmp_path,
    monkeypatch,
):
    task_dir = tmp_path / DEFAULT_ROOT_NAME / "train" / "tasks" / "safe"
    task_dir.mkdir(parents=True)
    payload = task_dir / CONFIG_FILENAME
    original = "value: keep\n"
    payload.write_text(original, encoding="utf-8")
    _simulate_reparse(monkeypatch, payload)

    kind, config, text, error = read_task_payload(
        str(task_dir),
        {"task_kind": TASK_KIND_CONFIG, "config_file": CONFIG_FILENAME},
    )
    assert (kind, config, text) == (TASK_KIND_CONFIG, {}, "")
    assert "Task payload must not be" in error

    with pytest.raises(ValueError, match="Task payload must not be"):
        write_task_payload(
            str(task_dir),
            task_kind=TASK_KIND_CONFIG,
            config_file=CONFIG_FILENAME,
            config={"value": "replace"},
        )
    assert payload.read_text(encoding="utf-8") == original


def test_settings_rejects_simulated_reparse_lock_before_write(tmp_path, monkeypatch):
    managed_root = tmp_path / DEFAULT_ROOT_NAME
    managed_root.mkdir()
    settings_path = managed_root / SETTINGS_FILENAME
    original = "ui_port: 8099\n"
    settings_path.write_text(original, encoding="utf-8")
    lock_path = Path(f"{settings_path}.lock")
    lock_path.write_text("keep", encoding="utf-8")
    _simulate_reparse(monkeypatch, lock_path)

    with pytest.raises(ValueError, match="Settings lock file must not be"):
        settings.save_setting_for_root(str(managed_root), "ui_port", 8123)

    assert settings_path.read_text(encoding="utf-8") == original
    assert lock_path.read_text(encoding="utf-8") == "keep"
    assert not list(managed_root.glob(f".{SETTINGS_FILENAME}.*.tmp"))


def test_ensure_config_default_revalidates_workspace_after_creation(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    real_validate = info_io.validate_workspace_directory
    calls = 0

    def simulate_reparse_after_create(path):
        nonlocal calls
        calls += 1
        real_validate(path)
        if calls == 2:
            raise ValueError("simulated reparse after create")

    monkeypatch.setattr(
        info_io,
        "validate_workspace_directory",
        simulate_reparse_after_create,
    )

    with pytest.raises(ValueError, match="simulated reparse after create"):
        pyruns.ensure_config_default(str(workspace))

    assert workspace.is_dir()
    assert not (workspace / CONFIG_DEFAULT_FILENAME).exists()


def test_artifact_dir_rejects_simulated_reparse_directory_before_write(
    tmp_path,
    monkeypatch,
):
    task_dir = tmp_path / DEFAULT_ROOT_NAME / "train" / "tasks" / "safe"
    task_dir.mkdir(parents=True)
    config_path = task_dir / CONFIG_FILENAME
    config_path.write_text("value: 1\n", encoding="utf-8")
    artifacts_root = task_dir / "artifacts"
    artifacts_root.mkdir()
    _simulate_reparse(monkeypatch, artifacts_root)
    monkeypatch.setenv("__PYRUNS_CONFIG__", str(config_path))
    monkeypatch.setenv("PYRUNS_RUN_INDEX", "1")

    with pytest.raises(ValueError, match="Managed workspace path must not contain"):
        pyruns.get_artifact_dir()

    assert list(artifacts_root.iterdir()) == []
