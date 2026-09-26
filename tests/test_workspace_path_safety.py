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
import pyruns.utils.task_files as task_files
from pyruns._config import (
    ACTIVE_WORKSPACE_FILENAME,
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    DEFAULT_ROOT_NAME,
    SCRIPT_INFO_FILENAME,
    SETTINGS_FILENAME,
    TASK_INFO_FILENAME,
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


@pytest.mark.skipif(os.name != "nt", reason="Windows extended paths and DOS filename rules")
def test_workspace_file_boundary_distinguishes_missing_and_literal_windows_names(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "inside.txt").write_text("inside", encoding="utf-8")
    extended_root = "\\\\?\\" + str(root)
    info_io.validate_workspace_file(extended_root + "\\inside.txt", str(root), label="Payload")

    # A literal trailing dot is a different directory from the missing plain name.
    special = extended_root + "\\trailing."
    os.mkdir(special)
    payload = special + "\\config.yaml"
    try:
        with open(payload, "w", encoding="utf-8") as handle:
            handle.write("value: keep\n")
        info_io.validate_workspace_file(payload, special, label="Payload")
        with pytest.raises(ValueError, match="outside its workspace boundary"):
            info_io.validate_workspace_file(payload, str(root / "trailing"), label="Payload")
    finally:
        if os.path.exists(payload):
            os.unlink(payload)
        os.rmdir(special)


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


@pytest.mark.parametrize("target_name", [SCRIPT_INFO_FILENAME, CONFIG_DEFAULT_FILENAME])
def test_workspace_is_revalidated_after_creation(tmp_path, monkeypatch, target_name):
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
        if target_name == SCRIPT_INFO_FILENAME:
            info_io.save_script_info(str(workspace), {"script_name": "train"})
        else:
            pyruns.ensure_config_default(str(workspace))

    assert workspace.is_dir()
    assert not (workspace / target_name).exists()
    assert not list(workspace.glob(f".{target_name}.*.tmp"))


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


def _link_directory(link: Path, target: Path) -> None:
    try:
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
        else:
            link.symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory link creation unavailable: {exc}")


def _unlink_directory(link: Path) -> None:
    if os.name == "nt":
        link.rmdir()
    else:
        link.unlink()


@pytest.mark.parametrize("level", ["managed_root", "workspace", "tasks", "task"])
def test_task_metadata_rechecks_ancestors_after_directory_replacement(tmp_path, level):
    managed_root = tmp_path / DEFAULT_ROOT_NAME
    workspace = managed_root / "train"
    task_dir = workspace / "tasks" / "safe"
    task_dir.mkdir(parents=True)
    (task_dir / TASK_INFO_FILENAME).write_text('{"name": "safe"}', encoding="utf-8")
    original = info_io.load_task_metadata(str(task_dir), raise_error=True)
    assert original["name"] == "safe"
    target = {
        "managed_root": managed_root, "workspace": workspace,
        "tasks": task_dir.parent, "task": task_dir,
    }[level]
    displaced = tmp_path / "displaced"
    target.rename(displaced)
    _link_directory(target, displaced)
    try:
        with pytest.raises(ValueError, match="symlink, junction, or reparse point|resolves outside"):
            info_io.load_task_metadata(str(task_dir), raise_error=True)
        with pytest.raises(ValueError, match="symlink, junction, or reparse point|resolves outside"):
            info_io.update_task_metadata(str(task_dir), lambda info: info.update(name="changed"))
    finally:
        _unlink_directory(target)
        displaced.rename(target)
    assert info_io.load_task_metadata(str(task_dir), raise_error=True) == original


def test_managed_path_rechecks_a_previously_missing_directory(tmp_path):
    managed_root = tmp_path / DEFAULT_ROOT_NAME
    workspace = managed_root / "train"
    info_io.validate_workspace_directory(str(workspace))
    outside = tmp_path / "outside"
    (outside / "train").mkdir(parents=True)
    _link_directory(managed_root, outside)
    try:
        with pytest.raises(ValueError, match="Managed workspace path must not contain"):
            info_io.validate_workspace_directory(str(workspace))
    finally:
        _unlink_directory(managed_root)
    workspace.mkdir(parents=True)
    info_io.validate_workspace_directory(str(workspace))


def test_relative_managed_path_tracks_working_directory_changes(tmp_path, monkeypatch):
    first = tmp_path / "first"
    second = tmp_path / "second"
    relative = Path(DEFAULT_ROOT_NAME) / "train" / "tasks" / "safe"
    (first / relative).mkdir(parents=True)
    (first / relative / TASK_INFO_FILENAME).write_text('{"name": "safe"}', encoding="utf-8")
    second.mkdir()
    _link_directory(second / DEFAULT_ROOT_NAME, first / DEFAULT_ROOT_NAME)
    try:
        monkeypatch.chdir(first)
        expected = info_io.load_task_metadata(str(relative), raise_error=True)
        monkeypatch.chdir(second)
        with pytest.raises(ValueError, match="Managed workspace path must not contain"):
            info_io.load_task_metadata(str(relative), raise_error=True)
        monkeypatch.chdir(first)
        assert info_io.load_task_metadata(str(relative), raise_error=True) == expected
    finally:
        monkeypatch.chdir(tmp_path)
        _unlink_directory(second / DEFAULT_ROOT_NAME)


@pytest.mark.parametrize("operation", ["read", "write"])
def test_task_payload_keeps_project_boundary_when_parent_changes(tmp_path, monkeypatch, operation):
    relative = Path(DEFAULT_ROOT_NAME) / "train" / "tasks" / "sample"
    first, second = tmp_path / "first", tmp_path / "second"
    for project, value in ((first, "first"), (second, "second")):
        (project / relative).mkdir(parents=True)
        (project / relative / CONFIG_FILENAME).write_text(f"value: {value}\n", encoding="utf-8")
    alias = tmp_path / "project"
    _link_directory(alias, first)
    validate = task_files.validate_workspace_file

    def switch_project_after_directory_check(path, workspace_dir, **kwargs):
        _unlink_directory(alias)
        _link_directory(alias, second)
        return validate(path, workspace_dir, **kwargs)

    monkeypatch.setattr(task_files, "validate_workspace_file", switch_project_after_directory_check)
    try:
        if operation == "read":
            kind, config, text, error = read_task_payload(str(alias / relative), {})
            assert (kind, config, text) == (TASK_KIND_CONFIG, {}, "")
            assert "resolves outside" in error
        else:
            with pytest.raises(ValueError, match="resolves outside"):
                write_task_payload(
                    str(alias / relative), task_kind=TASK_KIND_CONFIG,
                    config_file=CONFIG_FILENAME, config={"value": "changed"},
                )
    finally:
        if os.path.lexists(alias):
            _unlink_directory(alias)
    assert (first / relative / CONFIG_FILENAME).read_text(encoding="utf-8") == "value: first\n"
    assert (second / relative / CONFIG_FILENAME).read_text(encoding="utf-8") == "value: second\n"


@pytest.mark.parametrize("relative_path", [False, True])
def test_task_payload_resolves_current_project_on_each_call(tmp_path, monkeypatch, relative_path):
    relative = Path(DEFAULT_ROOT_NAME) / "train" / "tasks" / "中文样本"
    first, second = tmp_path / "first", tmp_path / "second"
    for project, value in ((first, 1), (second, 2)):
        (project / relative).mkdir(parents=True)
        (project / relative / CONFIG_FILENAME).write_text(f"value: {value}\n", encoding="utf-8")
    alias = tmp_path / "project"
    _link_directory(alias, first)
    monkeypatch.chdir(tmp_path)
    task_dir = (Path("project") if relative_path else alias) / relative
    try:
        assert read_task_payload(str(task_dir), {})[1]["value"] == 1
        _unlink_directory(alias)
        _link_directory(alias, second)
        kind, config, text, error = read_task_payload(str(task_dir), {})
        assert (kind, config, text, error) == (TASK_KIND_CONFIG, {"value": 2}, "", "")
    finally:
        if os.path.lexists(alias):
            _unlink_directory(alias)
