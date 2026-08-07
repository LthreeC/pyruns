"""
Shared fixtures for pyruns tests.
"""
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import pytest


_LOCAL_TMP_ROOT = Path(os.environ.get("PYRUNS_TEST_TMP_ROOT", Path(tempfile.gettempdir()) / "pyruns-tests"))


@pytest.fixture()
def tmp_path():
    """Workspace-local replacement for pytest's default tmp_path fixture."""
    root = _LOCAL_TMP_ROOT
    path = root / uuid.uuid4().hex
    try:
        root.mkdir(parents=True, exist_ok=True)
        path.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        root = Path(tempfile.mkdtemp(prefix="pyruns-tests-"))
        path = root / uuid.uuid4().hex
        path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        if root != _LOCAL_TMP_ROOT:
            shutil.rmtree(root, ignore_errors=True)


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

