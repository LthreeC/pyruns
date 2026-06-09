"""
Tests for pyruns.core — config_manager, system_metrics, executor,
task_generator, and report.
"""
import csv
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import Future
from pathlib import Path

import pytest
import psutil
import yaml
from unittest.mock import patch, MagicMock

import pyruns.core.executor as executor
import pyruns.core.task_manager as task_manager_module
from pyruns._config import (
    ENV_KEY_CONFIG,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
    ENV_KEY_CONDA_ENV,
    ENV_KEY_CONDA_EXE,
    ENV_KEY_PYTHON_EXECUTABLE,
    CONFIG_DEFAULT_FILENAME,
    CONFIG_FILENAME,
    ERROR_LOG_FILENAME,
    POWERSHELL_CONFIG_FILENAME,
    DEFAULT_ROOT_NAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    TASK_INFO_FILENAME,
    TRASH_DIR,
    RECORDS_KEY,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
    WORKSPACE_KIND_SHELL,
)
from pyruns.core.config_manager import ConfigNode, ConfigManager
from pyruns.core.executor import (
    _append_run_log_text,
    _build_command,
    _gpu_assignment_log,
    _gpu_failure_detail_lines,
    _prepare_env,
    _read_log_tail_text,
    _resolve_python_runtime,
    run_task_worker,
)
from pyruns.core.gpu_scheduler import GpuAssignment, GpuDecision, GpuDevice, GpuResourceScheduler, GpuSchedulerConfig
from pyruns.core.report import build_export_csv, build_export_json
from pyruns.core.system_metrics import SystemMonitor
from pyruns.core.task_generator import TaskGenerator, create_task_object
from pyruns.core.task_manager import TaskManager
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    list_script_candidates,
    shell_workspace_root_for_run_root,
    workspace_root_for_script,
)
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.info_io import save_task_info, update_task_info
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.shell_runtime import get_shell_config_filename_for_workspace, get_shell_runtime_for_workspace


class _StaticGpuProvider:
    def __init__(self, devices):
        self.devices = devices
        self.calls = 0

    def sample(self):
        self.calls += 1
        return list(self.devices)


