"""
Shared fixtures for pyruns tests.
"""
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

import pytest


_DEFAULT_TMP_ROOT = Path(tempfile.gettempdir()) / "pyruns-tests"
_LOCAL_TMP_ROOT = Path(os.environ.get("PYRUNS_TEST_TMP_ROOT", _DEFAULT_TMP_ROOT))


def _pyruns_project_ancestor(path: Path) -> Path | None:
    """Return the nearest ancestor whose project data could affect discovery."""

    resolved = path.resolve()
    for directory in (resolved, *resolved.parents):
        if directory.name == "_pyruns_":
            return directory.parent
        if (directory / "_pyruns_").is_dir():
            return directory
    return None


def _isolated_tmp_root(path: Path) -> Path:
    """Move the default test root outside every existing Pyruns project."""

    candidate = path.resolve()
    while (project := _pyruns_project_ancestor(candidate)) is not None:
        if "PYRUNS_TEST_TMP_ROOT" in os.environ:
            raise RuntimeError(
                "PYRUNS_TEST_TMP_ROOT must not be inside an existing Pyruns project: "
                f"{project}"
            )
        parent = project.parent
        if parent == project:
            raise RuntimeError(
                "cannot create an isolated Pyruns test directory on this filesystem"
            )
        candidate = parent / _DEFAULT_TMP_ROOT.name
    return candidate


@pytest.fixture(autouse=True)
def _prevent_windows_test_console_windows(monkeypatch):
    """Keep every subprocess spawned by tests invisible on Windows."""

    if os.name != "nt":
        yield
        return

    original_init = subprocess.Popen.__init__

    def hidden_init(self, *args, **kwargs):
        creationflags = int(kwargs.get("creationflags", 0))
        creationflags &= ~subprocess.CREATE_NEW_CONSOLE
        kwargs["creationflags"] = creationflags | subprocess.CREATE_NO_WINDOW
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(subprocess.Popen, "__init__", hidden_init)
    yield


@pytest.fixture()
def tmp_path():
    """Workspace-local replacement for pytest's default tmp_path fixture."""
    shared_root = _isolated_tmp_root(_LOCAL_TMP_ROOT)
    root = shared_root
    path = root / uuid.uuid4().hex
    remove_tree = shutil.rmtree
    try:
        root.mkdir(parents=True, exist_ok=True)
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(
            "cannot create an isolated test directory; set PYRUNS_TEST_TMP_ROOT "
            "to a writable path outside every Pyruns project"
        ) from exc
    try:
        yield path
    finally:
        remove_tree(path, ignore_errors=True)


@pytest.fixture()
def isolated_tmp_root_resolver():
    """Expose the test-root invariant for its focused regression test."""

    return _isolated_tmp_root


@pytest.fixture()
def sample_config():
    """A minimal config dict with no pipe syntax."""
    return {
        "lr": 0.001,
        "batch_size": 32,
        "optimizer": "adam",
        "model": {
            "name": "resnet",
            "layers": 50,
        },
    }


@pytest.fixture()
def sample_config_with_pipes():
    """Config with product pipe syntax."""
    return {
        "lr": "0.001 | 0.01 | 0.1",
        "batch_size": "32 | 64",
        "optimizer": "adam",
        "model": {
            "name": "resnet",
            "layers": 50,
        },
    }


@pytest.fixture()
def sample_config_mixed():
    """Config with both product and zip pipe syntax."""
    return {
        "lr": "0.001 | 0.01 | 0.1",     # product: 3
        "batch_size": "32 | 64",          # product: 2
        "seed": "(1 | 2 | 3)",            # zip: 3
        "tag": "(a | b | c)",             # zip: 3
        "optimizer": "adam",               # fixed
    }