def test_prepare_env_allows_child_to_import_current_pyruns_from_script_workdir(tmp_path, monkeypatch):
    """Experiment scripts run from their own cwd but still need pyruns APIs."""
    monkeypatch.delenv("PYTHONPATH", raising=False)

    env = _prepare_env(task_dir=str(tmp_path), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "-c", "import pyruns; print(pyruns.__file__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()).is_file()


def test_prepare_env_isolates_current_pyruns_from_launcher_site_packages(tmp_path, monkeypatch):
    """Task envs should get current pyruns without inheriting every launcher dependency."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    launcher_shared = launcher_site_packages / "sharedpkg"
    launcher_shared.mkdir()
    (launcher_shared / "__init__.py").write_text("ORIGIN = 'launcher-env'\n", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_pyruns = task_site_packages / "pyruns"
    task_pyruns.mkdir(parents=True)
    (task_pyruns / "__init__.py").write_text("__version__ = 'old-pyruns'\n", encoding="utf-8")

    task_shared = task_site_packages / "sharedpkg"
    task_shared.mkdir()
    (task_shared / "__init__.py").write_text("ORIGIN = 'task-env'\n", encoding="utf-8")

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import pyruns, sharedpkg\n"
                "print(pyruns.__version__)\n"
                "print(sharedpkg.ORIGIN)\n"
                "print(pyruns.__file__)\n"
            ),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert lines[0] == "new-pyruns"
    assert lines[1] == "task-env"
    assert str(launcher_site_packages) not in env["PYTHONPATH"].split(os.pathsep)
    assert str(launcher_site_packages) not in lines[2]


def test_prepare_env_keeps_current_pyruns_across_nested_imports(tmp_path, monkeypatch):
    """Nested user modules should repeatedly import the launcher pyruns, not the task env copy."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    launcher_shared = launcher_site_packages / "sharedpkg"
    launcher_shared.mkdir()
    (launcher_shared / "__init__.py").write_text("ORIGIN = 'launcher-env'\n", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_pyruns = task_site_packages / "pyruns"
    task_pyruns.mkdir(parents=True)
    (task_pyruns / "__init__.py").write_text("__version__ = 'old-pyruns'\n", encoding="utf-8")

    task_shared = task_site_packages / "sharedpkg"
    task_shared.mkdir()
    (task_shared / "__init__.py").write_text("ORIGIN = 'task-env'\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    (project / "module1.py").write_text(
        "\n".join([
            "import pyruns",
            "import sharedpkg",
            "",
            "def marker():",
            "    return {'module': 'module1', 'pyruns': pyruns.__version__, 'shared': sharedpkg.ORIGIN, 'file': pyruns.__file__}",
        ]),
        encoding="utf-8",
    )
    (project / "module2.py").write_text(
        "\n".join([
            "import pyruns",
            "import sharedpkg",
            "",
            "def marker():",
            "    return {'module': 'module2', 'pyruns': pyruns.__version__, 'shared': sharedpkg.ORIGIN, 'file': pyruns.__file__}",
        ]),
        encoding="utf-8",
    )
    (project / "train.py").write_text(
        "\n".join([
            "import pyruns",
            "import sharedpkg",
            "import module1",
            "import module2",
            "",
            "def run():",
            "    return [",
            "        {'module': 'train', 'pyruns': pyruns.__version__, 'shared': sharedpkg.ORIGIN, 'file': pyruns.__file__},",
            "        module1.marker(),",
            "        module2.marker(),",
            "    ]",
        ]),
        encoding="utf-8",
    )
    (project / "run.py").write_text(
        "\n".join([
            "import json",
            "import pyruns",
            "import sharedpkg",
            "import train",
            "",
            "result = [{'module': 'run', 'pyruns': pyruns.__version__, 'shared': sharedpkg.ORIGIN, 'file': pyruns.__file__}]",
            "result.extend(train.run())",
            "print(json.dumps(result, sort_keys=True))",
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "run.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert [item["module"] for item in payload] == ["run", "train", "module1", "module2"]
    assert {item["pyruns"] for item in payload} == {"new-pyruns"}
    assert {item["shared"] for item in payload} == {"task-env"}
    assert all(str(launcher_site_packages) not in item["file"] for item in payload)
    assert str(launcher_site_packages) not in env["PYTHONPATH"].split(os.pathsep)


def test_prepare_env_preloads_current_pyruns_when_project_shadows_package(tmp_path, monkeypatch):
    """A project-local pyruns.py should not override the pyruns version that launched the server."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_pyruns = task_site_packages / "pyruns"
    task_pyruns.mkdir(parents=True)
    (task_pyruns / "__init__.py").write_text("__version__ = 'old-pyruns'\n", encoding="utf-8")

    task_shared = task_site_packages / "sharedpkg"
    task_shared.mkdir()
    (task_shared / "__init__.py").write_text("ORIGIN = 'task-env'\n", encoding="utf-8")

    user_pythonpath = tmp_path / "user-pythonpath"
    user_pythonpath.mkdir()
    (user_pythonpath / "sitecustomize.py").write_text(
        "import builtins\nbuiltins.USER_SITECUSTOMIZE_RAN = True\n",
        encoding="utf-8",
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyruns.py").write_text("__version__ = 'project-shadow'\n", encoding="utf-8")
    (project / "localdep.py").write_text("ORIGIN = 'project-local'\n", encoding="utf-8")
    (project / "run.py").write_text(
        "\n".join([
            "import builtins",
            "import json",
            "import localdep",
            "import pyruns",
            "import sharedpkg",
            "import subprocess",
            "import sys",
            "",
            "child = subprocess.run([",
            "    sys.executable,",
            "    '-c',",
            "    \"import builtins, json, pyruns, sharedpkg; print(json.dumps({'pyruns_file': pyruns.__file__, 'pyruns_version': pyruns.__version__, 'shared': sharedpkg.ORIGIN, 'sitecustomize': bool(getattr(builtins, 'USER_SITECUSTOMIZE_RAN', False))}, sort_keys=True))\",",
            "], capture_output=True, text=True, check=True)",
            "print(json.dumps({",
            "    'child': json.loads(child.stdout),",
            "    'localdep': localdep.ORIGIN,",
            "    'pyruns_file': pyruns.__file__,",
            "    'pyruns_version': pyruns.__version__,",
            "    'shared': sharedpkg.ORIGIN,",
            "    'sitecustomize': bool(getattr(builtins, 'USER_SITECUSTOMIZE_RAN', False)),",
            "}, sort_keys=True))",
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(user_pythonpath), str(task_site_packages)]))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "run.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["pyruns_version"] == "new-pyruns"
    assert payload["shared"] == "task-env"
    assert payload["localdep"] == "project-local"
    assert payload["sitecustomize"] is True
    assert str(project / "pyruns.py") not in payload["pyruns_file"]
    assert payload["child"]["pyruns_version"] == "new-pyruns"
    assert payload["child"]["shared"] == "task-env"
    assert payload["child"]["sitecustomize"] is True
    assert str(project / "pyruns.py") not in payload["child"]["pyruns_file"]


def test_prepare_env_import_guard_is_lazy_for_scripts_without_pyruns(tmp_path, monkeypatch):
    """Scripts that do not import pyruns should not be forced to import pyruns at startup."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("import missing_pyruns_dependency\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_shared = task_site_packages / "sharedpkg"
    task_shared.mkdir(parents=True)
    (task_shared / "__init__.py").write_text("ORIGIN = 'task-env'\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    (project / "run.py").write_text(
        "import sharedpkg\nprint(sharedpkg.ORIGIN)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "run.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "task-env"
    assert "missing_pyruns_dependency" not in result.stderr
    assert "sitecustomize" not in result.stderr.lower()


def test_prepare_env_preserves_current_pyruns_distribution_metadata_when_isolated(tmp_path, monkeypatch):
    """The isolated package root should keep the launcher pyruns distribution version."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text(
        "from importlib.metadata import version\n__version__ = version('pyruns')\n",
        encoding="utf-8",
    )
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")
    dist_info = launcher_site_packages / "pyruns-9.8.7.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: pyruns\nVersion: 9.8.7\n",
        encoding="utf-8",
    )

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_pyruns = task_site_packages / "pyruns"
    task_pyruns.mkdir(parents=True)
    (task_pyruns / "__init__.py").write_text("__version__ = 'old-pyruns'\n", encoding="utf-8")

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "-c", "import pyruns; print(pyruns.__version__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "9.8.7"


def test_prepare_env_import_guard_applies_to_shell_task_python_children(tmp_path, monkeypatch):
    """Shell tasks that launch Python should inherit the same pyruns import protection."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_shared = task_site_packages / "sharedpkg"
    task_shared.mkdir(parents=True)
    (task_shared / "__init__.py").write_text("ORIGIN = 'task-env'\n", encoding="utf-8")

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyruns.py").write_text("__version__ = 'project-shadow'\n", encoding="utf-8")

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_SHELL)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, pyruns, sharedpkg; print(json.dumps({'pyruns': pyruns.__version__, 'shared': sharedpkg.ORIGIN}))",
        ],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"pyruns": "new-pyruns", "shared": "task-env"}


def test_prepare_env_import_guard_handles_package_shadow_submodules_and_reload(tmp_path, monkeypatch):
    """Project-local pyruns packages should not win for submodule imports or reloads."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("MARKER = 'launcher-core'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_pyruns = task_site_packages / "pyruns"
    (task_pyruns / "core").mkdir(parents=True)
    (task_pyruns / "__init__.py").write_text("__version__ = 'old-pyruns'\n", encoding="utf-8")
    (task_pyruns / "core" / "__init__.py").write_text("MARKER = 'task-core'\n", encoding="utf-8")

    project = tmp_path / "project"
    project_shadow = project / "pyruns"
    (project_shadow / "core").mkdir(parents=True)
    (project_shadow / "__init__.py").write_text("__version__ = 'project-package-shadow'\n", encoding="utf-8")
    (project_shadow / "core" / "__init__.py").write_text("MARKER = 'project-core'\n", encoding="utf-8")
    (project / "run.py").write_text(
        "\n".join([
            "import importlib",
            "import json",
            "import sys",
            "import pyruns",
            "import pyruns.core as core",
            "",
            "first = {'version': pyruns.__version__, 'core': core.MARKER, 'file': pyruns.__file__}",
            "reloaded = importlib.reload(pyruns)",
            "second = {'version': reloaded.__version__, 'file': reloaded.__file__}",
            "for name in list(sys.modules):",
            "    if name == 'pyruns' or name.startswith('pyruns.'):",
            "        sys.modules.pop(name, None)",
            "import pyruns as imported_again",
            "import pyruns.core as core_again",
            "third = {'version': imported_again.__version__, 'core': core_again.MARKER, 'file': imported_again.__file__}",
            "print(json.dumps({'first': first, 'second': second, 'third': third}, sort_keys=True))",
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "run.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["first"]["version"] == "new-pyruns"
    assert payload["first"]["core"] == "launcher-core"
    assert payload["second"]["version"] == "new-pyruns"
    assert payload["third"]["version"] == "new-pyruns"
    assert payload["third"]["core"] == "launcher-core"
    assert "project" not in payload["first"]["file"]
    assert "project" not in payload["third"]["file"]


def test_prepare_env_import_guard_is_active_for_user_sitecustomize_imports(tmp_path, monkeypatch):
    """User sitecustomize can import pyruns early without hitting task or project shadows."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_pyruns = task_site_packages / "pyruns"
    task_pyruns.mkdir(parents=True)
    (task_pyruns / "__init__.py").write_text("__version__ = 'old-pyruns'\n", encoding="utf-8")

    user_pythonpath = tmp_path / "user-pythonpath"
    user_pythonpath.mkdir()
    (user_pythonpath / "sitecustomize.py").write_text(
        "import builtins\nimport pyruns\nbuiltins.USER_SITECUSTOMIZE_PYRUNS = pyruns.__version__\n",
        encoding="utf-8",
    )

    project = tmp_path / "project"
    project.mkdir()
    (project / "pyruns.py").write_text("__version__ = 'project-shadow'\n", encoding="utf-8")
    (project / "run.py").write_text(
        "\n".join([
            "import builtins",
            "import json",
            "import pyruns",
            "print(json.dumps({'script': pyruns.__version__, 'sitecustomize': builtins.USER_SITECUSTOMIZE_PYRUNS}))",
        ]),
        encoding="utf-8",
    )

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([str(user_pythonpath), str(task_site_packages)]))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [sys.executable, "run.py"],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"script": "new-pyruns", "sitecustomize": "new-pyruns"}


def test_prepare_env_does_not_expose_source_root_sibling_packages(tmp_path, monkeypatch):
    """Only pyruns should be exposed from the launcher source tree, not sibling packages."""

    launcher_source_root = tmp_path / "launcher-source"
    launcher_pyruns = launcher_source_root / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    launcher_shared = launcher_source_root / "sharedpkg"
    launcher_shared.mkdir()
    (launcher_shared / "__init__.py").write_text("ORIGIN = 'launcher-source'\n", encoding="utf-8")

    task_site_packages = tmp_path / "task-env" / "Lib" / "site-packages"
    task_shared = task_site_packages / "sharedpkg"
    task_shared.mkdir(parents=True)
    (task_shared / "__init__.py").write_text("ORIGIN = 'task-env'\n", encoding="utf-8")

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setenv("PYTHONPATH", str(task_site_packages))
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env = _prepare_env(task_dir=str(tmp_path / "task"), task_kind=TASK_KIND_CONFIG)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json, pyruns, sharedpkg; print(json.dumps({'pyruns': pyruns.__version__, 'shared': sharedpkg.ORIGIN}))",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"pyruns": "new-pyruns", "shared": "task-env"}
    assert str(launcher_source_root) not in env["PYTHONPATH"].split(os.pathsep)


def test_prepare_env_refreshes_isolated_pyruns_root_when_package_files_change(tmp_path, monkeypatch):
    """The isolated pyruns root should not reuse a stale copy after package files change."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    init_file = launcher_pyruns / "__init__.py"
    init_file.write_text("__version__ = 'first-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env1 = _prepare_env(task_dir=str(tmp_path / "task1"), task_kind=TASK_KIND_CONFIG)
    first = subprocess.run(
        [sys.executable, "-c", "import pyruns; print(pyruns.__version__)"],
        cwd=tmp_path,
        env=env1,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "first-pyruns"

    init_file.write_text("__version__ = 'second-pyruns'\n", encoding="utf-8")

    env2 = _prepare_env(task_dir=str(tmp_path / "task2"), task_kind=TASK_KIND_CONFIG)
    second = subprocess.run(
        [sys.executable, "-c", "import pyruns; print(pyruns.__version__)"],
        cwd=tmp_path,
        env=env2,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "second-pyruns"


def test_prepare_env_refreshes_isolated_pyruns_root_when_nested_module_changes(tmp_path, monkeypatch):
    """The isolated copy should refresh when any Python module in pyruns changes."""

    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = launcher_site_packages / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    nested_module = launcher_pyruns / "core" / "config_manager.py"
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")
    nested_module.write_text("MARKER = 'first-module'\n", encoding="utf-8")

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env1 = _prepare_env(task_dir=str(tmp_path / "task1"), task_kind=TASK_KIND_CONFIG)
    first = subprocess.run(
        [sys.executable, "-c", "from pyruns.core import config_manager; print(config_manager.MARKER)"],
        cwd=tmp_path,
        env=env1,
        capture_output=True,
        text=True,
        check=False,
    )
    assert first.returncode == 0, first.stderr
    assert first.stdout.strip() == "first-module"

    nested_module.write_text("MARKER = 'second-module'\n", encoding="utf-8")

    env2 = _prepare_env(task_dir=str(tmp_path / "task2"), task_kind=TASK_KIND_CONFIG)
    second = subprocess.run(
        [sys.executable, "-c", "from pyruns.core import config_manager; print(config_manager.MARKER)"],
        cwd=tmp_path,
        env=env2,
        capture_output=True,
        text=True,
        check=False,
    )

    assert second.returncode == 0, second.stderr
    assert second.stdout.strip() == "second-module"


def test_prepare_env_reuses_isolated_pyruns_root_for_same_package_fingerprint(tmp_path, monkeypatch):
    """Repeated task launches should not recopy pyruns when package files are unchanged."""

    launcher_source_root = tmp_path / "launcher-source"
    launcher_pyruns = launcher_source_root / "pyruns"
    (launcher_pyruns / "core").mkdir(parents=True)
    (launcher_pyruns / "__init__.py").write_text("__version__ = 'new-pyruns'\n", encoding="utf-8")
    (launcher_pyruns / "core" / "__init__.py").write_text("", encoding="utf-8")
    (launcher_pyruns / "core" / "executor.py").write_text("", encoding="utf-8")

    original_copytree = executor.shutil.copytree
    copy_sources: list[str] = []

    def counting_copytree(src, dst, *args, **kwargs):
        copy_sources.append(os.path.normcase(os.path.abspath(str(src))))
        return original_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.setattr(executor.shutil, "copytree", counting_copytree)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    env1 = _prepare_env(task_dir=str(tmp_path / "task1"), task_kind=TASK_KIND_CONFIG)
    env2 = _prepare_env(task_dir=str(tmp_path / "task2"), task_kind=TASK_KIND_CONFIG)

    normalized_package = os.path.normcase(os.path.abspath(str(launcher_pyruns)))
    assert copy_sources.count(normalized_package) == 1
    assert env1["PYTHONPATH"].split(os.pathsep)[:2] == env2["PYTHONPATH"].split(os.pathsep)[:2]


def test_config_node_init():
    data = {
        "lr": 0.01,
        "optimizer": {
            "name": "adam",
            "beta": 0.9
        },
        "layers": [64, 128, {"dropout": 0.5}],
        "_private": "hidden"
    }
    node = ConfigNode(data)
    
    assert node.lr == 0.01
    assert node.optimizer.name == "adam"
    assert node.optimizer.beta == 0.9
    assert len(node.layers) == 3
    assert node.layers[0] == 64
    assert node.layers[2].dropout == 0.5
    assert getattr(node, "_private") == "hidden"


def test_config_node_to_dict():
    data = {
        "lr": 0.01,
        "optimizer": {
            "name": "adam",
            "beta": 0.9
        },
        "layers": [64, {"dropout": 0.5}],
        "_private": "should be ignored"
    }
    node = ConfigNode(data)
    d = node.to_dict()
    
    assert "lr" in d
    assert "optimizer" in d
    assert isinstance(d["optimizer"], dict)
    assert d["optimizer"]["name"] == "adam"
    assert isinstance(d["layers"], list)
    assert isinstance(d["layers"][1], dict)
    assert d["layers"][1]["dropout"] == 0.5
    assert "_private" not in d


def test_config_node_repr():
    node = ConfigNode({"a": 1, "b": "str"})
    r = repr(node)
    assert "ConfigNode(" in r
    assert "a=1" in r
    assert "b='str'" in r


def test_config_manager_not_loaded():
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="not loaded"):
        cm.load()


def test_config_manager_file_not_found():
    cm = ConfigManager()
    with pytest.raises(FileNotFoundError):
        cm.read("does_not_exist_at_all.yaml")


def test_config_manager_read_yaml(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("a: 1\nb: 2", encoding="utf-8")
    
    cm = ConfigManager()
    cm.read(str(p))
    node = cm.load()
    assert node.a == 1
    assert node.b == 2


def test_config_manager_read_json(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"a": 1, "b": {"c": 3}}', encoding="utf-8")
    
    cm = ConfigManager()
    cm.read(str(p))
    node = cm.load()
    assert node.a == 1
    assert node.b.c == 3


def test_config_manager_unsupported_format(tmp_path):
    p = tmp_path / "cfg.txt"
    p.write_text("Hello", encoding="utf-8")
    
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Unsupported format"):
        cm.read(str(p))


def test_config_manager_read_list(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("- a: 1\n- b: 2", encoding="utf-8")
    
    cm = ConfigManager()
    cm.read(str(p))
    nodes = cm.load()
    assert isinstance(nodes, list)
    assert len(nodes) == 2
    assert nodes[0].a == 1
    assert nodes[1].b == 2


@pytest.fixture
def mock_logger(monkeypatch):
    class MockLogger:
        logs = []
        def info(self, msg, *args):
            self.logs.append(("INFO", msg % args))
        def error(self, msg, *args):
            self.logs.append(("ERROR", msg % args))
    logger = MockLogger()
    monkeypatch.setattr("pyruns.core.config_manager.logger", logger)
    return logger

def test_config_manager_parse_error(tmp_path, mock_logger):
    p = tmp_path / "bad.yaml"
    p.write_text("a: \n  - b:\n c: [invalid yaml", encoding="utf-8")
    
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Failed to parse config"):
        cm.read(str(p))
    
    assert any("Failed to parse config" in msg for lvl, msg in mock_logger.logs if lvl == "ERROR")


# ═══════════════════════════════════════════════════════════════
#  SystemMonitor
# ═══════════════════════════════════════════════════════════════

@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_sample(mock_subprocess, mock_psutil):
    # Setup CPU/RAM mocks
    mock_psutil.cpu_percent.return_value = 25.5
    mock_mem = MagicMock()
    mock_mem.percent = 60.0
    mock_psutil.virtual_memory.return_value = mock_mem
    mock_psutil.Process.side_effect = [
        MagicMock(username=MagicMock(return_value="alice")),
        MagicMock(username=MagicMock(return_value="bob")),
        MagicMock(username=MagicMock(return_value="carol")),
    ]
    
    # Setup GPU + process mocks
    mock_subprocess.side_effect = [
        (
            b"0, NVIDIA RTX 4090, GPU-AAA, 45.0, 4000.0, 8000.0\n"
            b"1, NVIDIA RTX 4080, GPU-BBB, 90.0, 8000.0, 8000.0\n"
        ),
        (
            b"GPU-AAA, 1234, python.exe, 2048\n"
            b"GPU-AAA, 9999, tensorboard.exe, 256\n"
            b"GPU-BBB, 5678, train.py, 4096\n"
        ),
    ]
    
    monitor = SystemMonitor()
    metrics = monitor.sample()
    
    # Assert CPU/RAM
    assert metrics["cpu_percent"] == 25.5
    assert metrics["mem_percent"] == 60.0
    
    # Assert GPU
    gpus = metrics["gpus"]
    assert len(gpus) == 2
    assert gpus[0]["id"] == 0
    assert gpus[0]["index"] == 0
    assert gpus[0]["name"] == "NVIDIA RTX 4090"
    assert gpus[0]["uuid"] == "GPU-AAA"
    assert gpus[0]["util"] == 45.0
    assert gpus[0]["mem_used"] == 4000.0
    assert gpus[0]["mem_total"] == 8000.0
    assert [proc["pid"] for proc in gpus[0]["processes"]] == [1234, 9999]
    assert [proc["user"] for proc in gpus[0]["processes"]] == ["alice", "bob"]
    
    assert gpus[1]["index"] == 1
    assert gpus[1]["name"] == "NVIDIA RTX 4080"
    assert gpus[1]["util"] == 90.0
    assert gpus[1]["processes"][0]["name"] == "train.py"
    assert gpus[1]["processes"][0]["user"] == "carol"
    
    assert monitor._gpu_cache == gpus


@patch("pyruns.core.system_metrics.psutil")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_error(mock_subprocess, mock_psutil):
    mock_psutil.cpu_percent.return_value = 10.0
    mock_psutil.virtual_memory().percent = 20.0
    
    # Setup GPU mock to fail
    mock_subprocess.side_effect = Exception("nvidia-smi failed")
    
    monitor = SystemMonitor()
    # Pre-populate cache to test fallback
    cached_gpus = [{"index": 0, "util": 10.0, "mem_used": 1000.0, "mem_total": 8000.0}]
    monitor._gpu_cache = cached_gpus
    
    metrics = monitor.sample()
    
    # Error should return cache
    assert metrics["gpus"] == cached_gpus


@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_empty(mock_subprocess):
    # Setup GPU to return empty (e.g. no GPUs or driver not loaded properly but command succeeds)
    mock_subprocess.side_effect = [b"   \n\n\n", b""]
    
    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()
    assert gpus == []


@patch("pyruns.core.system_metrics.time.monotonic")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_reuses_empty_gpu_cache_until_ttl_expires(mock_subprocess, mock_monotonic):
    mock_monotonic.side_effect = [10.0, 10.5, 12.0]
    mock_subprocess.side_effect = [
        b"   \n\n\n",
        b"",
        b"0, NVIDIA RTX 4090, GPU-AAA, 1.0, 1000.0, 8000.0\n",
        b"",
    ]

    monitor = SystemMonitor()

    assert monitor._get_gpu_metrics() == []
    assert monitor._gpu_cache_valid is True
    assert monitor._get_gpu_metrics() == []
    assert mock_subprocess.call_count == 2

    gpus = monitor._get_gpu_metrics()

    assert len(gpus) == 1
    assert gpus[0]["uuid"] == "GPU-AAA"
    assert mock_subprocess.call_count == 4


@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_process_query_failure_still_returns_gpu_summary(mock_subprocess):
    mock_subprocess.side_effect = [
        b"0, NVIDIA RTX 4090, GPU-AAA, 45.0, 4000.0, 8000.0\n",
        Exception("process query failed"),
    ]

    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()

    assert len(gpus) == 1
    assert gpus[0]["name"] == "NVIDIA RTX 4090"
    assert gpus[0]["processes"] == []


@patch("pyruns.core.system_metrics.psutil.Process")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_process_user_falls_back_to_unknown(mock_subprocess, mock_process):
    mock_subprocess.return_value = b"GPU-AAA, 1234, python.exe, 2048\n"
    mock_process.side_effect = psutil.AccessDenied(pid=1234)

    monitor = SystemMonitor()
    processes = monitor._get_gpu_processes()

    assert processes["GPU-AAA"][0]["user"] == "unknown"


@patch("pyruns.core.system_metrics.psutil.Process")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_gpu_csv_parser_handles_quoted_names(mock_subprocess, mock_process):
    mock_process.return_value.username.return_value = "alice"
    mock_subprocess.side_effect = [
        b'0, "NVIDIA, RTX 4090", GPU-AAA, 45.0, 4000.0, 8000.0\n',
        b'GPU-AAA, 1234, "python, train.py", 2048\n',
    ]

    monitor = SystemMonitor()
    gpus = monitor._get_gpu_metrics()

    assert gpus[0]["name"] == "NVIDIA, RTX 4090"
    assert gpus[0]["processes"][0]["name"] == "python, train.py"


@patch("pyruns.core.system_metrics.time.monotonic")
@patch("pyruns.core.system_metrics.subprocess.check_output")
def test_system_monitor_retries_after_gpu_disable_cooldown(mock_subprocess, mock_monotonic):
    mock_monotonic.side_effect = [0.0, 1.0, 2.0, 20.0, 40.0]
    mock_subprocess.side_effect = [
        Exception("nvidia-smi failed"),
        Exception("nvidia-smi failed"),
        Exception("nvidia-smi failed"),
        b"0, NVIDIA RTX 4090, GPU-AAA, 45.0, 4000.0, 8000.0\n",
        b"",
    ]

    monitor = SystemMonitor()

    assert monitor._get_gpu_metrics() == []
    assert monitor._get_gpu_metrics() == []
    assert monitor._get_gpu_metrics() == []
    assert monitor._gpu_available is False
    assert mock_subprocess.call_count == 3

    assert monitor._get_gpu_metrics() == []
    assert mock_subprocess.call_count == 3

    gpus = monitor._get_gpu_metrics()
    assert len(gpus) == 1
    assert gpus[0]["uuid"] == "GPU-AAA"
    assert monitor._gpu_available is True


# ═══════════════════════════════════════════════════════════════
#  Executor
# ═══════════════════════════════════════════════════════════════


def test_prepare_env():
    env1 = _prepare_env(extra_env={"CUDA_VISIBLE_DEVICES": "1"})
    assert env1["PYTHONIOENCODING"] == "utf-8"
    assert env1["CUDA_VISIBLE_DEVICES"] == "1"
    
    env2 = _prepare_env(task_dir="/fake/dir")
    assert env2[ENV_KEY_CONFIG] == os.path.join("/fake/dir", CONFIG_FILENAME)

    env3 = _prepare_env(task_dir="/fake/dir", task_kind=TASK_KIND_SHELL, config_file=SHELL_CONFIG_FILENAME)
    assert ENV_KEY_CONFIG not in env3


def test_prepare_env_prefers_current_python_executable_on_path(monkeypatch):
    stale_path = os.pathsep.join(["/not/current/python", "/another/bin"])
    monkeypatch.setenv("PATH", stale_path)

    env = _prepare_env(task_dir="/fake/dir", task_kind=TASK_KIND_SHELL)

    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[0] == os.path.dirname(sys.executable)
    assert "/not/current/python" in path_entries


def test_prepare_env_preserves_parent_conda_environment_and_applies_task_overrides(monkeypatch):
    monkeypatch.setenv("CONDA_PREFIX", "/opt/conda/envs/exp")
    monkeypatch.setenv("CONDA_DEFAULT_ENV", "exp")
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "0,1")
    monkeypatch.setenv("PYTHONPATH", "/parent/pythonpath")

    env = _prepare_env(
        extra_env={"CUDA_VISIBLE_DEVICES": "2", "PYRUNS_EXAMPLE_ENV": "task-value"},
        task_dir="/fake/task",
        task_kind=TASK_KIND_CONFIG,
    )

    assert env["CONDA_PREFIX"] == "/opt/conda/envs/exp"
    assert env["CONDA_DEFAULT_ENV"] == "exp"
    assert env["CUDA_VISIBLE_DEVICES"] == "2"
    assert env["PYRUNS_EXAMPLE_ENV"] == "task-value"
    assert "/parent/pythonpath" in env["PYTHONPATH"]
    assert env[ENV_KEY_CONFIG] == os.path.join("/fake/task", CONFIG_FILENAME)


def test_resolve_python_runtime_from_task_env_python_executable(tmp_path):
    fake_python = tmp_path / "env" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("", encoding="utf-8")

    runtime = _resolve_python_runtime(extra_env={ENV_KEY_PYTHON_EXECUTABLE: str(fake_python)})

    assert runtime["mode"] == "python"
    assert runtime["source"] == "task_env"
    assert runtime["python_executable"] == str(fake_python.resolve())


def test_resolve_python_runtime_from_workspace_conda_settings(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
    fake_conda = tmp_path / "conda"
    fake_conda.write_text("", encoding="utf-8")
    workspace = tmp_path / DEFAULT_ROOT_NAME / "main"
    task_dir = workspace / "tasks" / "task1"
    task_dir.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        f"conda_env: eval-env\nconda_executable: {json.dumps(str(fake_conda))}\n",
        encoding="utf-8",
    )

    runtime = _resolve_python_runtime(str(task_dir))

    assert runtime["mode"] == "conda"
    assert runtime["source"] == "workspace_settings"
    assert runtime["conda_env"] == "eval-env"
    assert runtime["conda_executable"] == str(fake_conda.resolve())


def test_prepare_env_uses_runtime_python_executable_on_path(tmp_path):
    fake_python = tmp_path / "env" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("", encoding="utf-8")

    env = _prepare_env(
        task_dir="/fake/dir",
        task_kind=TASK_KIND_SHELL,
        python_runtime={"mode": "python", "python_executable": str(fake_python)},
    )

    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[0] == str(fake_python.parent)
    assert env[ENV_KEY_PYTHON_EXECUTABLE] == str(fake_python)


def test_prepare_env_marks_conda_runtime():
    env = _prepare_env(
        task_dir="/fake/dir",
        task_kind=TASK_KIND_SHELL,
        python_runtime={
            "mode": "conda",
            "conda_env": "eval-env",
            "conda_executable": "/opt/conda/bin/conda",
        },
    )

    assert env[ENV_KEY_CONDA_ENV] == "eval-env"
    assert env[ENV_KEY_CONDA_EXE] == "/opt/conda/bin/conda"


def test_prepare_env_applies_workspace_global_env_before_task_env(tmp_path, monkeypatch):
    monkeypatch.delenv(ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
    monkeypatch.setenv("TOKENIZERS_PARALLELISM", "terminal")
    workspace = tmp_path / DEFAULT_ROOT_NAME / "main"
    task_dir = workspace / "tasks" / "task1"
    task_dir.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "global_env:\n"
        "  TOKENIZERS_PARALLELISM: workspace\n"
        "  CUDA_VISIBLE_DEVICES: '0'\n",
        encoding="utf-8",
    )

    env = _prepare_env(
        extra_env={"CUDA_VISIBLE_DEVICES": "1"},
        task_dir=str(task_dir),
        task_kind=TASK_KIND_CONFIG,
    )

    assert env["TOKENIZERS_PARALLELISM"] == "workspace"
    assert env["CUDA_VISIBLE_DEVICES"] == "1"


def test_cli_terminal_runtime_skips_workspace_runtime_settings(tmp_path, monkeypatch):
    fake_conda = tmp_path / "conda"
    fake_conda.write_text("", encoding="utf-8")
    workspace = tmp_path / DEFAULT_ROOT_NAME / "main"
    task_dir = workspace / "tasks" / "task1"
    task_dir.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        f"conda_env: eval-env\nconda_executable: {json.dumps(str(fake_conda))}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    runtime = _resolve_python_runtime(str(task_dir))

    assert runtime["mode"] == "follow"
    assert runtime["source"] == "pyruns_process"


def test_cli_terminal_runtime_keeps_task_runtime_override(tmp_path, monkeypatch):
    fake_python = tmp_path / "env" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("", encoding="utf-8")
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    runtime = _resolve_python_runtime(extra_env={ENV_KEY_PYTHON_EXECUTABLE: str(fake_python)})

    assert runtime["mode"] == "python"
    assert runtime["source"] == "task_env"


def test_cli_terminal_runtime_skips_workspace_global_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CUDA_VISIBLE_DEVICES", "terminal")
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")
    workspace = tmp_path / DEFAULT_ROOT_NAME / "main"
    task_dir = workspace / "tasks" / "task1"
    task_dir.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "global_env:\n"
        "  CUDA_VISIBLE_DEVICES: workspace\n",
        encoding="utf-8",
    )

    env = _prepare_env(task_dir=str(task_dir), task_kind=TASK_KIND_CONFIG)

    assert env["CUDA_VISIBLE_DEVICES"] == "terminal"


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "lr": {"name": "--lr", "default": 0.01},
        "epochs": {"name": "--epochs", "default": 5},
    }
    
    script_path = "train.py"
    config = {"lr": 0.05, "epochs": 10, "flag": True}
    
    cmd, wd, cleanup_paths = _build_command(None, script_path, None, config)
    
    # sys.executable, train.py, --lr, 0.05, --epochs, 10, --flag
    assert cmd[0] == sys.executable
    assert cmd[1] == "train.py"
    assert "--lr" in cmd
    assert "0.05" in cmd
    assert "--flag" in cmd
    assert cleanup_paths == []


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_uses_declared_flags_and_bool_actions(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "batch_size": {"name": "--batch-size", "default": 32},
        "use_amp": {"name": "--use-amp", "action": "store_true", "default": False},
        "cache": {"name": "--no-cache", "action": "store_false", "default": True},
    }

    cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"batch_size": 64, "use_amp": True, "cache": False},
    )

    assert "--batch-size" in cmd
    assert "--batch_size" not in cmd
    assert cmd[cmd.index("--batch-size") + 1] == "64"
    assert "--use-amp" in cmd
    assert "--no-cache" in cmd


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_groups_nargs_list_values(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "layers": {"name": "--layers", "nargs": "+", "default": [64]},
    }

    cmd, _, _ = _build_command(None, "train.py", None, {"layers": [128, 256]})

    assert cmd.count("--layers") == 1
    index = cmd.index("--layers")
    assert cmd[index:index + 3] == ["--layers", "128", "256"]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_append_with_nargs_repeats_grouped_values(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "pair": {"name": "--pair", "action": "append", "nargs": 2, "default": []},
    }

    cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"pair": [["train", "dev"], ["test", "holdout"]]},
    )

    assert cmd == [
        sys.executable,
        "train.py",
        "--pair",
        "train",
        "dev",
        "--pair",
        "test",
        "holdout",
    ]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_boolean_optional_and_typed_bool(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "compile": {
            "name": "--compile",
            "action": "argparse.BooleanOptionalAction",
            "default": True,
        },
        "enabled": {
            "name": "--enabled",
            "type": "bool",
            "default": True,
        },
    }

    cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"compile": False, "enabled": False},
    )

    assert "--no-compile" in cmd
    assert "--enabled" in cmd
    assert cmd[cmd.index("--enabled") + 1] == "False"


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_count_action_repeats_flag(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "verbose": {
            "flags": ["-v", "--verbose"],
            "name": "--verbose",
            "action": "count",
            "default": 0,
        },
    }

    cmd, _, _ = _build_command(None, "train.py", None, {"verbose": 2})

    assert cmd.count("--verbose") == 2
    assert "2" not in cmd


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_non_argparse(mock_detect):
    mock_detect.return_value = ("pyruns_load", None)
    
    script_path = "train.py"
    config = {"lr": 0.05}
    
    cmd, wd, cleanup_paths = _build_command(None, script_path, None, config)
    
    # Should only contain python and script, no args appended
    assert len(cmd) == 2
    assert cmd[0] == sys.executable
    assert cmd[1] == "train.py"
    assert cleanup_paths == []


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_python_task_uses_script_directory_workdir(mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    script_dir = tmp_path / "project"
    script_dir.mkdir()
    script_path = script_dir / "train.py"
    script_path.write_text("print('cwd')\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(None, str(script_path), None, {})

    assert cmd == [sys.executable, str(script_path)]
    assert wd == str(script_dir)
    assert cleanup_paths == []


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_python_task_uses_runtime_python_executable(mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    fake_python = tmp_path / "env" / "bin" / "python"
    fake_python.parent.mkdir(parents=True)
    fake_python.write_text("", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        "train.py",
        None,
        {},
        python_runtime={"mode": "python", "python_executable": str(fake_python)},
    )

    assert cmd == [str(fake_python), "train.py"]
    assert cleanup_paths == []


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_python_task_uses_conda_runtime(mock_detect):
    mock_detect.return_value = ("pyruns_load", None)

    cmd, _, cleanup_paths = _build_command(
        None,
        "train.py",
        None,
        {},
        python_runtime={
            "mode": "conda",
            "conda_env": "eval-env",
            "conda_executable": "/opt/conda/bin/conda",
        },
    )

    assert cmd == [
        "/opt/conda/bin/conda",
        "run",
        "-n",
        "eval-env",
        "--no-capture-output",
        "python",
        "train.py",
    ]
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_posix(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: False)
    mock_shell.return_value = "/bin/bash"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("echo hello\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )

    assert cmd == ["/bin/bash", str(script_path)]
    assert wd == str(task_dir)
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_wraps_conda_runtime(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: False)
    mock_shell.return_value = "/bin/bash"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("python train.py\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
        python_runtime={
            "mode": "conda",
            "conda_env": "eval-env",
            "conda_executable": "/opt/conda/bin/conda",
        },
    )

    assert cmd == [
        "/opt/conda/bin/conda",
        "run",
        "-n",
        "eval-env",
        "--no-capture-output",
        "/bin/bash",
        str(script_path),
    ]
    assert wd == str(task_dir)
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_uses_project_root_workdir(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: False)
    mock_shell.return_value = "/bin/bash"
    project_root = tmp_path / "project"
    task_dir = project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME / "tasks" / "task"
    task_dir.mkdir(parents=True)
    workspace_dir = task_dir.parents[1]
    (workspace_dir / SCRIPT_INFO_FILENAME).write_text(
        json.dumps({"workspace_kind": "shell", "project_root": str(project_root)}),
        encoding="utf-8",
    )
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("pwd\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )

    assert cmd == ["/bin/bash", str(script_path)]
    assert wd == str(project_root).replace("\\", "/")
    assert cleanup_paths == []


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_windows_cmd(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: True)
    mock_shell.return_value = r"C:\Windows\System32\cmd.exe"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("#!/usr/bin/env bash\necho hello\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )

    wrapper_path = Path(cleanup_paths[0])
    assert cmd == [r"C:\Windows\System32\cmd.exe", "/d", "/c", str(wrapper_path)]
    assert wd == str(task_dir)
    assert wrapper_path.exists()
    assert wrapper_path.parent != task_dir
    wrapper_content = wrapper_path.read_text(encoding="utf-8-sig")
    assert "#!/usr/bin/env bash" not in wrapper_content
    assert "echo hello" in wrapper_content
    wrapper_path.unlink()


@patch("pyruns.core.executor._resolve_shell_executable")
def test_build_command_shell_task_windows_powershell(mock_shell, tmp_path, monkeypatch):
    monkeypatch.setattr("pyruns.core.executor._is_windows", lambda: True)
    mock_shell.return_value = r"C:\Program Files\PowerShell\7\pwsh.exe"
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script_path = task_dir / SHELL_CONFIG_FILENAME
    script_path.write_text("#!/usr/bin/env bash\nWrite-Host 'hello'\n", encoding="utf-8")

    cmd, wd, cleanup_paths = _build_command(
        None,
        None,
        None,
        {},
        task_kind=TASK_KIND_SHELL,
        task_dir=str(task_dir),
        config_file=SHELL_CONFIG_FILENAME,
    )
    wrapper_path = Path(cleanup_paths[0])
    assert cmd == [
        r"C:\Program Files\PowerShell\7\pwsh.exe",
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(wrapper_path),
    ]
    assert wd == str(task_dir)
    assert wrapper_path.exists()
    assert wrapper_path.parent != task_dir
    wrapper_content = wrapper_path.read_text(encoding="utf-8-sig")
    assert "#!/usr/bin/env bash" not in wrapper_content
    assert "Write-Host 'hello'" in wrapper_content
    assert "[Console]::OutputEncoding" in wrapper_content
    assert "$OutputEncoding = $__pyrunsUtf8" in wrapper_content
    wrapper_path.unlink()


@patch("pyruns.utils.shell_runtime.get_follow_shell_runtime")
def test_shell_runtime_follow_mode_ignores_shell_executable_setting(mock_follow_shell, tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text("shell_mode: follow\nshell_executable: bash.exe\n", encoding="utf-8")
    mock_follow_shell.return_value = {
        "mode": "follow",
        "source": "follow_terminal",
        "terminal_kind": "powershell",
        "display_name": "PowerShell",
        "executable": r"C:\Program Files\PowerShell\7\pwsh.exe",
        "available": True,
    }

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["mode"] == "follow"
    assert runtime["terminal_kind"] == "powershell"
    assert runtime["executable"] == r"C:\Program Files\PowerShell\7\pwsh.exe"


def test_shell_runtime_custom_mode_uses_explicit_shell_executable(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "shell_mode: custom\nshell_executable: /custom/shell\n",
        encoding="utf-8",
    )

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["mode"] == "custom"
    assert runtime["source"] == "custom_shell"
    assert runtime["executable"] == "/custom/shell"


def test_shell_runtime_custom_mode_marks_known_shell_unavailable_when_it_cannot_start(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    fake_bash = tmp_path / "bash.exe"
    fake_bash.write_text("not a real shell", encoding="utf-8")
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "shell_mode: custom\n"
        f"shell_executable: {json.dumps(str(fake_bash))}\n",
        encoding="utf-8",
    )

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["terminal_kind"] == "bash"
    assert runtime["executable"] == str(fake_bash)
    assert runtime["available"] is False


def test_shell_runtime_custom_mode_marks_unknown_shell_unavailable(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    fake_shell = tmp_path / "not-a-shell.bin"
    fake_shell.write_text("not a real shell", encoding="utf-8")
    settings_path = workspace.parent / "_pyruns_settings.yaml"
    settings_path.write_text(
        "shell_mode: custom\n"
        f"shell_executable: {json.dumps(str(fake_shell))}\n",
        encoding="utf-8",
    )

    runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["terminal_kind"] == "unknown"
    assert runtime["display_name"] == "Custom shell"
    assert runtime["executable"] == str(fake_shell)
    assert runtime["available"] is False


def test_shell_runtime_follow_mode_probes_detected_shell_availability(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    fake_bash = tmp_path / "follow-bash.exe"
    fake_bash.write_text("not a real shell", encoding="utf-8")

    with patch("pyruns.utils.shell_runtime.get_follow_shell_runtime") as mock_runtime:
        mock_runtime.return_value = {
            "source": "follow_terminal",
            "terminal_kind": "bash",
            "display_name": "Bash",
            "executable": str(fake_bash),
            "available": True,
        }

        runtime = get_shell_runtime_for_workspace(str(workspace))

    assert runtime["mode"] == "follow"
    assert runtime["terminal_kind"] == "bash"
    assert runtime["available"] is False


def test_shell_runtime_config_filename_tracks_custom_shell_kind(tmp_path):
    workspace = tmp_path / "_pyruns_" / "main"
    workspace.mkdir(parents=True)
    settings_path = workspace.parent / "_pyruns_settings.yaml"

    settings_path.write_text("shell_mode: custom\nshell_executable: sh\n", encoding="utf-8")
    assert get_shell_config_filename_for_workspace(str(workspace)) == SHELL_CONFIG_FILENAME

    settings_path.write_text("shell_mode: custom\nshell_executable: pwsh.exe\n", encoding="utf-8")
    assert get_shell_config_filename_for_workspace(str(workspace)) == POWERSHELL_CONFIG_FILENAME


def test_shell_workspace_root_uses_project_root_when_given_pyruns_root(tmp_path):
    project_root = tmp_path / DEFAULT_ROOT_NAME
    project_root.mkdir(parents=True)

    shell_root = shell_workspace_root_for_run_root(str(project_root))

    assert shell_root == str(project_root / SHELL_WORKSPACE_NAME).replace("\\", "/")


def test_shell_workspace_root_uses_parent_pyruns_root_when_given_script_workspace(tmp_path):
    script_root = tmp_path / DEFAULT_ROOT_NAME / "main"
    script_root.mkdir(parents=True)

    shell_root = shell_workspace_root_for_run_root(str(script_root))

    assert shell_root == str(tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME).replace("\\", "/")


def test_shell_named_python_script_uses_reserved_safe_workspace_dir(tmp_path):
    script_path = tmp_path / "_shell_.py"
    script_path.write_text(
        "\n".join(
            [
                "import argparse",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--epochs', type=int, default=3)",
                "parser.parse_args()",
                "",
            ]
        ),
        encoding="utf-8",
    )

    expected_workspace = str(tmp_path / DEFAULT_ROOT_NAME / f"py{SHELL_WORKSPACE_NAME}").replace("\\", "/")

    assert workspace_root_for_script(str(script_path)) == expected_workspace
    workspace = bootstrap_workspace(str(script_path))
    info = json.loads(Path(workspace, SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))

    assert workspace == expected_workspace
    assert info["workspace_kind"] == "script"
    assert info["script_name"] == "_shell_"
    assert shell_workspace_root_for_run_root(workspace) == str(
        tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME
    ).replace("\\", "/")
    assert any(
        item["script_name"] == "_shell_" and item["workspace_path"] == expected_workspace
        for item in list_script_candidates(str(tmp_path))
    )


def test_bootstrap_shell_workspace_records_project_root(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    shell_root = bootstrap_shell_workspace(str(project_root / DEFAULT_ROOT_NAME))
    info = json.loads(Path(shell_root, SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))

    assert shell_root == str(project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME).replace("\\", "/")
    assert info["project_root"] == str(project_root).replace("\\", "/")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_hydra_requires_shell_workspace(mock_detect):
    mock_detect.return_value = ("hydra", None)
    with pytest.raises(RuntimeError, match="shell workspace/task"):
        _build_command(None, "train.py", None, {})


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_unknown_requires_shell_workspace(mock_detect):
    mock_detect.return_value = ("unknown", None)
    with pytest.raises(RuntimeError, match="configuration style"):
        _build_command(None, "train.py", None, {})


def test_executor_runtime_path_and_shell_resolution_edges(tmp_path, monkeypatch):
    import pyruns.core.executor as executor

    missing = tmp_path / "missing"
    env = {}
    executor._prepend_pythonpath(env, str(missing))
    assert "PYTHONPATH" not in env

    package_root = tmp_path / "package"
    package_root.mkdir()
    env = {"PYTHONPATH": str(package_root)}
    executor._prepend_pythonpath(env, str(package_root))
    assert env["PYTHONPATH"] == str(package_root)

    extra_root = tmp_path / "extra"
    extra_root.mkdir()
    executor._prepend_pythonpath(env, str(extra_root))
    assert env["PYTHONPATH"].split(os.pathsep)[0] == str(extra_root)

    assert executor._path_env_key({"path": "lower"}) == "path"
    assert executor._path_env_key({"CustomPath": "mixed"}) == "PATH"

    env = {"Path": str(package_root), "PATH": "duplicate"}
    executor._prepend_path_entries(env, [str(missing)])
    assert env == {"Path": str(package_root), "PATH": "duplicate"}

    front = tmp_path / "front"
    front.mkdir()
    executor._prepend_path_entries(env, [str(front), str(front), str(package_root)])
    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[:2] == [str(front), str(package_root)]
    assert "Path" not in env

    python_exe = tmp_path / "python.exe"
    conda_exe = tmp_path / "conda.exe"
    python_exe.write_text("", encoding="utf-8")
    conda_exe.write_text("", encoding="utf-8")

    assert executor._resolve_executable_path(str(python_exe)) == str(python_exe.resolve())
    monkeypatch.setattr(executor.shutil, "which", lambda value: str(conda_exe) if value == "conda" else None)
    assert executor._resolve_executable_path("conda") == str(conda_exe.resolve())

    with pytest.raises(RuntimeError, match="python_executable"):
        executor._runtime_from_values(python_executable=str(missing), source="task")
    with pytest.raises(RuntimeError, match="conda_executable"):
        executor._runtime_from_values(conda_env="env", conda_executable=str(missing), source="task")
    assert executor._runtime_from_values(python_executable=str(python_exe), source="task")["mode"] == "python"
    assert executor._runtime_from_values(conda_env="env", conda_executable="conda", source="task")["mode"] == "conda"

    shell_exe = tmp_path / "bash.exe"
    shell_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        executor,
        "get_shell_runtime_for_task",
        lambda task_dir=None: {"mode": "custom", "executable": str(shell_exe), "available": True},
    )
    assert executor._resolve_shell_executable(str(tmp_path)) == str(shell_exe)

    monkeypatch.setattr(
        executor,
        "get_shell_runtime_for_task",
        lambda task_dir=None: {"mode": "custom", "executable": str(shell_exe), "available": False},
    )
    with pytest.raises(RuntimeError, match="shell_mode=custom"):
        executor._resolve_shell_executable(str(tmp_path))

    monkeypatch.setattr(
        executor,
        "get_shell_runtime_for_task",
        lambda task_dir=None: {"mode": "follow", "executable": "", "available": False},
    )
    with pytest.raises(RuntimeError, match="Unable to resolve"):
        executor._resolve_shell_executable(str(tmp_path))


def test_executor_shell_workdir_and_wrapper_edge_paths(tmp_path):
    import pyruns.core.executor as executor

    project_root = tmp_path / "project"
    task_dir = project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME / TASKS_DIR / "alpha"
    task_dir.mkdir(parents=True)
    script_info = task_dir.parents[1] / SCRIPT_INFO_FILENAME
    script_info.write_text("{bad json", encoding="utf-8")

    assert executor._resolve_shell_workdir(str(task_dir)) == str(project_root.resolve()).replace("\\", "/")

    external_root = tmp_path / "external"
    external_root.mkdir()
    script_info.write_text(json.dumps({"project_root": str(external_root)}), encoding="utf-8")
    assert executor._resolve_shell_workdir(str(task_dir)) == str(external_root.resolve()).replace("\\", "/")

    loose_task_dir = tmp_path / "loose" / TASKS_DIR / "task"
    loose_task_dir.mkdir(parents=True)
    assert executor._resolve_shell_workdir(str(loose_task_dir)) == str(loose_task_dir)

    script_path = task_dir / "run.sh"
    script_path.write_text("#!/usr/bin/env bash\necho hello\n", encoding="utf-8")
    assert executor._read_shell_script_body(str(script_path)) == "echo hello\n"

    command, workdir, cleanup_paths = executor._materialize_windows_shell_wrapper(
        str(task_dir),
        str(script_path),
        "bash.exe",
    )
    assert command == ["bash.exe", str(script_path)]
    assert workdir == str(task_dir)
    assert cleanup_paths == []


def test_executor_import_isolation_helpers_copy_and_skip_edges(tmp_path, monkeypatch):
    import pyruns.core.executor as executor

    package_parent = tmp_path / "site"
    package_dir = package_parent / "pyruns"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("__version__ = 'local'\n", encoding="utf-8")
    (package_dir / "static").mkdir()
    (package_dir / "static" / "asset.js").write_text("ignored", encoding="utf-8")
    dist_info = package_parent / "pyruns-1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text("Name: pyruns\n", encoding="utf-8")
    (dist_info / "__pycache__").mkdir()
    (dist_info / "__pycache__" / "x.pyc").write_bytes(b"bad")

    import_root = tmp_path / "import-root"
    import_root.mkdir()
    executor._copy_dist_info(str(package_parent), str(import_root))
    assert (import_root / "pyruns-1.0.dist-info" / "METADATA").exists()
    assert not (import_root / "pyruns-1.0.dist-info" / "__pycache__").exists()

    executor._copy_dist_info(str(package_parent), str(import_root))
    monkeypatch.setattr(executor.os, "listdir", lambda _path: (_ for _ in ()).throw(OSError("list failed")))
    executor._copy_dist_info(str(package_parent), str(import_root))
    monkeypatch.undo()

    failing_import_root = tmp_path / "failing-import-root"
    failing_import_root.mkdir()
    monkeypatch.setattr(executor.shutil, "copytree", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("copy failed")))
    executor._copy_dist_info(str(package_parent), str(failing_import_root))
    monkeypatch.undo()

    fingerprint = executor._pyruns_package_fingerprint(str(package_dir))
    assert len(fingerprint) == 40

    original_stat = executor.os.stat

    def stat_or_missing(path):
        if str(path).endswith("__init__.py"):
            raise OSError("stat failed")
        return original_stat(path)

    monkeypatch.setattr(executor.os, "stat", stat_or_missing)
    missing_fingerprint = executor._pyruns_package_fingerprint(str(package_dir))
    assert len(missing_fingerprint) == 40
    monkeypatch.undo()

    monkeypatch.setattr(executor.os, "walk", lambda _path: (_ for _ in ()).throw(OSError("walk failed")))
    walk_error_fingerprint = executor._pyruns_package_fingerprint(str(package_dir))
    assert len(walk_error_fingerprint) == 40
    monkeypatch.undo()

    executor._ISOLATED_IMPORT_ROOT_CACHE.clear()
    isolated_root = executor._isolated_pyruns_import_root(str(package_dir))
    assert Path(isolated_root, "pyruns", "__init__.py").exists()
    assert Path(isolated_root, "pyruns-1.0.dist-info", "METADATA").exists()
    assert executor._isolated_pyruns_import_root(str(package_dir)) == isolated_root


def test_executor_runtime_source_and_summary_helpers_cover_edge_paths(tmp_path, monkeypatch):
    import pyruns.core.executor as executor

    monkeypatch.setattr(executor, "_is_windows", lambda: False)
    assert executor._popen_process_group_kwargs() == {"start_new_session": True}

    monkeypatch.delenv(ENV_KEY_CLI_TERMINAL_RUNTIME, raising=False)
    assert executor._cli_terminal_runtime_enabled() is False
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "YES")
    assert executor._cli_terminal_runtime_enabled() is True

    task_dir = tmp_path / "workspace" / TASKS_DIR / "task"
    task_dir.mkdir(parents=True)
    assert executor._python_runtime_settings_root(None) is None
    assert executor._python_runtime_settings_root(str(task_dir)) == str(tmp_path / "workspace")

    assert executor._resolve_executable_path("") == ""
    rel_tool = tmp_path / "bin" / "tool.exe"
    rel_tool.parent.mkdir()
    rel_tool.write_text("", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    assert executor._resolve_executable_path("bin/tool.exe") == str(rel_tool.resolve())

    env = {}
    conda_runtime = {"mode": "conda", "conda_env": "train", "conda_executable": str(rel_tool)}
    executor._prepend_runtime_python_to_path(env, conda_runtime)
    assert env[ENV_KEY_CONDA_ENV] == "train"
    assert env[ENV_KEY_CONDA_EXE] == str(rel_tool)

    assert executor._python_command_prefix(conda_runtime)[:4] == [str(rel_tool), "run", "-n", "train"]
    assert executor._apply_python_runtime_to_shell_command(["echo", "hi"], {"mode": "follow"}) == ["echo", "hi"]

    source_file = tmp_path / "script.py"
    source_file.write_text("print('ok')\n", encoding="utf-8")
    assert executor._file_sha256(None) == "none"
    assert executor._file_sha256(str(tmp_path / "missing.py")) == "missing"
    assert len(executor._file_sha256(str(source_file))) == 12
    assert executor._file_sha256(str(tmp_path)) == "error"

    lease = {"runner_id": "other", "runner_host": "host", "lease_heartbeat": 1, "lease_until": 2}
    executor._clear_runner_lease(lease, "mine")
    assert lease["runner_id"] == "other"
    executor._clear_runner_lease(lease, "other")
    assert lease == {}

    executor._append_error_summary(
        str(task_dir),
        run_index=2,
        title="GPU ERROR",
        detail_lines=["assigned_gpus=0", "cuda_visible_devices=0"],
    )
    error_text = Path(task_dir, RUN_LOGS_DIR, ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "[PYRUNS] GPU ERROR" in error_text
    assert "assigned_gpus=0" in error_text

    save_task_info(str(task_dir), {"name": "task", "_pending_stop_summary": {"run_index": 3, "reason": "stop"}})
    assert executor._consume_pending_stop_summary(str(task_dir), 2) is None
    assert executor._consume_pending_stop_summary(str(task_dir), 3)["reason"] == "stop"
    assert "_pending_stop_summary" not in executor.load_task_info(str(task_dir))

    save_task_info(str(task_dir), {"name": "task", "_pending_stop_summary": "bad"})
    assert executor._consume_pending_stop_summary(str(task_dir), 3) is None

    assert executor._build_run_source_state(task_dir=str(task_dir), script_path=None, workdir=str(tmp_path)).startswith("git ")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_handles_unusual_param_shapes_and_fallbacks(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "input": {"name": []},
        "cache": {"flags": ["--no-cache"], "action": "argparse.BooleanOptionalAction"},
        "short": {"flags": ["-s"], "action": "argparse.BooleanOptionalAction"},
        "flag_bool": {"name": "--flag-bool"},
        "verbose": {"name": "-v", "action": "count"},
        "tag": {"name": "--tag"},
    }

    cmd, _, cleanup_paths = _build_command(
        None,
        "train.py",
        None,
        {
            "input": ["data-a", "data-b"],
            "cache": False,
            "short": False,
            "flag_bool": False,
            "verbose": "bad",
            "tag": ["a", "b"],
        },
    )

    assert cleanup_paths == []
    assert cmd[:3] == [sys.executable, "train.py", "data-a"]
    assert "data-b" in cmd
    assert "--no-cache" in cmd
    assert "-s" in cmd
    assert "--flag-bool" in cmd and "False" in cmd
    assert "-v" not in cmd
    assert cmd.count("--tag") == 2


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_falls_back_when_introspection_fails(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.side_effect = RuntimeError("cannot parse")

    cmd, workdir, cleanup_paths = _build_command(None, "train.py", None, {"lr": 0.1, "dry_run": True})

    assert cmd == [sys.executable, "train.py", "--lr", "0.1", "--dry_run"]
    assert workdir == ""
    assert cleanup_paths == []


def test_build_shell_command_requires_existing_script(tmp_path):
    import pyruns.core.executor as executor

    with pytest.raises(FileNotFoundError):
        executor._build_shell_command(str(tmp_path), SHELL_CONFIG_FILENAME)


def test_build_run_source_state_records_file_hashes_without_config_hash(tmp_path, monkeypatch):
    from pyruns.core import executor

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script = tmp_path / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    config = task_dir / CONFIG_FILENAME
    config.write_text("lr: 0.01\n", encoding="utf-8")
    monkeypatch.setattr(
        executor,
        "_build_git_source_state",
        lambda cwd: "git none | unknown",
    )

    state = executor._build_run_source_state(
        task_dir=str(task_dir),
        script_path=str(script),
        workdir=str(tmp_path),
    )

    assert "git none | unknown" in state
    assert "| script " in state
    assert "config" not in state


def test_build_git_source_state_reports_clean_dirty_and_unknown(monkeypatch):
    from pyruns.core import executor

    status_output = b""

    def fake_git_bytes(cwd, args, **kwargs):
        if args == ["rev-parse", "--show-toplevel"]:
            return b"/repo\n"
        if args == ["rev-parse", "--short=12", "HEAD"]:
            return b"abc123def456\n"
        if args == ["status", "--porcelain=v1", "-z", "--untracked-files=normal"]:
            return status_output
        raise AssertionError(f"unexpected git command: {args}")

    monkeypatch.setattr(executor, "_git_bytes", fake_git_bytes)

    assert executor._build_git_source_state("/repo") == "git abc123def456 | clean"

    status_output = b" M train.py\0?? scratch.py\0"
    assert executor._build_git_source_state("/repo") == "git abc123def456 | dirty"

    status_output = None
    assert executor._build_git_source_state("/repo") == "git abc123def456 | unknown"


def test_build_git_source_state_reports_unknown_without_git_root(monkeypatch):
    from pyruns.core import executor

    monkeypatch.setattr(executor, "_git_bytes", lambda cwd, args, **kwargs: None)

    assert executor._build_git_source_state("/not-a-repo") == "git none | unknown"


def test_git_bytes_disables_optional_git_locks(monkeypatch):
    from pyruns.core import executor

    captured = {}

    class Result:
        returncode = 0
        stdout = b"ok"

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["env"] = kwargs.get("env")
        return Result()

    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)

    assert executor._git_bytes("/repo", ["status", "--porcelain=v1"]) == b"ok"
    assert captured["command"] == ["git", "status", "--porcelain=v1"]
    assert captured["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "1"



@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_success(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    
    task_info = {
        "name": "TestTask",
        "script": "script.py",
        "status": "queued",
        "start_times": [],
        "finish_times": [],
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(task_info, f)
        
    # Mock subprocess with PIPE-style stdout
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.return_value = 0  # Success
    # stdout.read1 returns one chunk then empty bytes (EOF)
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"hello output\n", b''])
    mock_popen.return_value = mock_proc
    
    source_state = "git abc123 | clean | script abc"
    with patch("pyruns.core.executor._build_run_source_state", return_value=source_state):
        res = run_task_worker(
            task_dir=task_dir,
            name="TestTask",
            created_at="now",
            config={},
            run_index=1
        )
    
    assert res["status"] == "completed"
    assert res["progress"] == 1.0
    
    # Check task_info updated
    info = {}
    for _ in range(100):
        with open(os.path.join(task_dir, TASK_INFO_FILENAME), "r") as f:
            info = json.load(f)
        if info.get("source_states"):
            break
        time.sleep(0.01)
        
    assert info["status"] == "completed"
    assert info["progress"] == 1.0
    assert len(info["start_times"]) == 1
    assert len(info["finish_times"]) == 1
    assert info["pids"] == [9999]
    assert len(info.get("records", [])) == 1
    assert info["source_states"] == [source_state]

    # Check log file was written by _tee_output
    log_path = os.path.join(task_dir, "run_logs", "run1.log")
    assert os.path.exists(log_path)
    log_content = b""
    for _ in range(100):
        with open(log_path, "rb") as f:
            log_content = f.read()
        if source_state.encode("utf-8") in log_content:
            break
        time.sleep(0.01)
    assert b"hello output" in log_content
    assert source_state.encode("utf-8") in log_content

    # Check emit was called
    assert mock_emit.called


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_starts_process_before_source_state(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(
            {
                "name": "FastStartTask",
                "script": "script.py",
                "status": "queued",
                "start_times": [],
                "finish_times": [],
            },
            f,
        )

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.return_value = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"done\n", b""])
    mock_popen.return_value = mock_proc
    order = []
    source_started = threading.Event()

    def build_source_state(**kwargs):
        order.append("source")
        source_started.set()
        return "git late | clean | script late"

    def record_popen(*args, **kwargs):
        order.append("popen")
        return mock_proc

    mock_popen.side_effect = record_popen
    with patch("pyruns.core.executor._build_run_source_state", side_effect=build_source_state):
        res = run_task_worker(
            task_dir=task_dir,
            name="FastStartTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert res["status"] == "completed"
    assert source_started.wait(1)
    assert order[0] == "popen"
    assert "source" in order


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_posix_starts_child_in_new_session(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump({"name": "SessionTask", "script": "script.py", "status": "queued"}, f)

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.return_value = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"", b""])
    mock_popen.return_value = mock_proc

    with (
        patch("pyruns.core.executor._is_windows", return_value=False),
        patch("pyruns.core.executor._build_run_source_state", return_value=""),
    ):
        res = run_task_worker(
            task_dir=task_dir,
            name="SessionTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert res["status"] == "completed"
    assert mock_popen.call_args.kwargs["start_new_session"] is True


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_failure(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    
    task_info = {
        "name": "FailTask",
        "script": "script.py",
        "status": "queued",
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(task_info, f)
        
    mock_proc = MagicMock()
    mock_proc.pid = 8888
    mock_proc.wait.return_value = 1  # Failed exit code
    mock_proc.returncode = 1
    # stdout.read1 returns log content then EOF
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"Some log output", b''])
    mock_popen.return_value = mock_proc
    
    res = run_task_worker(
        task_dir=task_dir,
        name="FailTask",
        created_at="now",
        config={},
        run_index=1
    )
    
    assert res["status"] == "failed"
    assert res["progress"] == 0.0
    
    # Check task_info updated
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "r") as f:
        info = json.load(f)
    assert info["status"] == "failed"
    
    # Check failed run keeps run1.log and appends a failure summary to error.log
    run_log = os.path.join(task_dir, "run_logs", "run1.log")
    assert os.path.exists(run_log)
    with open(run_log, "r", encoding="utf-8", errors="replace") as f:
        assert "Some log output" in f.read()
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    assert os.path.exists(error_log)
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
        assert "Run #1 failed" in content
        assert "reason=exit_code 1" in content


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_separates_finish_banner_after_output_without_newline(
    mock_popen,
    mock_emit,
    mock_detect,
    tmp_path,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump({"name": "NoNewlineTask", "script": "script.py", "status": "queued"}, f)

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.wait.return_value = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"last output without newline", b""])
    mock_popen.return_value = mock_proc

    with patch("pyruns.core.executor._build_run_source_state", return_value=""):
        result = run_task_worker(
            task_dir=task_dir,
            name="NoNewlineTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert result["status"] == "completed"
    log_path = os.path.join(task_dir, RUN_LOGS_DIR, "run1.log")
    content = Path(log_path).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    assert "last output without newline\n[PYRUNS] ==================== FINISH" in content
    assert "last output without newline[PYRUNS]" not in content


def test_run_task_worker_internal_spawn_error_persists_failure_and_keeps_cleanup_error_secondary(tmp_path, monkeypatch):
    import pyruns.core.executor as executor
    from pyruns.utils.info_io import load_task_info

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "BrokenTask",
            "status": "queued",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "start_times": [],
            "finish_times": [],
            "pids": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {})
    cleanup_path = tmp_path / "wrapper.cmd"
    cleanup_path.write_text("@echo off\n", encoding="utf-8")
    bad_workdir = tmp_path / "missing-workdir"

    monkeypatch.setattr(
        executor,
        "_build_command",
        lambda *args, **kwargs: (["missing-command"], str(bad_workdir), [str(cleanup_path)]),
    )
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("spawn failed")))
    monkeypatch.setattr(executor.os, "remove", lambda path: (_ for _ in ()).throw(OSError("cleanup locked")))

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="BrokenTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["status"] == "failed"
    assert "spawn failed" in result["error"]
    info = load_task_info(str(task_dir))
    assert info["status"] == "failed"
    assert info["progress"] == 0.0
    assert info["finish_times"][0]
    error_log = task_dir / RUN_LOGS_DIR / ERROR_LOG_FILENAME
    assert "Internal error during run #1" in error_log.read_text(encoding="utf-8")
    assert cleanup_path.exists()


def test_task_manager_start_batch_tasks_uses_available_slots_immediately(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(5)]

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent, execution_mode=None):
        submitted.append((target["name"], run_index, independent))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=4)

    statuses = {task["name"]: task["status"] for task in manager.list_tasks()}
    assert sum(1 for status in statuses.values() if status == "running") == 4
    assert sum(1 for status in statuses.values() if status == "queued") == 1
    assert len(submitted) == 4

    for task in tasks[:4]:
        info = json.loads((Path(task["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
        assert info["status"] == "running"

    queued_info = json.loads((Path(tasks[4]["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert queued_info["status"] == "queued"


def test_task_manager_start_task_now_skips_active_task(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("runner", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        active = manager._tasks_by_name[task["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._running_ids.add(task["name"])
        manager._recompute_processing_flag_locked()

    submitted: list[str] = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent, execution_mode=None: submitted.append(target["name"]),
    )

    manager.start_task_now(task["name"])

    assert submitted == []
    refreshed = manager.get_task(task["name"])
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 1


def test_task_manager_start_batch_tasks_skips_active_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(3)]

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        active = manager._tasks_by_name[tasks[0]["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._running_ids.add(active["name"])
        manager._recompute_processing_flag_locked()

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent, execution_mode=None):
        submitted.append((target["name"], run_index, independent))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=2)

    assert [item[0] for item in submitted] == [tasks[1]["name"]]
    statuses = {task["name"]: task["status"] for task in manager.list_tasks()}
    assert statuses[tasks[0]["name"]] == "running"
    assert statuses[tasks[1]["name"]] == "running"
    assert statuses[tasks[2]["name"]] == "queued"
    assert manager.get_task(tasks[0]["name"])["run_index"] == 1


def test_task_manager_gpu_auto_queues_and_writes_queue_log_before_assignment(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_gpus_per_task: 1",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 6",
                "gpu_scheduler_max_wait_seconds: 86400",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-wait", {"lr": 0.1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))

    manager.start_batch_tasks([task["name"]], max_workers=1)

    assert submitted == []
    assert manager.get_task(task["name"])["status"] == "queued"
    queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
    text = queue_log.read_text(encoding="utf-8")
    assert "[PYRUNS] ================= GPU WAIT =================" in text
    assert "Run #1 waiting for GPU resources" in text
    assert "max wait=24h" in text


def test_task_manager_gpu_auto_assigns_cuda_env_when_queued_task_is_picked(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: multi",
                "gpu_scheduler_gpus_per_task: 2",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-run", {"lr": 0.1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider(
            [
                GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1),
                GpuDevice(1, "A800", "GPU-1", 4096, 40960, 2),
            ]
        ),
        clock=lambda: now[0],
    )
    manager.start_batch_tasks([task["name"]], max_workers=1)
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 1
    assert target["_scheduled_env"]["CUDA_VISIBLE_DEVICES"] == "0,1"
    assert target["_gpu_assignment"]["gpu_ids"] == [0, 1]
    queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
    text = queue_log.read_text(encoding="utf-8")
    assert "[PYRUNS] ================= GPU ASSIGNED =================" in text
    assert "CUDA_VISIBLE_DEVICES=0,1" in text


def test_task_manager_gpu_auto_independent_task_can_bypass_full_batch_slots(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 75",
                "gpu_scheduler_min_free_memory_gb: 8",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    batch_task = generator.create_task("batch-wait", {"lr": 0.1})
    run_now_task = generator.create_task("run-now", {"lr": 0.2})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    manager.max_workers = 1
    manager._running_ids.add("already-running")
    now = [300.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([
            GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0),
        ]),
        clock=lambda: now[0],
    )
    with manager._lock:
        manager._tasks_by_name[batch_task["name"]]["status"] = "queued"
        manager._tasks_by_name[batch_task["name"]]["run_index"] = 1
        manager._tasks_by_name[batch_task["name"]]["_gpu_wait_started_at"] = 290.0
        manager._tasks_by_name[run_now_task["name"]]["status"] = "queued"
        manager._tasks_by_name[run_now_task["name"]]["run_index"] = 1
        manager._tasks_by_name[run_now_task["name"]]["_gpu_wait_started_at"] = 290.0
        manager._tasks_by_name[run_now_task["name"]]["_queued_independent"] = True
        manager._recompute_processing_flag_locked()

    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    target, run_index = manager._pick_queued_task(independent_only=True)

    assert target is not None
    assert target["name"] == "run-now"
    assert run_index == 1
    assert "run-now" not in manager._running_ids
    assert manager.get_task(batch_task["name"])["status"] == "queued"


def test_task_manager_start_task_now_queues_gpu_task_as_independent(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "gpu_scheduler_enabled: true\ngpu_scheduler_stable_seconds: 1\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("run-now-gpu", {"lr": 0.1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))
    manager.start_task_now(task["name"], execution_mode="process")

    queued = manager.get_task(task["name"])
    assert submitted == []
    assert queued["status"] == "queued"
    assert queued["_queued_independent"] is True
    assert queued["_queued_execution_mode"] == "process"
    assert (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()


def test_task_manager_clears_stale_gpu_schedule_env_before_plain_rerun(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-stale-env", {"lr": 0.1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        target = manager._tasks_by_name[task["name"]]
        target["_scheduled_env"] = {"CUDA_VISIBLE_DEVICES": "7", "PYRUNS_ASSIGNED_GPUS": "7"}
        target["_gpu_assignment"] = {"gpu_ids": [7]}
        target["_gpu_wait_started_at"] = 1.0
        target["_gpu_last_wait_reason"] = "old"
        target["_queued_independent"] = True
        target["_queued_execution_mode"] = "process"

    submitted = []

    def fake_submit(target, run_index, *, independent, execution_mode=None):
        submitted.append(dict(target))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)
    manager.start_batch_tasks([task["name"]], max_workers=1)

    assert len(submitted) == 1
    assert "_scheduled_env" not in submitted[0]
    assert "_gpu_assignment" not in submitted[0]
    assert "_gpu_wait_started_at" not in submitted[0]
    assert "_gpu_last_wait_reason" not in submitted[0]
    assert "_queued_independent" not in submitted[0]
    assert "_queued_execution_mode" not in submitted[0]


def test_task_manager_plain_rerun_does_not_create_gpu_wait_state(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("plain-rerun", {"lr": 0.1})
    update_task_info(task["dir"], lambda info: info.update({"status": "completed", "run_index": 1}))

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    assert manager.rerun_task(task["name"]) is True

    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert "_gpu_wait_started_at" not in queued
    assert "_queued_independent" not in queued
    assert not (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()


def test_task_manager_on_task_done_clears_gpu_schedule_state(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-done", {"lr": 0.1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        target = manager._tasks_by_name[task["name"]]
        target["status"] = "running"
        target["_scheduled_env"] = {"CUDA_VISIBLE_DEVICES": "0"}
        target["_gpu_assignment"] = {"gpu_ids": [0]}
        target["_queued_independent"] = True
        manager._running_ids.add(task["name"])

    future = Future()
    future.set_result({"status": "completed"})
    manager._on_task_done(future, task["name"])

    refreshed = manager.get_task(task["name"])
    assert "_scheduled_env" not in refreshed
    assert "_gpu_assignment" not in refreshed
    assert "_queued_independent" not in refreshed
    assert task["name"] not in manager._running_ids


def test_task_manager_gpu_auto_respects_existing_cuda_visible_devices_in_task_env(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: multi",
                "gpu_scheduler_gpus_per_task: 2",
                "gpu_scheduler_memory_used_pct: 40",
                "gpu_scheduler_min_free_memory_gb: 40",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_respect_cuda_visible_devices: true",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-fixed", {"lr": 0.1})
    update_task_info(str(Path(task["dir"])), lambda info: info.update({"env": {"CUDA_VISIBLE_DEVICES": "1,2"}}))

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    now = [200.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider(
            [
                GpuDevice(0, "A800", "GPU-0", 1024, 81920, 1),
                GpuDevice(1, "A800", "GPU-1", 1024, 81920, 1),
                GpuDevice(2, "A800", "GPU-2", 1024, 81920, 1),
            ]
        ),
        clock=lambda: now[0],
    )
    manager.start_batch_tasks([task["name"]], max_workers=1)
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, _ = manager._pick_queued_task()

    assert target is not None
    assert target["_gpu_assignment"]["gpu_ids"] == [1, 2]
    assert target["_scheduled_env"] == {"PYRUNS_ASSIGNED_GPUS": "1,2"}


def test_task_manager_gpu_auto_times_out_waiting_tasks_and_writes_logs(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_task_mode: single",
                "gpu_scheduler_memory_used_pct: 40",
                "gpu_scheduler_min_free_memory_gb: 40",
                "gpu_scheduler_compute_used_pct: 30",
                "gpu_scheduler_stable_seconds: 15",
                "gpu_scheduler_max_wait_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-timeout", {"lr": 0.1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    manager.start_batch_tasks([task["name"]], max_workers=1)
    with manager._lock:
        manager._tasks_by_name[task["name"]]["_gpu_wait_started_at"] = time.monotonic() - 10

    target, _ = manager._pick_queued_task()

    assert target is None
    assert manager.get_task(task["name"])["status"] == "failed"
    log_dir = Path(task["dir"]) / RUN_LOGS_DIR
    queue_text = (log_dir / "queue.log").read_text(encoding="utf-8")
    error_text = (log_dir / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "[PYRUNS] ================= GPU WAIT TIMEOUT =================" in queue_text
    assert "max wait=1s" in queue_text
    assert "reason=gpu_wait_timeout" in error_text


def test_run_task_worker_records_gpu_assignment_in_run_log(tmp_path):
    task_dir = tmp_path / "tasks" / "gpu-task"
    task_dir.mkdir(parents=True)
    save_task_info(
        str(task_dir),
        {
            "name": "gpu-task",
            "status": "pending",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "cmd": [
                os.path.abspath(sys.executable),
                "-c",
                "import os; print('visible=' + os.environ.get('CUDA_VISIBLE_DEVICES', ''))",
            ],
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.1})

    result = run_task_worker(
        str(task_dir),
        "gpu-task",
        "2026-03-20_00-00-00",
        {"lr": 0.1},
        {"CUDA_VISIBLE_DEVICES": "0,1", "PYRUNS_ASSIGNED_GPUS": "0,1"},
        run_index=1,
    )

    assert result["status"] == "completed"
    run_log = task_dir / RUN_LOGS_DIR / "run1.log"
    text = run_log.read_text(encoding="utf-8")
    assert "[PYRUNS] GPU assignment: 0,1" in text
    assert "[PYRUNS] CUDA_VISIBLE_DEVICES=0,1" in text
    assert "visible=0,1" in text


def test_run_task_worker_marks_cuda_oom_failures_in_error_log(tmp_path):
    task_dir = tmp_path / "tasks" / "oom-task"
    task_dir.mkdir(parents=True)
    save_task_info(
        str(task_dir),
        {
            "name": "oom-task",
            "status": "pending",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "cmd": [
                os.path.abspath(sys.executable),
                "-c",
                "import sys; print('torch.cuda.OutOfMemoryError: CUDA out of memory'); sys.exit(1)",
            ],
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.1})

    result = run_task_worker(
        str(task_dir),
        "oom-task",
        "2026-03-20_00-00-00",
        {"lr": 0.1},
        {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
        run_index=1,
    )

    assert result["status"] == "failed"
    error_log = task_dir / RUN_LOGS_DIR / ERROR_LOG_FILENAME
    text = error_log.read_text(encoding="utf-8")
    assert "reason=cuda_out_of_memory" in text
    assert "assigned_gpus=0" in text
    assert "cuda_visible_devices=0" in text


def test_task_manager_cancel_task_writes_cancel_reason(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    monkeypatch.setattr(manager, "_latest_pid_from_disk", lambda task: 12345)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: None)

    assert manager.cancel_task("runner") is True

    info = json.loads((task_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["status"] == "failed"
    assert info["_pending_stop_summary"]["reason"] == "cancelled_by_user"
    assert info["_pending_stop_summary"]["detail_lines"] == ["previous_status=running"]


def test_task_manager_cancel_task_tolerates_busy_task_info(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    monkeypatch.setattr(manager, "_latest_pid_from_disk", lambda task: 12345)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: None)

    with patch("pyruns.core.task_manager.update_task_info", side_effect=TimeoutError("busy")):
        assert manager.cancel_task("runner") is True

    assert manager.get_task("runner")["status"] == "failed"


def test_task_manager_cancel_task_uses_short_task_info_lock(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: None)
    timeout_values = []

    def record_timeout(task_dir, updater, **kwargs):
        timeout_values.append(kwargs.get("timeout_sec"))
        raise TimeoutError("busy")

    with patch("pyruns.core.task_manager.update_task_info", side_effect=record_timeout):
        assert manager.cancel_task("runner") is True

    assert timeout_values == [task_manager_module._STOP_TASK_INFO_LOCK_TIMEOUT_SEC]


def test_task_manager_shutdown_cleanup_kills_only_running_task_latest_pid(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    running_dir = tasks_dir / "runner"
    queued_dir = tasks_dir / "queued"
    running_dir.mkdir()
    queued_dir.mkdir()

    save_task_info(
        str(running_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [111, 222],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(running_dir / CONFIG_FILENAME), {"lr": 0.01})
    save_task_info(
        str(queued_dir),
        {
            "name": "queued",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [333],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(queued_dir / CONFIG_FILENAME), {"lr": 0.02})

    killed: list[int] = []
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: killed.append(pid))
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    manager._cleanup_on_shutdown()

    assert killed == [222]
    running_info = json.loads((running_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    queued_info = json.loads((queued_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert running_info["status"] == "failed"
    assert queued_info["status"] == "failed"


def test_task_manager_shutdown_cleanup_ignores_malformed_in_memory_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: None)
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    manager.tasks = [{}, {"name": "missing-status"}, None]

    manager._cleanup_on_shutdown()

    assert manager.tasks == [{}, {"name": "missing-status"}, None]


def test_task_manager_observers_serialization_and_missing_root_scan(tmp_path):
    missing_tasks_dir = tmp_path / "missing"
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(missing_tasks_dir), lazy_scan=False)

    assert manager.list_tasks() == []
    assert TaskManager.serialize_task(None) is None

    calls = []

    def good_callback():
        calls.append("good")

    def bad_callback():
        calls.append("bad")
        raise RuntimeError("observer failed")

    manager.on_change(good_callback)
    manager.on_change(good_callback)
    manager.on_change(bad_callback)
    manager.trigger_update()
    manager.off_change(bad_callback)
    manager.trigger_update()

    assert calls == ["good", "bad", "good"]
    summary = TaskManager.serialize_task(
        {
            "dir": r"C:\tmp\task",
            "name": "alpha",
            "status": "running",
            "env": {"A": "1"},
            "source_states": ["git abc | clean | script abc"],
            "records": [{"loss": 0.1}],
            "tracks": [{"step": 1}],
        },
        summary=True,
    )
    assert summary["dir"] == "C:/tmp/task"
    assert summary["source_states"] == ["git abc | clean | script abc"]
    assert summary["records"] == []
    assert summary["env"] == {"A": "1"}


def test_task_manager_scan_and_load_task_dir_edge_cases(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    missing_info_dir = tasks_dir / "missing-info"
    missing_info_dir.mkdir()

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    assert manager._load_task_dir("missing-info") is None

    empty_dir = tasks_dir / "empty-info"
    empty_dir.mkdir()
    (empty_dir / TASK_INFO_FILENAME).write_text("{}", encoding="utf-8")
    with patch("pyruns.core.task_manager.load_task_info", return_value={}):
        assert manager._load_task_dir("empty-info") is None

    with patch("pyruns.core.task_manager.load_task_info", side_effect=RuntimeError("bad info")):
        assert manager._load_task_dir("empty-info") is None

    statless_dir = tasks_dir / "statless"
    statless_dir.mkdir()
    save_task_info(
        str(statless_dir),
        {
            "name": "statless",
            "status": "pending",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 2,
        },
    )
    save_yaml(str(statless_dir / CONFIG_FILENAME), {"lr": 0.01})

    original_exists = os.path.exists
    original_stat = os.stat
    info_path = str(statless_dir / TASK_INFO_FILENAME)

    def fake_exists(path):
        if str(path) == info_path:
            return True
        return original_exists(path)

    def fake_stat(path, *args, **kwargs):
        if str(path).endswith(TASK_INFO_FILENAME):
            raise OSError("no stat")
        return original_stat(path, *args, **kwargs)

    with (
        patch("pyruns.core.task_manager.os.path.exists", side_effect=fake_exists),
        patch("pyruns.core.task_manager.os.stat", side_effect=fake_stat),
    ):
        loaded = manager._load_task_dir("statless")
    assert loaded["_mtime_ns"] == 0
    assert loaded["run_index"] == 2

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("scandir failed")):
        manager.scan_disk()
    assert manager.list_tasks() == []


def test_task_manager_refresh_discovers_external_added_and_removed_tasks(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    generator.create_task("beta", {"value": 2})
    shutil.rmtree(alpha["dir"])

    assert manager.refresh_from_disk(check_all=True, discover=True) is True

    tasks = {task["name"]: task for task in manager.list_tasks()}
    assert set(tasks) == {"beta"}
    assert tasks["beta"]["config"]["value"] == 2


def test_task_manager_add_tasks_upserts_existing_name(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        manager._tasks_by_name["alpha"]["script"] = "train.py"

    duplicate = dict(alpha)
    duplicate["notes"] = "created through api"

    manager.add_tasks([duplicate])

    tasks = [task for task in manager.list_tasks() if task["name"] == "alpha"]
    assert len(tasks) == 1
    assert tasks[0]["notes"] == "created through api"
    assert tasks[0]["script"] == "train.py"


def test_task_manager_refresh_keeps_discovered_tasks_in_disk_order(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    newest = generator.create_task("newest", {"value": 3})
    base = time.time()
    os.utime(newest["dir"], (base + 30, base + 30))

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    older = generator.create_task("older", {"value": 1})
    middle = generator.create_task("middle", {"value": 2})
    os.utime(older["dir"], (base + 10, base + 10))
    os.utime(middle["dir"], (base + 20, base + 20))

    assert manager.refresh_from_disk(check_all=True, discover=True) is True

    assert [task["name"] for task in manager.list_tasks()] == ["newest", "middle", "older"]


def test_task_manager_refresh_keeps_tasks_when_directory_scan_fails(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("stale nfs handle")):
        assert manager.refresh_from_disk(check_all=True, discover=True) is False

    assert [task["name"] for task in manager.list_tasks()] == ["alpha"]


def test_task_manager_pin_reorder_notes_env_and_rename_edges(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    beta = generator.create_task("beta", {"value": 2})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    assert manager.set_task_pinned("missing") == (False, "Task not found")
    ok, pinned = manager.set_task_pinned("alpha")
    assert ok is True and pinned is True

    assert manager.reorder_tasks([])[1] == "No valid tasks were provided for reordering."
    assert manager.reorder_tasks([{"name": "alpha"}, {"name": "alpha"}])[1].startswith("Duplicate task")
    assert manager.reorder_tasks([{"name": "missing"}])[1] == "Task not found: missing"
    ok, reordered = manager.reorder_tasks([{"name": "beta", "pinned": True}, {"name": "alpha", "pinned": False}])
    assert ok is True
    assert [item["name"] for item in reordered] == ["beta", "alpha"]
    assert manager.get_task("beta")["pinned"] is True

    assert manager.update_task_notes("missing", "x") == (False, "Task not found")
    assert manager.update_task_notes("alpha", "note") == (True, "note")
    assert manager.update_task_env("missing", {}) == (False, "Task not found")
    assert manager.update_task_env("alpha", {"A": 1, "": "skip"}) == (True, {"A": "1"})

    assert manager.rename_task("alpha", "") == (False, "Task name cannot be empty")
    assert manager.rename_task("missing", "new") == (False, "Task not found")
    with manager._lock:
        manager._tasks_by_name["alpha"]["status"] = "queued"
    assert manager.rename_task("alpha", "alpha-new") == (False, "Running or queued tasks cannot be renamed")
    with manager._lock:
        manager._tasks_by_name["alpha"]["status"] = "pending"
    assert manager.rename_task("alpha", "alpha") == (True, "alpha")
    assert "invalid" in manager.rename_task("alpha", "bad/name")[1]
    assert "already exists" in manager.rename_task("alpha", "beta")[1]

    with patch("pyruns.core.task_manager.os.rename", lambda old, new: (_ for _ in ()).throw(OSError("rename failed"))):
        assert manager.rename_task("alpha", "gamma") == (False, "rename failed")

    with patch("pyruns.core.task_manager.update_task_info", side_effect=RuntimeError("write failed")):
        ok, message = manager.rename_task("alpha", "gamma")
    assert ok is False
    assert "write failed" in message
    assert Path(alpha["dir"]).exists()
    assert Path(beta["dir"]).exists()


def test_task_manager_delete_active_task_preserves_folder_when_trash_move_fails(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "runner"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "runner",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    trash_conflict = tasks_dir / TRASH_DIR / "runner"
    trash_conflict.mkdir(parents=True)

    killed = []
    removed = []
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: killed.append(pid))
    monkeypatch.setattr("pyruns.core.task_manager.get_now_str", lambda: "2026-03-20_00-00-02")
    monkeypatch.setattr("pyruns.core.task_manager.shutil.move", lambda src, dst: (_ for _ in ()).throw(OSError("move failed")))
    monkeypatch.setattr("pyruns.core.task_manager.shutil.rmtree", lambda path: removed.append(path))
    monkeypatch.setattr("pyruns.core.task_manager.time.sleep", lambda delay: None)

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    manager.delete_tasks(["missing"])
    assert manager.get_task("runner") is not None

    deleted = manager.delete_tasks(["runner", "runner"])

    assert killed == [12345]
    assert deleted == []
    assert removed == []
    assert task_dir.exists()
    assert manager.get_task("runner")["status"] == "failed"


def test_task_manager_keeps_live_foreign_runner_running(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "remote"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "remote",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: False)

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    assert manager.get_task("remote")["status"] == "running"


def test_task_manager_does_not_submit_when_foreign_runner_owns_lease(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("alpha", {"value": 1})
    save_task_info(
        task["dir"],
        {
            "name": "alpha",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 2,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [4321],
            "runner_id": "other-host:4321:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
        },
    )

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    submitted = []

    class CapturingExecutor:
        def submit(self, *args, **kwargs):
            submitted.append((args, kwargs))

    manager._executor = CapturingExecutor()
    monkeypatch.setattr(manager, "_ensure_executor", lambda: None)
    with manager._lock:
        target = manager._tasks_by_name["alpha"]
        target["status"] = "queued"
        manager._running_ids.add("alpha")

    manager._submit_task(target, 3, independent=False)

    assert submitted == []
    assert manager.get_task("alpha")["status"] == "running"


def test_task_manager_start_batch_sync_conflict_keeps_foreign_runner_without_submit(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 4,
            "runner_id": "other-host:4321:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [4321],
        }),
    )
    submitted = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent, execution_mode=None: submitted.append(target["name"]),
    )

    manager.start_batch_tasks(["alpha"], max_workers=1)

    refreshed = manager.get_task("alpha")
    assert submitted == []
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 4
    assert refreshed["runner_id"] == "other-host:4321:abcdef"
    assert "alpha" not in manager._running_ids


def test_task_manager_gpu_queue_sync_conflict_skips_wait_log_and_clears_transient_state(tmp_path, monkeypatch):
    settings_root = tmp_path
    tasks_dir = settings_root / "tasks"
    tasks_dir.mkdir()
    (settings_root / "_pyruns_settings.yaml").write_text(
        "gpu_scheduler_enabled: true\ngpu_scheduler_stable_seconds: 1\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-race", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 3,
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [123],
        }),
    )
    logged = []
    monkeypatch.setattr(manager, "_append_gpu_wait_started", lambda *args, **kwargs: logged.append(args))

    manager.start_batch_tasks(["gpu-race"], max_workers=1)

    refreshed = manager.get_task("gpu-race")
    assert logged == []
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 3
    assert refreshed["runner_id"] == "other-host:123:abcdef"
    assert not (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()
    with manager._lock:
        current = manager._tasks_by_name["gpu-race"]
        assert "_gpu_wait_started_at" not in current
        assert "_queued_independent" not in current
    assert "gpu-race" not in manager._running_ids


def test_task_manager_rerun_returns_false_when_queue_sync_conflicts_with_foreign_runner(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "completed", "run_index": 1}))

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 2,
            "runner_id": "other-host:987:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [987],
        }),
    )

    assert manager.rerun_task("alpha") is False
    refreshed = manager.get_task("alpha")
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 2
    assert refreshed["runner_id"] == "other-host:987:abcdef"


def test_task_manager_internal_executor_and_worker_error_paths(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        target = manager._tasks_by_name["alpha"]
        target["status"] = "queued"
        target["run_index"] = 3
        manager._recompute_processing_flag_locked()

    picked, run_index = manager._pick_queued_task()
    assert picked["name"] == "alpha"
    assert run_index == 3
    assert manager.get_task("alpha")["status"] == "running"

    class FailingExecutor:
        def submit(self, *args, **kwargs):
            raise RuntimeError("submit failed")

    manager._executor = FailingExecutor()
    monkeypatch.setattr(manager, "_ensure_executor", lambda: None)
    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda task, **kwargs: task.update(marked_failed=kwargs))
    manager._submit_task(picked, 3, independent=False)
    assert picked["status"] == "failed"
    assert picked["marked_failed"]["reason"] == "submission_error"

    with manager._lock:
        picked["status"] = "running"
        manager._running_ids.add("alpha")

    failed_marks = []
    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda task, **kwargs: failed_marks.append(kwargs))
    future = Future()
    future.set_exception(RuntimeError("worker failed"))
    manager._on_task_done(future, "alpha")

    assert failed_marks[0]["reason"] == "worker_exception"
    assert manager.get_task("alpha")["status"] == "failed"


def test_task_manager_scheduler_helpers_and_cleanup_edges(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    queued = generator.create_task("queued", {"value": 1})
    running = generator.create_task("running", {"value": 2})
    remote = generator.create_task("remote", {"value": 3})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    with manager._lock:
        manager._tasks_by_name["queued"]["status"] = "queued"
        manager._tasks_by_name["queued"]["run_index"] = 2
        manager._tasks_by_name["running"]["status"] = "running"
        manager._tasks_by_name["running"]["run_index"] = 1
        manager._running_ids.add("running")
        manager._tasks_by_name["remote"]["status"] = "running"
        manager._tasks_by_name["remote"]["run_index"] = 1
        manager._running_ids.add("remote")
        manager._recompute_processing_flag_locked()

    picked, run_index = manager._pick_queued_task()
    assert picked["name"] == "queued"
    assert run_index == 2
    assert "queued" in manager._running_ids

    class ExistingExecutor:
        def __init__(self):
            self.shutdown_calls = []

        def shutdown(self, **kwargs):
            self.shutdown_calls.append(kwargs)

    old_executor = ExistingExecutor()
    manager._executor = old_executor
    manager._executor_mode = "thread"
    manager._executor_workers = 1
    manager.execution_mode = "thread"
    manager.max_workers = 1
    manager._ensure_executor()
    assert manager._executor is old_executor

    manager.max_workers = 2
    manager._ensure_executor()
    assert old_executor.shutdown_calls == [{"wait": False}]
    assert manager._executor is not old_executor

    foreign_info = {
        "runner_id": "remote-runner",
        "runner_host": "other",
        "lease_until": time.time() + 60,
    }
    local_info = {
        "runner_id": manager.runner_id,
        "runner_host": manager.runner_host,
        "lease_until": time.time() + 60,
    }
    monkeypatch.setattr("pyruns.core.task_manager.load_task_info", lambda task_dir: foreign_info if str(task_dir).endswith("remote") else local_info)
    killed = []
    monkeypatch.setattr("pyruns.core.task_manager.kill_process", lambda pid: killed.append(pid))
    monkeypatch.setattr(manager, "_latest_pid", lambda info: 4321)
    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda task, **kwargs: task.update(cleaned=kwargs))

    manager._cleanup_on_shutdown()
    manager._cleanup_on_shutdown()

    assert killed == [4321, 4321]
    assert manager.get_task("running")["status"] == "failed"
    assert manager.get_task("remote")["status"] == "running"
    assert manager._shutdown_cleanup_done is True


def test_task_manager_scan_async_and_disk_discovery_edge_paths(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("keep", {"value": 1})
    stale = generator.create_task("stale", {"value": 2})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    callbacks = []
    callback = lambda: callbacks.append("changed")
    manager.on_change(callback)
    manager.off_change(lambda: None)
    manager.scan_disk_async()
    deadline = time.time() + 2.0
    while not callbacks and time.time() < deadline:
        time.sleep(0.01)
    assert callbacks

    assert manager.load_task_by_name("../bad") is None
    assert "keep" in manager._list_task_dir_names()

    shutil.rmtree(stale["dir"])
    for index in range(9):
        generator.create_task(f"new-{index}", {"value": index})

    changed = manager.sync_task_dirs_from_disk()
    assert changed is True
    assert manager.get_task("stale") is None
    assert manager.get_task("new-0") is not None

    def fail_scandir(_path):
        raise OSError("cannot scan")

    monkeypatch.setattr(task_manager_module.os, "scandir", fail_scandir)
    assert manager._scan_task_dir_names() == (False, [])
    assert manager.sync_task_dirs_from_disk() is False

    missing_root = tmp_path / "missing-root"
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        missing_manager = TaskManager(tasks_dir=str(missing_root), lazy_scan=False)
    assert missing_manager.list_tasks() == []

    monkeypatch.undo()
    shutil.rmtree(tasks_dir)
    assert manager.sync_task_dirs_from_disk() is True
    assert manager.list_tasks() == []


def test_task_manager_default_root_serialization_and_lease_edges(tmp_path, monkeypatch):
    custom_root = tmp_path / "run-root"
    tasks_dir = custom_root / TASKS_DIR
    tasks_dir.mkdir(parents=True)

    monkeypatch.setattr("pyruns._config.ROOT_DIR", str(custom_root))
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=None, lazy_scan=None)

    assert manager.tasks_dir == str(tasks_dir)
    assert manager._disk_scan_complete is False
    assert manager.list_tasks() == []

    assert TaskManager.serialize_task(None) is None
    summary = TaskManager.serialize_task(
        {
            "dir": "C:\\workspace\\tasks\\alpha",
            "name": "alpha",
            "status": "completed",
            "config": {"lr": 0.1},
            "env": {"A": "1"},
            "start_times": ("s1",),
            "finish_times": ("f1",),
            "pids": (123,),
            "source_states": ("git clean",),
            "records": [{"loss": 1}],
            "tracks": [{"name": "loss"}],
        },
        summary=True,
    )
    assert summary["dir"] == "C:/workspace/tasks/alpha"
    assert summary["config"] == {}
    assert summary["records"] == []
    assert summary["tracks"] == []
    assert summary["env"] == {"A": "1"}

    assert TaskManager._lease_until_value({"lease_until": "bad"}) == 0.0


def test_task_manager_logs_and_gpu_helper_branches(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "task"
    task_dir.mkdir()
    task = {"name": "task", "dir": str(task_dir), "run_index": 1}

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    assert manager._format_duration(3660) == "1.0h"
    assert manager._format_duration(120) == "2m"
    assert manager._format_duration(90) == "1.5m"
    assert manager._format_elapsed(3661) == "01:01:01"

    config = GpuSchedulerConfig(enabled=True, task_mode="multi", gpus_per_task=2, device_ids=[1, 3], max_wait_seconds=3600)
    assert manager._gpu_need_label(config) == "2 GPUs"
    assert manager._gpu_pool_label(config) == "1,3"

    assignment = GpuAssignment(
        task_name="task",
        run_index=1,
        gpu_ids=[1, 3],
        cuda_visible_devices="1,3",
        env={"CUDA_VISIBLE_DEVICES": "1,3"},
        waited_seconds=61,
    )
    assert manager._gpu_assignment_to_dict(assignment)["gpu_ids"] == [1, 3]

    manager._append_gpu_wait_started(task, 1, config)
    manager._append_gpu_assignment(task, assignment)
    manager._append_gpu_assignment(
        task,
        GpuAssignment(
            task_name="task",
            run_index=2,
            gpu_ids=[],
            cuda_visible_devices="GPU-uuid-0,MIG-GPU-uuid/0/1",
            env={"PYRUNS_ASSIGNED_GPUS": "GPU-uuid-0,MIG-GPU-uuid/0/1"},
            waited_seconds=0,
        ),
    )
    manager._append_gpu_wait_decision(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=10,
        now=100,
    )
    repeated = manager._gpu_wait_decision_lines(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=11,
        now=101,
    )
    assert repeated is None
    periodic = manager._gpu_wait_decision_lines(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=61,
        now=161,
    )
    assert periodic is not None
    assert "still waiting after 00:01:01" in periodic[0]

    queue_text = (task_dir / RUN_LOGS_DIR / "queue.log").read_text(encoding="utf-8")
    assert "GPU WAIT" in queue_text
    assert "GPU ASSIGNED" in queue_text
    assert "CUDA_VISIBLE_DEVICES=GPU-uuid-0,MIG-GPU-uuid/0/1" in queue_text

    class FakeGpu:
        def __init__(self, index, memory_used_pct, compute_util_pct, free_memory_gb):
            self.index = index
            self.memory_used_pct = memory_used_pct
            self.compute_util_pct = compute_util_pct
            self.free_memory_gb = free_memory_gb

    assert manager._gpu_snapshot_lines([], config) == ["GPU snapshot: no NVIDIA GPU metrics available"]
    assert manager._gpu_snapshot_lines([FakeGpu(4, 90, 50, 1)], config) == ["GPU snapshot: configured GPU pool is empty"]
    visible_lines = manager._gpu_snapshot_lines(
        [FakeGpu(1, 10, 5, 80), FakeGpu(3, 90, 50, 1)],
        config,
    )
    assert "GPU 1 eligible" in visible_lines[0]
    assert "GPU 3 blocked" in visible_lines[1]

    monkeypatch.setattr("builtins.open", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("no write")))
    manager._append_gpu_queue_log(task, "GPU WAIT", ["cannot write"])
    manager._append_error_summary(str(task_dir), title="error", detail_lines=["detail"])


def test_task_manager_gpu_wait_log_interval_uses_stable_window(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    task = {"name": "task", "dir": str(tasks_dir / "task"), "run_index": 1}
    config = GpuSchedulerConfig(enabled=True, stable_seconds=15, sample_interval_seconds=2)
    decision = GpuDecision(assignment=None, reason="busy", snapshot=[])

    assert manager._gpu_wait_log_interval(config) == 15

    first = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=0, now=100)
    assert first is not None
    assert manager._gpu_wait_decision_lines(task, 1, config, decision, waited=14, now=114) is None
    second = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=15, now=115)
    assert second is not None
    assert "still waiting after 00:00:15" in second[0]


def test_task_manager_gpu_wait_log_interval_respects_sample_interval_floor(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    task = {"name": "task", "dir": str(tasks_dir / "task"), "run_index": 1}
    config = GpuSchedulerConfig(enabled=True, stable_seconds=1, sample_interval_seconds=2)
    decision = GpuDecision(assignment=None, reason="busy", snapshot=[])

    assert manager._gpu_wait_log_interval(config) == 2

    first = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=0, now=100)
    assert first is not None
    assert manager._gpu_wait_decision_lines(task, 1, config, decision, waited=1, now=101) is None
    second = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=2, now=102)
    assert second is not None
    assert "still waiting after 00:00:02" in second[0]


def test_executor_gpu_log_helpers_and_bounded_tail_read(tmp_path, monkeypatch):
    log_path = tmp_path / "run.log"
    log_path.write_text("abc", encoding="utf-8")

    payload = _append_run_log_text(str(log_path), "tail\n", clean_boundary=True)
    assert payload.startswith("\n")
    assert log_path.read_text(encoding="utf-8") == "abc\ntail\n"

    tail_text = _read_log_tail_text(str(log_path), max_bytes=4).replace("\r\n", "\n")
    assert "abc\ntail\n".endswith(tail_text)
    assert tail_text.endswith("il\n")
    assert _read_log_tail_text(str(tmp_path / "missing.log")) == ""

    assert _gpu_assignment_log({}) == ""
    assert "[PYRUNS] GPU assignment: 2" in _gpu_assignment_log({"PYRUNS_ASSIGNED_GPUS": "2"})
    cuda_log = _gpu_assignment_log({"CUDA_VISIBLE_DEVICES": "4"})
    assert "GPU assignment: 4" in cuda_log
    assert "CUDA_VISIBLE_DEVICES=4" in cuda_log
    assert _gpu_failure_detail_lines({}) == []
    assert _gpu_failure_detail_lines({
        "PYRUNS_ASSIGNED_GPUS": "0,1",
        "CUDA_VISIBLE_DEVICES": "0,1",
    }) == ["assigned_gpus=0,1", "cuda_visible_devices=0,1"]

    monkeypatch.setattr(executor.os.path, "getsize", lambda _path: (_ for _ in ()).throw(OSError("stat failed")))
    noisy_log = tmp_path / "noisy.log"
    noisy_log.write_text("ready\n", encoding="utf-8")
    assert _append_run_log_text(str(noisy_log), "next\n", clean_boundary=True) == "next\n"


def test_launcher_path_helpers_and_native_picker_fallbacks(tmp_path, monkeypatch):
    import pyruns.launcher as launcher

    script_path = tmp_path / "_shell_.py"
    script_path.write_text("print('shell named script')\n", encoding="utf-8")
    not_script = tmp_path / "not_script.txt"
    not_script.write_text("", encoding="utf-8")

    assert launcher.normalize_path(str(script_path)).endswith("_shell_.py")
    assert launcher.validate_python_script_path(str(script_path)).endswith("_shell_.py")
    with pytest.raises(FileNotFoundError):
        launcher.validate_python_script_path(str(not_script))
    assert launcher.workspace_name_for_script_base(SHELL_WORKSPACE_NAME) == f"py{SHELL_WORKSPACE_NAME}"
    assert launcher.workspace_root_for_script(str(script_path)).endswith(f"{DEFAULT_ROOT_NAME}/py{SHELL_WORKSPACE_NAME}")
    assert launcher.shell_workspace_root_for_run_root(str(tmp_path / DEFAULT_ROOT_NAME)).endswith(f"{DEFAULT_ROOT_NAME}/{SHELL_WORKSPACE_NAME}")
    assert launcher.shell_workspace_root_for_run_root(str(tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME)).endswith(SHELL_WORKSPACE_NAME)
    assert launcher.shell_project_root_for_workspace(str(tmp_path / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME)) == str(tmp_path).replace("\\", "/")

    monkeypatch.setattr(launcher.os, "name", "posix")
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    assert launcher.native_picker_available() is False
    monkeypatch.setenv("DISPLAY", ":1")
    assert launcher.native_picker_available() is True

    original_import = __import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "tkinter" or name.startswith("tkinter"):
            raise ImportError("tk unavailable")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr("builtins.__import__", fake_import)
    assert launcher.choose_script_file(str(tmp_path)) is None
    assert launcher.choose_config_file(str(tmp_path)) is None
    assert launcher.choose_shell_file(str(tmp_path)) is None
    assert launcher.choose_directory(str(tmp_path)) is None


def test_launcher_native_picker_success_paths_normalize_selection(tmp_path, monkeypatch):
    import types
    import pyruns.launcher as launcher

    script = tmp_path / "train.py"
    config = tmp_path / "config.yaml"
    shell = tmp_path / "run.sh"
    directory = tmp_path / "workspace"
    for path in (script, config, shell):
        path.write_text("", encoding="utf-8")
    directory.mkdir()

    selected_files = iter([str(script), str(config), str(shell)])
    roots = []

    class FakeRoot:
        def __init__(self):
            self.withdrawn = False
            self.destroyed = False
            self.attributes_calls = []

        def withdraw(self):
            self.withdrawn = True

        def attributes(self, *args):
            self.attributes_calls.append(args)

        def destroy(self):
            self.destroyed = True

    def make_root():
        root = FakeRoot()
        roots.append(root)
        return root

    filedialog = types.SimpleNamespace(
        askopenfilename=lambda **kwargs: next(selected_files),
        askdirectory=lambda **kwargs: str(directory),
    )
    tkinter = types.SimpleNamespace(Tk=make_root, filedialog=filedialog)
    monkeypatch.setitem(sys.modules, "tkinter", tkinter)
    monkeypatch.setitem(sys.modules, "tkinter.filedialog", filedialog)

    assert launcher.choose_script_file(str(tmp_path)) == str(script).replace("\\", "/")
    assert launcher.choose_config_file(str(tmp_path)) == str(config).replace("\\", "/")
    assert launcher.choose_shell_file(str(tmp_path)) == str(shell).replace("\\", "/")
    assert launcher.choose_directory(str(tmp_path)) == str(directory).replace("\\", "/")
    assert len(roots) == 4
    assert all(root.withdrawn and root.destroyed for root in roots)
    assert all(root.attributes_calls == [("-topmost", True)] for root in roots)


def test_launcher_discovers_workspace_and_file_candidates(tmp_path):
    import pyruns.launcher as launcher

    project = tmp_path / "project"
    project.mkdir()
    script = project / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    file_only = project / "eval.py"
    file_only.write_text("print('eval')\n", encoding="utf-8")
    workspace_root = project / DEFAULT_ROOT_NAME
    workspace = workspace_root / "train"
    workspace.mkdir(parents=True)
    (workspace / SCRIPT_INFO_FILENAME).write_text(
        json.dumps(
            {
                "workspace_kind": "script",
                "script_name": "train",
                "script_path": str(script),
            }
        ),
        encoding="utf-8",
    )
    shell_workspace = workspace_root / SHELL_WORKSPACE_NAME
    shell_workspace.mkdir()
    (shell_workspace / SCRIPT_INFO_FILENAME).write_text(json.dumps({"workspace_kind": WORKSPACE_KIND_SHELL}), encoding="utf-8")
    bad_workspace = workspace_root / "bad"
    bad_workspace.mkdir()
    (bad_workspace / SCRIPT_INFO_FILENAME).write_text("{bad json", encoding="utf-8")

    assert launcher.resolve_workspace_for_script(str(script)) == str(workspace).replace("\\", "/")
    candidates = launcher.list_script_candidates(str(project))
    by_name = {item["script_name"]: item for item in candidates}

    assert by_name["train"]["source"] == "workspace+file"
    assert by_name["eval"]["source"] == "file"
    assert SHELL_WORKSPACE_NAME not in by_name

    summary = launcher.read_workspace_summary(str(workspace))
    assert summary["script_name"] == "train"
    assert summary["workspace_kind"] == "script"


def test_launcher_config_candidates_bootstrap_errors_and_query(tmp_path, monkeypatch, capsys):
    import pyruns.launcher as launcher

    script = tmp_path / "train.py"
    script.write_text("import pyruns\ncfg = pyruns.load()\n", encoding="utf-8")
    config = tmp_path / "configs" / "base.yaml"
    config.parent.mkdir()
    config.write_text("lr: 0.1\n", encoding="utf-8")
    root_default = Path(launcher.workspace_root_for_script(str(script))) / CONFIG_DEFAULT_FILENAME
    root_default.parent.mkdir(parents=True)
    root_default.write_text("lr: 0.2\n", encoding="utf-8")

    candidates = launcher.list_config_candidates(str(script))
    labels = [item["label"] for item in candidates]
    assert labels[0] == "Workspace default"
    assert "configs/base.yaml" in labels

    monkeypatch.setattr("pyruns.launcher.detect_config_source_fast", lambda path: ("pyruns_load", None))
    metadata = launcher.get_config_selection_metadata(str(script))
    assert metadata["requires_config_template"] is False

    root_default.unlink()
    metadata = launcher.get_config_selection_metadata(str(script))
    assert metadata["requires_config_template"] is True
    assert launcher.list_workspace_candidates(str(script), str(config))[0]["config_name"] == "base.yaml"

    with pytest.raises(FileNotFoundError, match="Custom config"):
        launcher.bootstrap_workspace(str(script), str(tmp_path / "missing.yaml"))

    with pytest.raises(FileNotFoundError, match="needs a YAML template"):
        launcher.bootstrap_workspace(str(script))

    existing_workspace = Path(launcher.workspace_root_for_script(str(script)))
    existing_workspace.mkdir(parents=True, exist_ok=True)
    (existing_workspace / SCRIPT_INFO_FILENAME).write_text(
        json.dumps(
            {
                "created_at": "2026-01-01 00:00:00",
                "last_used_template": "old",
            }
        ),
        encoding="utf-8",
    )
    root_default.write_text("lr: 0.2\n", encoding="utf-8")
    workspace = launcher.bootstrap_workspace(str(script))
    info = json.loads((Path(workspace) / SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["last_used_template"] == "old"

    shell_root = launcher.bootstrap_shell_workspace(workspace)
    shell_info = json.loads((Path(shell_root) / SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))
    assert shell_info["workspace_kind"] == WORKSPACE_KIND_SHELL
    assert Path(shell_root).name == SHELL_WORKSPACE_NAME

    query = launcher.launcher_query(str(script), str(config))
    assert query.startswith("/?launcher=1")
    assert "script=" in query and "config=" in query

    monkeypatch.setattr("pyruns.launcher.bootstrap_workspace", lambda script_path, custom_yaml=None: (_ for _ in ()).throw(FileNotFoundError("missing script")))
    with pytest.raises(SystemExit):
        launcher.bootstrap_from_cli(str(script))
    assert "missing script" in capsys.readouterr().out


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_merges_pending_stop_summary_into_single_error_block(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)

    task_info = {
        "name": "StopTask",
        "script": "script.py",
        "status": "failed",
        "run_index": 1,
        "start_times": ["2026-03-20_00-00-01"],
        "finish_times": [""],
        "pids": [7777],
        "_pending_stop_summary": {
            "run_index": 1,
            "event": "stopped",
            "reason": "cancelled_by_user",
            "detail_lines": ["previous_status=running"],
        },
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w", encoding="utf-8") as f:
        json.dump(task_info, f)

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.wait.return_value = 1
    mock_proc.returncode = 1
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"stopped output", b""])
    mock_popen.return_value = mock_proc

    res = run_task_worker(
        task_dir=task_dir,
        name="StopTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert res["status"] == "failed"
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Run #1 stopped" in content
    assert "reason=cancelled_by_user" in content
    assert "previous_status=running" in content
    assert "exit_code=1" in content
    assert "reason=exit_code 1" not in content

    final_info = json.loads(Path(task_dir, TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert "_pending_stop_summary" not in final_info

# ═══════════════════════════════════════════════════════════════
#  LogEmitter — publish-subscribe event bus
# ═══════════════════════════════════════════════════════════════

from pyruns.utils.events import LogEmitter


def test_log_emitter_subscribe_emit():
    emitter = LogEmitter()
    received = []
    emitter.subscribe("task1", lambda chunk: received.append(chunk))
    emitter.emit("task1", "hello\r\n")
    emitter.emit("task1", "world\r\n")
    assert received == ["hello\r\n", "world\r\n"]


def test_log_emitter_unsubscribe():
    emitter = LogEmitter()
    received = []
    cb = lambda chunk: received.append(chunk)
    emitter.subscribe("task1", cb)
    emitter.emit("task1", "before")
    emitter.unsubscribe("task1", cb)
    emitter.emit("task1", "after")
    assert received == ["before"]


def test_log_emitter_multiple_subscribers():
    emitter = LogEmitter()
    r1, r2 = [], []
    emitter.subscribe("task1", lambda c: r1.append(c))
    emitter.subscribe("task1", lambda c: r2.append(c))
    emitter.emit("task1", "data")
    assert r1 == ["data"]
    assert r2 == ["data"]


def test_log_emitter_no_subscribers():
    """emit with no subscribers should not raise."""
    emitter = LogEmitter()
    emitter.emit("nonexistent_task", "should not crash")


def test_log_emitter_isolation():
    """Subscribers only receive events for their subscribed task."""
    emitter = LogEmitter()
    received = []
    emitter.subscribe("task_A", lambda c: received.append(c))
    emitter.emit("task_B", "wrong task")
    emitter.emit("task_A", "right task")
    assert received == ["right task"]


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator — task creation and file writing
# ═══════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════
#  create_task_object
# ═══════════════════════════════════════════════════════════════

class TestCreateTaskObject:
    def test_basic_fields(self):
        obj = create_task_object("/tmp/task1", "my-task", config={"lr": 0.01})
        assert obj["dir"] == "/tmp/task1"
        assert obj["name"] == "my-task"
        assert obj["status"] == "pending"
        assert obj["config"] == {"lr": 0.01}
        assert obj["env"] == {}

    def test_created_at_format(self):
        obj = create_task_object("/tmp", "t", config={})
        # Should be like "2026-02-12 15:30:00"
        assert len(obj["created_at"]) == 19
        assert "-" in obj["created_at"]
        assert "_" in obj["created_at"]  # 2026-02-12_15-30-00

    def test_shell_task_fields(self):
        obj = create_task_object(
            "/tmp/task-shell",
            "shell-task",
            task_kind=TASK_KIND_SHELL,
            config_text="echo hello\n",
        )
        assert obj["task_kind"] == TASK_KIND_SHELL
        assert obj["config_file"] == SHELL_CONFIG_FILENAME
        assert obj["config_text"] == "echo hello\n"


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator.create_task
# ═══════════════════════════════════════════════════════════════

class TestTaskGeneratorCreateTask:
    def test_creates_folder(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "epochs": 10}
        task = gen.create_task("my-exp", cfg)

        assert os.path.isdir(task["dir"])

    def test_folder_name_matches_prefix(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("baseline", {"lr": 0.01})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("baseline")

    def test_writes_task_info_json(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("exp1", {"lr": 0.01})

        info_path = os.path.join(task["dir"], "task_info.json")
        assert os.path.exists(info_path)
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == "exp1"
        assert info["status"] == "pending"

    def test_writes_config_yaml(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "model": {"name": "resnet"}}
        task = gen.create_task("exp2", cfg)

        cfg_path = os.path.join(task["dir"], "config.yaml")
        assert os.path.exists(cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded["lr"] == 0.01
        assert loaded["model"]["name"] == "resnet"

    def test_creates_run_logs_dir(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("exp3", {"x": 1})

        log_dir = os.path.join(task["dir"], "run_logs")
        assert os.path.isdir(log_dir)
        # Log file is NOT created until execution starts
        assert not os.path.exists(os.path.join(log_dir, "run1.log"))

    def test_meta_keys_stripped_from_config(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {"lr": 0.01, "_meta_desc": "lr=0.01", "_meta_other": "x"}
        task = gen.create_task("exp4", cfg)

        cfg_path = os.path.join(task["dir"], "config.yaml")
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert "_meta_desc" not in loaded
        assert "_meta_other" not in loaded
        assert loaded["lr"] == 0.01

    def test_group_index_in_folder_name(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="[3-of-10]")

        folder = os.path.basename(task["dir"])
        assert "batch-run_[3-of-10]" in folder

    def test_display_name_with_group_index(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="[3-of-10]")
        assert task["name"] == "batch-run_[3-of-10]"

    def test_deduplication_on_name_clash(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        t1 = gen.create_task("same-name", {"x": 1})
        t2 = gen.create_task("same-name", {"x": 2})

        assert t1["dir"] != t2["dir"]
        assert os.path.isdir(t1["dir"])
        assert os.path.isdir(t2["dir"])
        assert t2["name"] == os.path.basename(t2["dir"])
        with open(os.path.join(t2["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == t2["name"]

    def test_deduplication_keeps_unique_dirs_when_timestamp_suffix_collides(self, tmp_path, monkeypatch):
        gen = TaskGenerator(root_dir=str(tmp_path))
        monkeypatch.setattr("pyruns.core.task_generator.time.time", lambda: 1234.567)

        tasks = [
            gen.create_task("same-name", {"x": 1}),
            gen.create_task("same-name", {"x": 2}),
            gen.create_task("same-name", {"x": 3}),
        ]

        assert len({task["dir"] for task in tasks}) == 3
        assert len({task["name"] for task in tasks}) == 3
        for task in tasks:
            assert os.path.isdir(task["dir"])

    def test_empty_prefix_uses_timestamp(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("", {"x": 1})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("task_")

    def test_task_kind_written_per_task(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task_cfg = gen.create_task("cfg-task", {"x": 1}, task_kind=TASK_KIND_CONFIG)
        with patch("pyruns.core.task_generator.get_shell_config_filename_for_workspace", return_value=SHELL_CONFIG_FILENAME):
            task_shell = gen.create_shell_task("shell-task", "echo shell\n")

        with open(os.path.join(task_cfg["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info_cfg = json.load(f)
        with open(os.path.join(task_shell["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info_shell = json.load(f)

        assert info_cfg["task_kind"] == TASK_KIND_CONFIG
        assert "config_mode" not in info_cfg
        assert info_cfg["config_file"] == CONFIG_FILENAME
        assert info_shell["task_kind"] == TASK_KIND_SHELL
        assert "config_mode" not in info_shell
        assert info_shell["config_file"] == SHELL_CONFIG_FILENAME

    def test_create_shell_task_uses_runtime_specific_payload_filename(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))

        with patch(
            "pyruns.core.task_generator.get_shell_config_filename_for_workspace",
            return_value=POWERSHELL_CONFIG_FILENAME,
        ):
            task_shell = gen.create_shell_task("shell-task", "Write-Host 'hello'\n")

        assert task_shell["task_kind"] == TASK_KIND_SHELL
        assert task_shell["config_file"] == POWERSHELL_CONFIG_FILENAME
        assert os.path.exists(os.path.join(task_shell["dir"], POWERSHELL_CONFIG_FILENAME))

    def test_legacy_config_task_kind_is_loaded_as_python(self, tmp_path):
        task_dir = tmp_path / "legacy-task"
        task_dir.mkdir()
        save_task_info(str(task_dir), {
            "name": "legacy-task",
            "status": "pending",
            "created_at": "2026-01-01_00-00-00",
            "config_mode": "config",
            "config_file": CONFIG_FILENAME,
        })
        save_yaml(str(task_dir / CONFIG_FILENAME), {"x": 1})

        manager = TaskManager(tasks_dir=str(tmp_path), lazy_scan=False)
        task = manager.get_task("legacy-task")

        assert task is not None
        assert task["task_kind"] == TASK_KIND_CONFIG
        assert task["config_file"] == CONFIG_FILENAME

    def test_legacy_config_task_kind_input_writes_python(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("legacy-input", {"x": 1}, task_kind="config")

        with open(os.path.join(task["dir"], "task_info.json"), "r", encoding="utf-8") as f:
            info = json.load(f)

        assert task["task_kind"] == TASK_KIND_CONFIG
        assert info["task_kind"] == TASK_KIND_CONFIG
        assert "config_mode" not in info

    def test_invalid_task_kind_is_rejected(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        with pytest.raises(ValueError, match="Unsupported task kind"):
            gen.create_task("invalid", {"x": 1}, task_kind="unknown-kind")

    def test_invalid_task_name_is_rejected(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        with pytest.raises(ValueError, match="invalid characters"):
            gen.create_task("bad/name", {"x": 1})


# ═══════════════════════════════════════════════════════════════
#  TaskGenerator.create_tasks (batch)
# ═══════════════════════════════════════════════════════════════

class TestTaskGeneratorCreateTasks:
    def test_single_config(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        tasks = gen.create_tasks([{"x": 1}], "single")
        assert len(tasks) == 1
        # No index suffix for single task
        assert tasks[0]["name"] == "single"

    def test_multiple_configs_get_index(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = [{"x": i} for i in range(3)]
        tasks = gen.create_tasks(configs, "batch")

        assert len(tasks) == 3
        assert tasks[0]["name"] == "batch_[1-of-3]"
        assert tasks[1]["name"] == "batch_[2-of-3]"
        assert tasks[2]["name"] == "batch_[3-of-3]"

    def test_batch_with_pipe_configs(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        base = {"lr": "0.001 | 0.01", "bs": 32}
        configs = generate_batch_configs(base)
        assert len(configs) == 2

        tasks = gen.create_tasks(configs, "exp")
        assert len(tasks) == 2
        # Configs should have typed values, not pipe strings
        for task in tasks:
            cfg_path = os.path.join(task["dir"], "config.yaml")
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            assert isinstance(cfg["lr"], (int, float))

    def test_batch_folders_unique(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = [{"x": i} for i in range(5)]
        tasks = gen.create_tasks(configs, "run")

        dirs = [t["dir"] for t in tasks]
        assert len(set(dirs)) == 5  # all unique


# ═══════════════════════════════════════════════════════════════
#  Report — CSV and JSON export
# ═══════════════════════════════════════════════════════════════


def _make_task(tmp_path, name, records=None, starts=None, finishes=None, pids=None):
    """Create a task dict with a real task_info.json on disk."""
    task_dir = str(tmp_path / name)
    os.makedirs(task_dir, exist_ok=True)
    info = {
        "name": name,
        "status": "completed",
        "start_times": starts or ["2026-01-01 00:00:00"],
        "finish_times": finishes or ["2026-01-01 00:01:00"],
        "pids": pids or [12345],
    }
    if records is not None:
        info[RECORDS_KEY] = records
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump(info, f)
    return {
        "name": name,
        "status": "completed",
        "dir": task_dir,
        "start_times": info["start_times"],
        "finish_times": info["finish_times"],
        "pids": info["pids"],
    }


class TestBuildExportCSV:
    def test_single_task_single_run(self, tmp_path):
        task = _make_task(tmp_path, "t1", records=[{"loss": 0.5, "acc": 92}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["name"] == "t1"
        assert rows[0]["run"] == "1"
        assert rows[0]["loss"] == "0.5"
        assert rows[0]["acc"] == "92"

    def test_multi_run(self, tmp_path):
        task = _make_task(
            tmp_path, "t2",
            records=[{"loss": 0.5}, {"loss": 0.1}],
            starts=["2026-01-01 00:00:00", "2026-01-02 00:00:00"],
            finishes=["2026-01-01 00:01:00", "2026-01-02 00:01:00"],
            pids=[111, 222],
        )
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["run"] == "1"
        assert rows[1]["run"] == "2"
        assert rows[0]["pid"] == "111"
        assert rows[1]["pid"] == "222"

    def test_empty_tasks(self):
        csv_str = build_export_csv([])
        assert csv_str == ""

    def test_column_order(self, tmp_path):
        task = _make_task(tmp_path, "t3", records=[{"zeta": 1, "alpha": 2}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        cols = reader.fieldnames
        # Priority columns should come first
        assert cols[:4] == ["name", "status", "run", "start_time"]


class TestBuildExportJSON:
    def test_basic(self, tmp_path):
        task = _make_task(tmp_path, "j1", records=[{"loss": 0.3}])
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["task_name"] == "j1"
        assert result[0]["monitor"] == [{"loss": 0.3}]

    def test_no_monitor_excluded(self, tmp_path):
        task = _make_task(tmp_path, "j2")
        result = json.loads(build_export_json([task]))
        assert len(result) == 0  # tasks with no monitor are excluded


