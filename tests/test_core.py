"""
Tests for pyruns.core — config_manager, system_metrics, executor,
task_generator, and report.
"""
import csv
import gc
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor
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
    MAX_CONFIG_FILE_BYTES,
    WORKSPACE_KIND_SHELL,
)
from omegaconf import DictConfig, ListConfig, OmegaConf

from pyruns.core.config_manager import ConfigManager
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
from pyruns.core.task_manager import TaskManager, TaskStateConflict
from pyruns.launcher import (
    bootstrap_shell_workspace,
    bootstrap_workspace,
    list_script_candidates,
    shell_workspace_root_for_run_root,
    workspace_root_for_script,
)
from pyruns.utils.batch_utils import generate_batch_configs
from pyruns.utils.info_io import (
    MAX_RUN_HISTORY_SLOTS,
    ensure_run_slot,
    load_task_info,
    normalize_run_history,
    run_slot_count,
    save_task_info,
    update_task_info,
)
from pyruns.utils.config_utils import save_yaml
from pyruns.utils.shell_runtime import get_shell_config_filename_for_workspace, get_shell_runtime_for_workspace


class _StaticGpuProvider:
    def __init__(self, devices):
        self.devices = devices
        self.calls = 0

    def sample(self):
        self.calls += 1
        return list(self.devices)


def _make_task_manager(tasks_dir: Path, *, lazy_scan=False, **kwargs) -> TaskManager:
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        return TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=lazy_scan,
            **kwargs,
        )


def _mark_task_owned_by_manager(
    manager: TaskManager,
    task_name: str,
    task_dir: Path,
    *,
    pids: list[int] | None = None,
    counts_for_batch: bool = True,
) -> None:
    def _apply(info):
        info["status"] = "running"
        info["run_index"] = 1
        info["pids"] = list(pids or [12345])
        info["pid_create_times"] = [1000.0 for _pid in info["pids"]]
        info["runner_id"] = manager.runner_id
        info["runner_host"] = manager.runner_host
        info["lease_heartbeat"] = time.time()
        info["lease_until"] = time.time() + 60

    updated = update_task_info(str(task_dir), _apply)
    with manager._lock:
        current = manager._tasks_by_name[task_name]
        manager._apply_info_to_task(current, updated)
        manager._mark_running_locked(task_name, counts_for_batch=counts_for_batch)
        manager._recompute_processing_flag_locked()


def _write_worker_task_info(task_dir: Path, name: str) -> str:
    (task_dir / RUN_LOGS_DIR).mkdir(parents=True, exist_ok=True)
    (task_dir / TASK_INFO_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "script": "script.py",
                "status": "queued",
                "start_times": [],
                "finish_times": [],
            }
        ),
        encoding="utf-8",
    )
    return str(task_dir)


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


def test_executor_support_code_uses_unpredictable_private_temp_roots(tmp_path, monkeypatch):
    launcher_root = tmp_path / "launcher"
    package_dir = launcher_root / "pyruns"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("SAFE = True\n", encoding="utf-8")

    fingerprint = executor._pyruns_package_fingerprint(str(package_dir))
    old_import_digest = executor.hashlib.sha1(
        f"{fingerprint}:{os.getpid()}".encode("utf-8")
    ).hexdigest()[:16]
    old_import_root = tmp_path / f"pyruns-import-{old_import_digest}"
    (old_import_root / "pyruns").mkdir(parents=True)
    (old_import_root / "pyruns" / "__init__.py").write_text(
        "raise RuntimeError('attacker controlled')\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(executor.tempfile, "gettempdir", lambda: str(tmp_path))
    executor._ISOLATED_IMPORT_ROOT_CACHE.clear()
    executor._SITE_GUARD_ROOT_CACHE.clear()

    import_root = Path(executor._isolated_pyruns_import_root(str(package_dir)))
    assert import_root != old_import_root
    assert (import_root / "pyruns" / "__init__.py").read_text(encoding="utf-8") == "SAFE = True\n"
    if os.name != "nt":
        assert import_root.stat().st_mode & 0o077 == 0

    old_guard_digest = executor.hashlib.sha1(
        f"{import_root}:{os.getpid()}".encode("utf-8")
    ).hexdigest()[:16]
    old_guard_root = tmp_path / f"pyruns-guard-{old_guard_digest}"
    old_guard_root.mkdir()
    (old_guard_root / "sitecustomize.py").write_text(
        "raise RuntimeError('attacker controlled')\n",
        encoding="utf-8",
    )

    guard_root = Path(executor._pyruns_sitecustomize_guard_root(str(import_root)))
    assert guard_root != old_guard_root
    assert "_PyrunsImportGuard" in (guard_root / "sitecustomize.py").read_text(encoding="utf-8")
    if os.name != "nt":
        assert guard_root.stat().st_mode & 0o077 == 0

    executor._ISOLATED_IMPORT_ROOT_CACHE.clear()
    second_import_root = Path(executor._isolated_pyruns_import_root(str(package_dir)))
    assert second_import_root != import_root


def test_omegaconf_nested_access_and_container_export():
    data = {
        "lr": 0.01,
        "optimizer": {
            "name": "adam",
            "beta": 0.9
        },
        "layers": [64, 128, {"dropout": 0.5}],
        "label": "train",
        "_private": "hidden",
    }
    node = OmegaConf.create(data)

    assert node.lr == 0.01
    assert node.optimizer.name == "adam"
    assert node.optimizer.beta == 0.9
    assert len(node.layers) == 3
    assert node.layers[0] == 64
    assert node.layers[2].dropout == 0.5
    assert node["_private"] == "hidden"
    d = OmegaConf.to_container(node, resolve=False)
    assert "lr" in d
    assert "optimizer" in d
    assert isinstance(d["optimizer"], dict)
    assert d["optimizer"]["name"] == "adam"
    assert isinstance(d["layers"], list)
    assert isinstance(d["layers"][2], dict)
    assert d["layers"][2]["dropout"] == 0.5
    assert d["_private"] == "hidden"
    assert isinstance(node, DictConfig)
    assert isinstance(node.optimizer, DictConfig)
    assert isinstance(node.layers, ListConfig)


def test_config_manager_rejects_unloaded_missing_unsupported_and_invalid(
    tmp_path,
    monkeypatch,
):
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="not loaded"):
        cm.load()

    cm = ConfigManager()
    with pytest.raises(FileNotFoundError):
        cm.read("does_not_exist_at_all.yaml")

    p = tmp_path / "cfg.txt"
    p.write_text("Hello", encoding="utf-8")
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Unsupported format"):
        cm.read(str(p))

    class MockLogger:
        def __init__(self):
            self.logs = []

        def info(self, msg, *args):
            self.logs.append(("INFO", msg % args))

        def error(self, msg, *args):
            self.logs.append(("ERROR", msg % args))

    logger = MockLogger()
    monkeypatch.setattr("pyruns.core.config_manager.logger", logger)
    p = tmp_path / "bad.yaml"
    p.write_text("a: \n  - b:\n c: [invalid yaml", encoding="utf-8")
    cm = ConfigManager()
    with pytest.raises(RuntimeError, match="Failed to parse config"):
        cm.read(str(p))
    assert any("Failed to parse config" in msg for level, msg in logger.logs if level == "ERROR")


def test_config_manager_reads_yaml_json_and_list(tmp_path):
    yaml_path = tmp_path / "cfg.yaml"
    yaml_path.write_text("a: 1\nb: 2", encoding="utf-8")
    yaml_manager = ConfigManager()
    yaml_manager.read(str(yaml_path))
    yaml_node = yaml_manager.load()
    assert yaml_node.a == 1
    assert yaml_node.b == 2

    json_path = tmp_path / "cfg.json"
    json_path.write_text('{"a": 1, "b": {"c": 3}}', encoding="utf-8")
    json_manager = ConfigManager()
    json_manager.read(str(json_path))
    json_node = json_manager.load()
    assert json_node.a == 1
    assert json_node.b.c == 3

    list_path = tmp_path / "list.yaml"
    list_path.write_text("- a: 1\n- b: 2", encoding="utf-8")
    list_manager = ConfigManager()
    list_manager.read(str(list_path))
    nodes = list_manager.load()
    assert isinstance(nodes, ListConfig)
    assert len(nodes) == 2
    assert nodes[0].a == 1
    assert nodes[1].b == 2


def test_config_manager_uses_omegaconf_interpolation_and_pyruns_scalars(tmp_path):
    path = tmp_path / "advanced.yaml"
    path.write_text(
        "base: /tmp\n"
        "output: ${base}/results\n"
        "range: 30:40:1\n"
        "scientific: 5e-3\n",
        encoding="utf-8",
    )

    manager = ConfigManager()
    manager.read(str(path))
    config = manager.load()

    assert isinstance(config, DictConfig)
    assert config.output == "/tmp/results"
    assert config.range == "30:40:1"
    assert config.scientific == 0.005
    assert OmegaConf.to_container(config, resolve=False)["output"] == "${base}/results"


def test_config_manager_rejects_oversized_config(tmp_path):
    path = tmp_path / "oversized.yaml"
    path.write_bytes(b"value: " + b"x" * (MAX_CONFIG_FILE_BYTES + 1))

    with pytest.raises(RuntimeError, match="too large"):
        ConfigManager().read(str(path))


#  SystemMonitor

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


#  Executor


def test_prepare_env_prefers_current_python_executable_on_path(monkeypatch):
    stale_path = os.pathsep.join(["/not/current/python", "/another/bin"])
    monkeypatch.setenv("PATH", stale_path)

    env = _prepare_env(
        task_dir="/fake/dir",
        task_kind=TASK_KIND_SHELL,
        config_file=SHELL_CONFIG_FILENAME,
    )

    path_entries = env["PATH"].split(os.pathsep)
    assert path_entries[0] == os.path.dirname(sys.executable)
    assert "/not/current/python" in path_entries
    assert ENV_KEY_CONFIG not in env


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
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert "/parent/pythonpath" in env["PYTHONPATH"]
    assert env[ENV_KEY_CONFIG] == os.path.abspath(
        os.path.join("/fake/task", CONFIG_FILENAME)
    )


def test_prepare_env_never_exposes_ui_access_token(monkeypatch):
    monkeypatch.setenv("PYRUNS_UI_TOKEN", "server-secret")

    env = _prepare_env(
        extra_env={"PYRUNS_UI_TOKEN": "task-override"},
        task_dir="/fake/task",
        task_kind=TASK_KIND_SHELL,
    )

    assert "PYRUNS_UI_TOKEN" not in env


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
        "  WORKSPACE_VALUE: workspace\n"
        "  CUDA_VISIBLE_DEVICES: '0'\n",
        encoding="utf-8",
    )
    wsl_env_keys = set()

    env = _prepare_env(
        extra_env={"CUDA_VISIBLE_DEVICES": "1", "TASK_VALUE": "task"},
        task_dir=str(task_dir),
        task_kind=TASK_KIND_CONFIG,
        wsl_env_keys=wsl_env_keys,
    )

    assert env["TOKENIZERS_PARALLELISM"] == "workspace"
    assert env["CUDA_VISIBLE_DEVICES"] == "1"
    executor._augment_wsl_env(
        [r"C:\Windows\System32\wsl.exe", "--exec", "/bin/bash", "/mnt/c/run.sh"],
        env,
        wsl_env_keys,
    )

    entries = set(env["WSLENV"].split(":"))
    assert {
        "CUDA_VISIBLE_DEVICES",
        "TOKENIZERS_PARALLELISM",
        "WORKSPACE_VALUE",
        "TASK_VALUE",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "PYTHONUTF8",
    } <= entries


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

    wsl_env_keys = set()
    env = _prepare_env(
        task_dir=str(task_dir),
        task_kind=TASK_KIND_CONFIG,
        wsl_env_keys=wsl_env_keys,
    )

    assert env["CUDA_VISIBLE_DEVICES"] == "terminal"
    assert "CUDA_VISIBLE_DEVICES" not in wsl_env_keys


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
def test_build_command_argparse_expands_omegaconf_list_values(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "dataset": {"name": "dataset", "default": "toy"},
        "layers": {"name": "--layers", "nargs": "+", "default": [64]},
        "tag": {"name": "--tag", "action": "append", "default": []},
        "pair": {"name": "--pair", "action": "append", "nargs": 2, "default": []},
    }
    config = OmegaConf.create({
        "dataset": "toy",
        "layers": [128, 256],
        "tag": ["smoke", "nightly"],
        "pair": [["train", "dev"], ["test", "holdout"]],
    })

    cmd, _, cleanup_paths = _build_command(None, "train.py", None, config)

    assert cmd == [
        sys.executable,
        "train.py",
        "toy",
        "--layers",
        "128",
        "256",
        "--tag",
        "smoke",
        "--tag",
        "nightly",
        "--pair",
        "train",
        "dev",
        "--pair",
        "test",
        "holdout",
    ]
    assert cleanup_paths == []


def test_build_command_argparse_actions_execute_with_expected_values(tmp_path):
    script = tmp_path / "argparse_actions.py"
    script.write_text(
        "import argparse, json\n"
        "parser = argparse.ArgumentParser()\n"
        "parser.add_argument('--compile', action=argparse.BooleanOptionalAction, "
        "default=True)\n"
        "parser.add_argument('--enabled', type=bool, default=True)\n"
        "parser.add_argument('--fast', dest='mode', action='store_const', "
        "const='fast', default='slow')\n"
        "parser.add_argument('--labelled', dest='labels', action='append_const', "
        "const='labelled', default=[])\n"
        "parser.add_argument('--tag', action='append', default=[])\n"
        "parser.add_argument('--feature', action=argparse.BooleanOptionalAction)\n"
        "parser.add_argument('-v', '--verbose', action='count')\n"
        "parser.add_argument('--optional-label', dest='optional_labels', "
        "action='append_const', const='optional')\n"
        "args = parser.parse_args()\n"
        "print(json.dumps(vars(args), sort_keys=True))\n",
        encoding="utf-8",
    )

    command, _, _ = _build_command(
        None,
        str(script),
        None,
        OmegaConf.create(
            {
                "compile": False,
                "enabled": False,
                "mode": "fast",
                "labels": ["labelled", "labelled"],
                "tag": "grid",
                "feature": None,
                "verbose": None,
                "optional_labels": None,
            }
        ),
    )
    result = subprocess.run(command, capture_output=True, text=True, check=False)

    assert "--no-compile" in command
    assert command[command.index("--enabled") + 1] == ""
    assert command.count("--labelled") == 2
    assert command[command.index("--tag") + 1] == "grid"
    assert "--feature" not in command
    assert "--verbose" not in command
    assert "--optional-label" not in command
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "compile": False,
        "enabled": False,
        "feature": None,
        "labels": ["labelled", "labelled"],
        "mode": "fast",
        "optional_labels": None,
        "tag": ["grid"],
        "verbose": None,
    }


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_serializes_const_and_accumulative_actions(
    mock_extract,
    mock_detect,
):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "mode": {
            "name": "--fast",
            "action": "store_const",
            "const": "fast",
            "default": "slow",
        },
        "labels": {
            "name": "--labelled",
            "action": "append_const",
            "const": "labelled",
            "default": [],
        },
        "tag": {"name": "--tag", "action": "append", "default": ["base"]},
        "verbose": {
            "flags": ["-v", "--verbose"],
            "name": "--verbose",
            "action": "count",
            "default": 1,
        },
    }

    default_cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {"mode": "slow", "labels": [], "tag": ["base"], "verbose": 1},
    )
    selected_cmd, _, _ = _build_command(
        None,
        "train.py",
        None,
        {
            "mode": "fast",
            "labels": ["labelled", "labelled"],
            "tag": ["base", "extra"],
            "verbose": 3,
        },
    )

    assert default_cmd == [sys.executable, "train.py"]
    assert selected_cmd == [
        sys.executable,
        "train.py",
        "--fast",
        "--labelled",
        "--labelled",
        "--tag",
        "extra",
        "--verbose",
        "--verbose",
    ]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
@pytest.mark.parametrize(
    ("info", "value"),
    [
        ({"name": "--enabled", "action": "store_true", "default": True}, False),
        ({"name": "--disabled", "action": "store_false", "default": False}, True),
        ({"name": "--fast", "action": "store_const", "const": "fast", "default": "slow"}, "other"),
        ({"name": "-s", "action": "argparse.BooleanOptionalAction"}, False),
        ({"name": "-v", "action": "count"}, "bad"),
        ({"name": "-v", "action": "count"}, 0),
        ({"name": "--tag", "action": "append"}, []),
        ({"name": "--labelled", "action": "append_const", "const": "labelled"}, []),
    ],
)
def test_build_command_argparse_rejects_unrepresentable_action_values(
    mock_extract,
    mock_detect,
    info,
    value,
):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {"setting": info}

    with pytest.raises(RuntimeError, match="cannot represent"):
        _build_command(None, "train.py", None, {"setting": value})


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
    assert wrapper_path.parent == task_dir
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
    assert wrapper_path.parent == task_dir
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


def test_shell_workspace_root_resolves_project_and_script_roots(tmp_path):
    project_root = tmp_path / DEFAULT_ROOT_NAME
    project_root.mkdir(parents=True)
    assert shell_workspace_root_for_run_root(str(project_root)) == str(
        project_root / SHELL_WORKSPACE_NAME
    ).replace("\\", "/")

    script_root = project_root / "main"
    script_root.mkdir(parents=True)
    assert shell_workspace_root_for_run_root(str(script_root)) == str(
        project_root / SHELL_WORKSPACE_NAME
    ).replace("\\", "/")


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


@pytest.mark.parametrize("filename", ["shell.py", "SHELL.py"])
def test_shell_workspace_selector_is_reserved_from_python_script_names(tmp_path, filename):
    script_path = tmp_path / filename
    script_path.write_text("print('reserved')\n", encoding="utf-8")

    with pytest.raises(ValueError, match="reserved for the shell workspace selector"):
        workspace_root_for_script(str(script_path))
    with pytest.raises(ValueError, match="reserved for the shell workspace selector"):
        bootstrap_workspace(str(script_path))

    assert not (tmp_path / DEFAULT_ROOT_NAME).exists()


def test_bootstrap_shell_workspace_records_project_root(tmp_path):
    project_root = tmp_path / "project"
    project_root.mkdir()

    shell_root = bootstrap_shell_workspace(str(project_root / DEFAULT_ROOT_NAME))
    info = json.loads(Path(shell_root, SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"))

    assert shell_root == str(project_root / DEFAULT_ROOT_NAME / SHELL_WORKSPACE_NAME).replace("\\", "/")
    assert info["project_root"] == str(project_root).replace("\\", "/")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
def test_build_command_non_argparse_styles_require_shell_workspace(mock_detect):
    for style, message in [
        ("hydra", "shell workspace/task"),
        ("unknown", "configuration style"),
    ]:
        mock_detect.return_value = (style, None)
        with pytest.raises(RuntimeError, match=message):
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
    assert command == ["bash.exe", str(script_path).replace("\\", "/")]
    assert workdir == str(task_dir)
    assert cleanup_paths == []

    wsl_command, wsl_workdir, wsl_cleanup_paths = executor._materialize_windows_shell_wrapper(
        str(task_dir),
        r"C:\Users\me\project\_pyruns_\shell\tasks\task\run.sh",
        r"C:\Windows\System32\bash.exe",
    )
    assert wsl_command == [
        r"C:\Windows\System32\bash.exe",
        "/mnt/c/Users/me/project/_pyruns_/shell/tasks/task/run.sh",
    ]
    assert wsl_workdir == str(task_dir)
    assert wsl_cleanup_paths == []

    modern_wsl_command, modern_wsl_workdir, modern_wsl_cleanup_paths = (
        executor._materialize_windows_shell_wrapper(
            str(task_dir),
            r"C:\Users\me\project\_pyruns_\shell\tasks\task\run.sh",
            r"C:\Windows\System32\wsl.exe",
        )
    )
    assert modern_wsl_command == [
        r"C:\Windows\System32\wsl.exe",
        "--exec",
        "/bin/bash",
        "/mnt/c/Users/me/project/_pyruns_/shell/tasks/task/run.sh",
    ]
    assert modern_wsl_workdir == str(task_dir)
    assert modern_wsl_cleanup_paths == []

    env = {ENV_KEY_CONFIG: r"C:\task\config.yaml", "PYRUNS_EXAMPLE_ENV": "ok", "WSLENV": "EXISTING"}
    executor._augment_wsl_env(
        [r"C:\Windows\System32\bash.exe", "/mnt/c/run.sh"],
        env,
        {"PYRUNS_EXAMPLE_ENV", "1BAD", "EXISTING"},
    )
    assert env["WSLENV"] == f"EXISTING:{ENV_KEY_CONFIG}/p:PYRUNS_EXAMPLE_ENV"

    env = {ENV_KEY_CONFIG: r"C:\task\config.yaml", "PYRUNS_TASK_ENV": "ok"}
    executor._augment_wsl_env(
        [
            r"C:\miniconda\condabin\conda.bat",
            "run",
            "-n",
            "train",
            "--no-capture-output",
            r"C:\Windows\System32\bash.exe",
            "/mnt/c/run.sh",
        ],
        env,
        {"PYRUNS_TASK_ENV"},
    )
    assert env["WSLENV"] == f"{ENV_KEY_CONFIG}/p:PYRUNS_TASK_ENV"

    env = {"PYRUNS_WSL_VALUE": "works"}
    executor._augment_wsl_env(
        [r"C:\Windows\System32\wsl.exe", "--exec", "/bin/bash", "/mnt/c/run.sh"],
        env,
        {"PYRUNS_WSL_VALUE"},
    )
    assert env["WSLENV"] == "PYRUNS_WSL_VALUE"

    env = {ENV_KEY_CONFIG: r"C:\task\config.yaml", "WSLENV": f"{ENV_KEY_CONFIG}:OTHER"}
    executor._augment_wsl_env(
        [r"C:\Windows\System32\bash.exe", "/mnt/c/run.sh"],
        env,
        set(),
    )
    assert env["WSLENV"] == f"{ENV_KEY_CONFIG}/p:OTHER"

    ps_script = task_dir / "run.ps1"
    ps_script.write_text(
        "if (-not (Test-Path (Join-Path $PSScriptRoot 'sentinel.txt'))) { exit 7 }\n",
        encoding="utf-8",
    )
    cmd_script = task_dir / "run.cmd"
    cmd_script.write_text(
        "if not exist \"%~dp0sentinel.txt\" exit /b 7\n",
        encoding="utf-8",
    )
    (task_dir / "sentinel.txt").write_text("ok", encoding="utf-8")

    ps_command, _, ps_cleanup_paths = executor._materialize_windows_shell_wrapper(
        str(task_dir),
        str(ps_script),
        "powershell.exe",
    )
    cmd_command, _, cmd_cleanup_paths = executor._materialize_windows_shell_wrapper(
        str(task_dir),
        str(cmd_script),
        "cmd.exe",
    )
    try:
        assert ps_command[-1] == ps_cleanup_paths[0]
        assert cmd_command[-1] == cmd_cleanup_paths[0]
        assert Path(ps_cleanup_paths[0]).parent == task_dir
        assert Path(cmd_cleanup_paths[0]).parent == task_dir
        ps_wrapper_text = Path(ps_cleanup_paths[0]).read_text(encoding="utf-8-sig")
        assert "$PSScriptRoot" in ps_wrapper_text
        assert "$__pyrunsSucceeded = $?" in ps_wrapper_text
        assert "exit $__pyrunsExitCode" in ps_wrapper_text
        assert "%~dp0sentinel.txt" in Path(cmd_cleanup_paths[0]).read_text(encoding="utf-8-sig")
    finally:
        for cleanup_path in [*ps_cleanup_paths, *cmd_cleanup_paths]:
            Path(cleanup_path).unlink(missing_ok=True)


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
    monkeypatch.setattr(executor, "_is_windows", lambda: True)
    monkeypatch.setattr(executor.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    assert executor._popen_process_group_kwargs() == {"creationflags": 0x08000000}

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


def test_spawn_captured_process_hides_windows_console(monkeypatch, tmp_path):
    import pyruns.core.executor as executor

    sentinel = object()
    captured = {}

    def fake_popen(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return sentinel

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        executor,
        "spawn_terminal_process",
        MagicMock(side_effect=Exception("ConPTY unavailable")),
    )
    monkeypatch.setattr(executor, "_is_windows", lambda: True)
    monkeypatch.setattr(
        executor.subprocess,
        "CREATE_NO_WINDOW",
        0x08000000,
        raising=False,
    )

    result = executor._spawn_captured_process(
        ["powershell", "-File", "task.ps1"],
        workdir=str(tmp_path),
        env={"PATH": "test"},
        preserve_terminal_output=True,
    )

    assert result is sentinel
    assert captured["command"] == ["powershell", "-File", "task.ps1"]
    assert captured["kwargs"]["creationflags"] == 0x08000000
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] is subprocess.PIPE
    assert captured["kwargs"]["stderr"] is subprocess.STDOUT
    assert captured["kwargs"]["shell"] is False


def test_terminal_output_filter_preserves_sgr_and_removes_screen_controls():
    from pyruns.utils.terminal_capture import _SgrOutputFilter

    output_filter = _SgrOutputFilter()
    rendered = b"".join(
        [
            output_filter.feed(b"\x1b[?9001h\x1b[?25l\x1b[2"),
            output_filter.feed(b"J\x1b[m\x1b[38;5;9mred"),
            output_filter.feed(
                b"\x1b]0;PowerShell\x07\x1b[?25h\x1b[m\r\n"
            ),
            output_filter.finish(),
        ]
    )

    assert rendered == b"\x1b[m\x1b[38;5;9mred\x1b[m\r\n"
    assert b"\x1b[2J" not in rendered
    assert b"PowerShell" not in rendered

    unterminated_color = _SgrOutputFilter()
    rendered = b"".join(
        [
            unterminated_color.feed(b"\x1b[38;5;9merror\r\n"),
            unterminated_color.finish(),
        ]
    )
    assert rendered == b"\x1b[38;5;9merror\r\n\x1b[0m"
    assert unterminated_color.finish() == b""


def test_windows_terminal_capture_forces_native_conpty(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from pyruns.utils import terminal_capture

    fake_process = MagicMock()
    fake_process.pid = 1234
    spawn = MagicMock(return_value=fake_process)
    monkeypatch.setitem(
        sys.modules,
        "winpty",
        SimpleNamespace(
            Backend=SimpleNamespace(ConPTY=0),
            PtyProcess=SimpleNamespace(spawn=spawn),
        ),
    )

    adapter = terminal_capture._spawn_windows_conpty(
        ["powershell", "-File", "task.ps1"],
        cwd=str(tmp_path),
        env={"PATH": "test"},
    )

    assert adapter.pid == 1234
    assert spawn.call_args.kwargs["backend"] == 0
    assert spawn.call_args.kwargs["env"]["TERM"] == "xterm-256color"
    assert spawn.call_args.kwargs["env"]["COLORTERM"] == "truecolor"


@pytest.mark.skipif(os.name == "nt", reason="requires a POSIX PTY")
def test_posix_terminal_capture_preserves_command_colors(tmp_path):
    from pyruns.utils.terminal_capture import _spawn_posix_pty

    process = _spawn_posix_pty(
        ["sh", "-c", "printf '\\033[31mred\\033[0m\\n'"],
        cwd=str(tmp_path),
        env=os.environ.copy(),
    )
    chunks = []
    while True:
        chunk = process.stdout.read1(4096)
        if not chunk:
            break
        chunks.append(chunk)
    assert process.wait(timeout=5) == 0
    process.close_output()

    assert b"\x1b[31mred\x1b[0m" in b"".join(chunks)


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.parse_utils.extract_argparse_params")
def test_build_command_argparse_handles_unusual_param_shapes_and_fallbacks(mock_extract, mock_detect):
    mock_detect.return_value = ("argparse", None)
    mock_extract.return_value = {
        "input": {"name": []},
        "cache": {"flags": ["--no-cache"], "action": "argparse.BooleanOptionalAction"},
        "short": {"flags": ["-s"], "action": "argparse.BooleanOptionalAction"},
        "flag_bool": {"name": "--flag-bool", "type": "bool", "default": True},
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
            "short": True,
            "flag_bool": False,
            "verbose": None,
            "tag": ["a", "b"],
        },
    )

    assert cleanup_paths == []
    assert cmd[:3] == [sys.executable, "train.py", "data-a"]
    assert "data-b" in cmd
    assert "--no-no-cache" in cmd
    assert "-s" in cmd
    assert "--flag-bool" in cmd and cmd[cmd.index("--flag-bool") + 1] == ""
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


def test_executor_rejects_task_payload_paths_outside_task_directory(tmp_path):
    import pyruns.core.executor as executor

    task_dir = tmp_path / DEFAULT_ROOT_NAME / "train" / TASKS_DIR / "safe"
    task_dir.mkdir(parents=True)
    outside = tmp_path / DEFAULT_ROOT_NAME / "train" / "outside.sh"
    outside.write_text("echo unsafe\n", encoding="utf-8")
    escaped = os.path.join("..", "..", "outside.sh")

    with pytest.raises(ValueError, match="outside the task directory"):
        executor._build_shell_command(str(task_dir), escaped)
    with pytest.raises(ValueError, match="outside the task directory"):
        executor._prepare_env(task_dir=str(task_dir), config_file=escaped)


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
        captured["kwargs"] = kwargs
        return Result()

    monkeypatch.setenv("GIT_OPTIONAL_LOCKS", "1")
    monkeypatch.setattr(executor.subprocess, "run", fake_run)

    assert executor._git_bytes("/repo", ["status", "--porcelain=v1"]) == b"ok"
    assert captured["command"] == ["git", "status", "--porcelain=v1"]
    assert captured["env"]["GIT_OPTIONAL_LOCKS"] == "0"
    for key, value in executor.hidden_subprocess_kwargs().items():
        assert captured["kwargs"][key] == value
    assert os.environ["GIT_OPTIONAL_LOCKS"] == "1"



@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_success(mock_popen, mock_emit, mock_detect, tmp_path):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = _write_worker_task_info(tmp_path, "TestTask")
        
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
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "r") as f:
        info = json.load(f)
        
    assert info["status"] == "completed"
    assert info["progress"] == 1.0
    assert len(info["start_times"]) == 1
    assert len(info["finish_times"]) == 1
    assert info["pids"] == [9999]
    assert info["exit_codes"] == [0]
    assert len(info["durations"]) == 1
    assert info["durations"][0] >= 0
    assert len(info.get("records", [])) == 1
    assert info["source_states"] == [source_state]

    # Check log file was written by _tee_output
    log_path = os.path.join(task_dir, "run_logs", "run1.log")
    assert os.path.exists(log_path)
    with open(log_path, "rb") as f:
        log_content = f.read()
    assert b"hello output" in log_content
    assert source_state.encode("utf-8") in log_content
    assert b"[PYRUNS] Exit code: 0" in log_content
    assert b"[PYRUNS] Duration:" in log_content

    # Check emit was called
    assert mock_emit.called
    assert all(call.kwargs.get("log_file_name") == "run1.log" for call in mock_emit.call_args_list)


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_duration_excludes_log_reader_drain_delay(mock_popen, _mock_emit, mock_detect, tmp_path, monkeypatch):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = _write_worker_task_info(tmp_path, "DurationTask")

    clock = {"value": 100.0}
    monkeypatch.setattr(executor.time, "monotonic", lambda: clock["value"])
    original_join = executor.threading.Thread.join

    def delayed_join(thread, timeout=None):
        if timeout == 5:
            clock["value"] += 5.0
        return original_join(thread, timeout=timeout)

    monkeypatch.setattr(executor.threading.Thread, "join", delayed_join)
    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.returncode = 0
    mock_proc.stdout.read1 = MagicMock(return_value=b"")

    def wait():
        clock["value"] += 2.0
        return 0

    mock_proc.wait.side_effect = wait
    mock_popen.return_value = mock_proc

    result = run_task_worker(
        task_dir=task_dir,
        name="DurationTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["duration_seconds"] == 2.0
    assert load_task_info(task_dir)["durations"] == [2.0]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_detaches_inherited_output_after_parent_exit(
    mock_popen,
    _mock_emit,
    mock_detect,
    tmp_path,
    monkeypatch,
):
    mock_detect.return_value = ("pyruns_load", None)
    monkeypatch.setattr(executor, "_OUTPUT_READER_DRAIN_TIMEOUT_SEC", 0.01)
    task_dir = _write_worker_task_info(tmp_path, "DetachedOutputTask")

    release_background_output = threading.Event()
    reader_finished = threading.Event()
    read_count = 0

    def read_output(_size):
        nonlocal read_count
        read_count += 1
        if read_count == 1:
            return b"parent output\n"
        if read_count == 2:
            assert release_background_output.wait(2)
            return b"background output\n"
        reader_finished.set()
        return b""

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.returncode = 0
    mock_proc.wait.return_value = 0
    mock_proc.poll.return_value = 0
    mock_proc.stdout.read1.side_effect = read_output
    mock_popen.return_value = mock_proc

    try:
        with patch(
            "pyruns.core.executor._build_run_source_state",
            return_value="git none | unknown | script none",
        ):
            result = run_task_worker(
                task_dir=task_dir,
                name="DetachedOutputTask",
                created_at="now",
                config={},
                run_index=1,
            )
    finally:
        release_background_output.set()

    assert reader_finished.wait(1)
    assert result["status"] == "completed"
    assert load_task_info(task_dir)["status"] == "completed"
    run_text = Path(task_dir, RUN_LOGS_DIR, "run1.log").read_text(encoding="utf-8")
    assert "parent output" in run_text
    assert "background output" not in run_text
    assert run_text.rstrip().splitlines()[-1].startswith("[PYRUNS] Duration: ")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_drains_output_while_collecting_source_state(
    mock_popen,
    mock_emit,
    mock_detect,
    tmp_path,
    monkeypatch,
):
    import pyruns.core.executor as executor

    monkeypatch.setattr(executor, "_SOURCE_OUTPUT_SPOOL_MAX_BYTES", 1)
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = _write_worker_task_info(tmp_path, "FastStartTask")

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.return_value = 0
    mock_popen.return_value = mock_proc
    order = []
    source_started = threading.Event()
    output_read = threading.Event()
    output_emitted = threading.Event()
    allow_eof = threading.Event()

    def read_output(_size):
        if not output_read.is_set():
            assert source_started.wait(1)
            order.append("output")
            output_read.set()
            return b"done\n"
        assert allow_eof.wait(1)
        return b""

    mock_proc.stdout.read1 = MagicMock(side_effect=read_output)

    def wait_for_output():
        assert output_emitted.wait(1)
        allow_eof.set()
        return 0

    mock_proc.wait.side_effect = wait_for_output

    def record_emit(_name, content, **_kwargs):
        if "done" in content:
            output_emitted.set()

    mock_emit.side_effect = record_emit

    def build_source_state(**kwargs):
        order.append("source")
        source_started.set()
        assert output_read.wait(1)
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
    assert output_read.wait(1)
    assert output_emitted.wait(1)
    assert order[0] == "popen"
    assert order.index("output") > order.index("source")
    info = load_task_info(task_dir)
    assert info["source_states"] == ["git late | clean | script late"]
    run_text = Path(task_dir, RUN_LOGS_DIR, "run1.log").read_text(encoding="utf-8")
    assert run_text.index("[PYRUNS] Source git late") < run_text.index("done")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_drains_output_after_capture_storage_failure(
    mock_popen,
    _mock_emit,
    mock_detect,
    tmp_path,
    monkeypatch,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = _write_worker_task_info(tmp_path, "CaptureFailureTask")

    output_drained = threading.Event()
    chunks = iter([b"x" * 4096, b""])

    def read_output(_size):
        chunk = next(chunks)
        if not chunk:
            output_drained.set()
        return chunk

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.returncode = 0
    mock_proc.stdout.read1.side_effect = read_output

    def wait_for_drain():
        if not output_drained.wait(1):
            raise RuntimeError("child remained blocked because output was not drained")
        return 0

    mock_proc.wait.side_effect = wait_for_drain
    mock_popen.return_value = mock_proc

    def fail_spool(*_args, **_kwargs):
        raise OSError("capture spool unavailable")

    monkeypatch.setattr(executor.tempfile, "SpooledTemporaryFile", fail_spool)
    with patch("pyruns.core.executor._build_run_source_state", return_value="git none | unknown | script none"):
        result = run_task_worker(
            task_dir=task_dir,
            name="CaptureFailureTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert output_drained.is_set()
    assert result["status"] == "failed"
    assert "output capture failed" in result["error"].lower()
    assert load_task_info(task_dir)["status"] == "failed"
    assert "capture spool unavailable" in Path(
        task_dir,
        RUN_LOGS_DIR,
        ERROR_LOG_FILENAME,
    ).read_text(encoding="utf-8")


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.kill_process")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_closes_capture_when_process_wait_fails(
    mock_popen,
    mock_kill,
    _mock_emit,
    mock_detect,
    tmp_path,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = _write_worker_task_info(tmp_path, "WaitFailureTask")

    terminated = threading.Event()
    output_closed = threading.Event()
    output_finished = threading.Event()

    def wait(timeout=None):
        if timeout is None:
            raise OSError("wait failed")
        assert terminated.is_set()
        return 1

    def poll():
        return 1 if terminated.is_set() else None

    def read_output(_size):
        assert output_closed.wait(1)
        output_finished.set()
        return b""

    def kill(_pid, expected_create_time=None):
        terminated.set()
        return True

    mock_proc = MagicMock()
    mock_proc.pid = 9999
    mock_proc.wait.side_effect = wait
    mock_proc.poll.side_effect = poll
    mock_proc.stdout.read1.side_effect = read_output
    mock_proc.close_output.side_effect = output_closed.set
    mock_popen.return_value = mock_proc
    mock_kill.side_effect = kill

    with patch(
        "pyruns.core.executor._build_run_source_state",
        return_value="git none | unknown | script none",
    ):
        result = run_task_worker(
            task_dir=task_dir,
            name="WaitFailureTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert result["status"] == "failed"
    assert result["error"] == "wait failed"
    assert output_finished.is_set()
    mock_proc.close_output.assert_called_once_with()


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
    assert info["exit_codes"] == [1]
    assert len(info["durations"]) == 1
    assert info["durations"][0] >= 0
    
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


def test_run_task_worker_prelaunch_error_is_written_to_selected_run_log(tmp_path):
    task_dir = str(tmp_path)
    save_task_info(
        task_dir,
        {
            "name": "InvalidEnvTask",
            "status": "running",
            "task_kind": TASK_KIND_SHELL,
            "config_file": SHELL_CONFIG_FILENAME,
            "cmd": [sys.executable, "-c", "print('unreachable')"],
            "run_index": 1,
        },
    )

    result = run_task_worker(
        task_dir=task_dir,
        name="InvalidEnvTask",
        created_at="now",
        config={},
        env_vars={"BAD=KEY": "x"},
        run_index=1,
    )

    assert result["status"] == "failed"
    assert load_task_info(task_dir)["run_statuses"] == ["failed"]
    run_log = Path(task_dir, RUN_LOGS_DIR, "run1.log")
    assert run_log.is_file()
    assert "invalid environment variable name" in run_log.read_text(encoding="utf-8")


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
    assert load_task_info(task_dir)["run_statuses"] == ["completed"]
    log_path = os.path.join(task_dir, RUN_LOGS_DIR, "run1.log")
    content = Path(log_path).read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    assert "last output without newline\n[PYRUNS] ==================== FINISH" in content
    assert "last output without newline[PYRUNS]" not in content


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_preserves_tqdm_carriage_return_stream_and_emit_offsets(
    mock_popen,
    mock_emit,
    mock_detect,
    tmp_path,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w") as f:
        json.dump({"name": "ProgressTask", "script": "script.py", "status": "queued"}, f)

    progress_chunk = b"\r  0%|          | 0/2 [00:00<?, ?it/s]\r                                         \r\n\r 50%|#####     | 1/2 [00:01<00:01,  1.00s/it]"
    mock_proc = MagicMock()
    mock_proc.pid = 7778
    mock_proc.wait.return_value = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[progress_chunk, b""])
    mock_popen.return_value = mock_proc

    with patch("pyruns.core.executor._build_run_source_state", return_value=""):
        result = run_task_worker(
            task_dir=task_dir,
            name="ProgressTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert result["status"] == "completed"
    log_path = Path(task_dir) / RUN_LOGS_DIR / "run1.log"
    log_bytes = log_path.read_bytes()
    assert progress_chunk in log_bytes
    assert b"\r                                         \r\n\r 50%" in log_bytes

    progress_text = progress_chunk.decode("utf-8")
    progress_call = next(call for call in mock_emit.call_args_list if call.args[1] == progress_text)
    assert progress_call.kwargs["offset"] == log_bytes.index(progress_chunk) + len(progress_chunk)


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
    error_text = error_log.read_text(encoding="utf-8")
    assert "Internal error during run #1" in error_text
    assert "Traceback:" in error_text
    assert "OSError: spawn failed" in error_text
    run_text = (task_dir / RUN_LOGS_DIR / "run1.log").read_text(encoding="utf-8")
    assert "[PYRUNS] -------------------- ERROR --------------------" in run_text
    assert "Traceback (most recent call last):" in run_text
    assert "OSError: spawn failed" in run_text
    assert "command=" not in run_text
    assert "task_dir=" not in run_text
    assert cleanup_path.exists()


def test_run_task_worker_missing_argv_command_retries_through_workspace_shell(
    tmp_path,
    monkeypatch,
):
    import pyruns.core.executor as executor

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "MissingCommand",
            "status": "queued",
            "task_kind": TASK_KIND_SHELL,
            "command_mode": "argv",
            "cmd": ["ls"],
            "workdir": str(tmp_path),
            "start_times": [],
            "finish_times": [],
            "pids": [],
        },
    )
    shell_command = ["workspace-shell", "payload"]
    monkeypatch.setattr(
        executor,
        "_build_shell_command",
        lambda *args, **kwargs: (shell_command, str(tmp_path), []),
    )
    mock_proc = MagicMock()
    mock_proc.pid = 9874
    mock_proc.stdout.read1 = MagicMock(
        side_effect=[b"original shell error\n", b""]
    )
    mock_proc.wait.return_value = 1
    mock_proc.returncode = 1
    popen_commands = []

    def fake_popen(command, *args, **kwargs):
        popen_commands.append(command)
        if len(popen_commands) == 1:
            raise FileNotFoundError(2, "executable not found", "ls")
        return mock_proc

    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(executor, "get_process_create_time", lambda _pid: None)
    monkeypatch.setattr(executor, "_build_run_source_state", lambda **kwargs: {})

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="MissingCommand",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["status"] == "failed"
    assert result["exit_code"] == 1
    assert popen_commands == [["ls"], shell_command]
    run_text = (task_dir / RUN_LOGS_DIR / "run1.log").read_text(encoding="utf-8")
    assert "original shell error\n" in run_text
    assert "Command:" not in run_text
    assert "Hint:" not in run_text
    assert "Full details:" not in run_text

    error_text = (task_dir / RUN_LOGS_DIR / ERROR_LOG_FILENAME).read_text(
        encoding="utf-8"
    )
    assert "Run #1 failed" in error_text
    assert "reason=exit_code 1" in error_text


def test_run_task_worker_rejects_a_missing_persisted_workdir_without_spawning(tmp_path, monkeypatch):
    import pyruns.core.executor as executor

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    missing_workdir = tmp_path / "removed-project"
    save_task_info(
        str(task_dir),
        {
            "name": "MissingWorkdir",
            "status": "queued",
            "task_kind": TASK_KIND_SHELL,
            "cmd": [sys.executable, "-c", "print('must not run')"],
            "workdir": str(missing_workdir),
            "start_times": [],
            "finish_times": [],
            "pids": [],
        },
    )
    monkeypatch.setattr(
        executor.subprocess,
        "Popen",
        lambda *args, **kwargs: pytest.fail("process should not start for a missing stored workdir"),
    )

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="MissingWorkdir",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["status"] == "failed"
    assert "Stored working directory is unavailable" in result["error"]
    assert str(missing_workdir) in result["error"]
    assert "Stored working directory is unavailable" in (
        task_dir / RUN_LOGS_DIR / "run1.log"
    ).read_text(encoding="utf-8")


def test_run_task_worker_kills_started_process_after_internal_error(tmp_path, monkeypatch):
    import pyruns.core.executor as executor
    from pyruns.utils.info_io import load_task_info

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script = tmp_path / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    save_task_info(
        str(task_dir),
        {
            "name": "StartedTask",
            "status": "queued",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "script": str(script),
            "start_times": [],
            "finish_times": [],
            "pids": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {})

    mock_proc = MagicMock()
    mock_proc.pid = 9876
    mock_proc.poll.side_effect = [None, 0]
    mock_proc.stdout.read1 = MagicMock(side_effect=[b""])
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: mock_proc)
    monkeypatch.setattr(
        executor,
        "_build_command",
        lambda *args, **kwargs: ([sys.executable, "-c", "print('ok')"], str(tmp_path), []),
    )

    original_update = executor.update_task_info
    update_calls = {"count": 0}

    def flaky_update(*args, **kwargs):
        update_calls["count"] += 1
        if update_calls["count"] == 1:
            raise RuntimeError("task info update failed")
        return original_update(*args, **kwargs)

    captured_create_times = []
    killed = []
    monkeypatch.setattr(executor, "update_task_info", flaky_update)
    monkeypatch.setattr(
        executor,
        "get_process_create_time",
        lambda pid: captured_create_times.append(pid) or 1000.0,
    )
    monkeypatch.setattr(
        executor,
        "kill_process",
        lambda pid, expected_create_time=None: killed.append((pid, expected_create_time)) or True,
    )

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="StartedTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["status"] == "failed"
    assert "task info update failed" in result["error"]
    assert captured_create_times == [9876]
    assert killed == [(9876, 1000.0)]
    info = load_task_info(str(task_dir))
    assert info["status"] == "failed"
    error_log = task_dir / RUN_LOGS_DIR / ERROR_LOG_FILENAME
    error_text = error_log.read_text(encoding="utf-8")
    assert "Internal error during run #1" in error_text
    assert "child_process_terminated=True" in error_text


def test_terminate_started_process_uses_owned_popen_when_create_time_is_unavailable(monkeypatch):
    mock_proc = MagicMock()
    mock_proc.pid = 9875
    mock_proc.poll.side_effect = [None, 0]
    killed = []
    monkeypatch.setattr(
        executor,
        "kill_process",
        lambda pid, expected_create_time=None: killed.append((pid, expected_create_time)) or True,
    )

    terminated = executor._terminate_started_process(
        mock_proc,
        expected_create_time=None,
        task_name="OwnedProcessTask",
        run_index=1,
    )

    assert terminated is True
    assert killed == [(9875, None)]
    mock_proc.wait.assert_called_once_with(timeout=1)


def test_run_task_worker_pending_stop_before_process_start_skips_popen(tmp_path, monkeypatch):
    import pyruns.core.executor as executor
    from pyruns.utils.info_io import load_task_info

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "PreStopTask",
            "status": "running",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "_pending_stop_summary": {
                "run_index": 1,
                "event": "stopped",
                "reason": "cancelled_by_user",
                "detail_lines": ["previous_status=running"],
            },
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {})
    monkeypatch.setattr(executor, "_build_command", lambda *args, **kwargs: pytest.fail("command should not be built"))
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("process should not start"))

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="PreStopTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["status"] == "cancelled"
    info = load_task_info(str(task_dir))
    assert info["status"] == "cancelled"
    assert info["progress"] == 0.0
    assert info["finish_times"][0]
    assert info["durations"] == [None]
    assert info["exit_codes"] == [None]
    assert "_pending_stop_summary" not in info
    error_text = (task_dir / RUN_LOGS_DIR / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "Run #1 stopped" in error_text
    assert "process_started=False" in error_text
    assert not (task_dir / RUN_LOGS_DIR / "run1.log").exists()


def test_run_task_worker_pending_stop_after_popen_kills_child_before_pid_persist(tmp_path, monkeypatch):
    import pyruns.core.executor as executor
    from pyruns.utils.info_io import load_task_info

    task_dir = tmp_path / "task"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "PostPopenStopTask",
            "status": "running",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": [],
            "finish_times": [],
            "pids": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {})
    monkeypatch.setattr(
        executor,
        "_build_command",
        lambda *args, **kwargs: ([sys.executable, "-c", "print('ok')"], str(tmp_path), []),
    )

    mock_proc = MagicMock()
    mock_proc.pid = 9877
    mock_proc.poll.side_effect = [None, 0]
    mock_proc.stdout.read1 = MagicMock(side_effect=[b""])

    def fake_popen(*args, **kwargs):
        update_task_info(
            str(task_dir),
            lambda info: info.update({
                "_pending_stop_summary": {
                    "run_index": 1,
                    "event": "stopped",
                    "reason": "cancelled_by_user",
                    "detail_lines": ["previous_status=running"],
                },
            }),
        )
        return mock_proc

    killed = []
    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(executor, "get_process_create_time", lambda _pid: 1000.0)
    monkeypatch.setattr(
        executor,
        "kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="PostPopenStopTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert result["status"] == "cancelled"
    assert killed == [9877]
    mock_proc.wait.assert_called_once_with(timeout=1)
    info = load_task_info(str(task_dir))
    assert info["status"] == "cancelled"
    assert info["progress"] == 0.0
    assert info["pids"][0] == 9877
    assert info["durations"][0] >= 0
    assert info["exit_codes"] == [None]
    assert "_pending_stop_summary" not in info
    error_text = (task_dir / RUN_LOGS_DIR / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "Run #1 stopped" in error_text
    assert "process_started=True" in error_text
    assert "process_terminated=True" in error_text
    assert not (task_dir / RUN_LOGS_DIR / "run1.log").exists()


def test_run_task_worker_stops_process_when_ownership_changes_during_launch(
    tmp_path,
    monkeypatch,
):
    task_dir = tmp_path / "task"
    task_dir.mkdir()
    script = tmp_path / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    save_task_info(
        str(task_dir),
        {
            "name": "OwnershipRaceTask",
            "status": "running",
            "progress": 0.0,
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "script": str(script),
            "run_index": 1,
            "runner_id": "runner-old",
            "runner_host": "host-old",
            "start_times": [""],
            "finish_times": [""],
            "run_statuses": ["running"],
            "pids": [None],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {})
    monkeypatch.setattr(
        executor,
        "_build_command",
        lambda *args, **kwargs: ([sys.executable, "-c", "print('old')"], str(tmp_path), []),
    )

    mock_proc = MagicMock()
    mock_proc.pid = 9878
    mock_proc.poll.side_effect = [None, 0]
    mock_proc.stdout.read1 = MagicMock(side_effect=[b""])

    def fake_popen(*args, **kwargs):
        def replace_owner(info):
            ensure_run_slot(info, 2)
            info["status"] = "running"
            info["progress"] = 0.5
            info["run_index"] = 2
            info["runner_id"] = "runner-new"
            info["runner_host"] = "host-new"
            info["run_statuses"] = ["failed", "running"]

        update_task_info(str(task_dir), replace_owner)
        return mock_proc

    killed = []
    monkeypatch.setattr(executor.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(executor, "get_process_create_time", lambda _pid: 1000.0)
    monkeypatch.setattr(
        executor,
        "kill_process",
        lambda pid, expected_create_time=None: killed.append((pid, expected_create_time)) or True,
    )

    result = executor.run_task_worker(
        task_dir=str(task_dir),
        name="OwnershipRaceTask",
        created_at="now",
        config={},
        run_index=1,
        runner_id="runner-old",
        runner_host="host-old",
    )

    assert result["status"] == "failed"
    assert result["error"] == "task ownership changed after process launch"
    assert result["child_process_terminated"] is True
    assert killed == [(9878, 1000.0)]
    mock_proc.wait.assert_called_once_with(timeout=1)
    final_info = load_task_info(str(task_dir))
    assert final_info["status"] == "running"
    assert final_info["progress"] == 0.5
    assert final_info["run_index"] == 2
    assert final_info["runner_id"] == "runner-new"
    assert final_info["run_statuses"] == ["failed", "running"]


def test_task_manager_uses_explicit_runner_token(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    manager = _make_task_manager(tasks_dir, runner_token="submission-token")

    assert manager.runner_id.rsplit(":", 1)[-1] == "submission-token"
    manager.shutdown()

def test_task_manager_start_batch_tasks_uses_available_slots_immediately(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(5)]

    manager = _make_task_manager(tasks_dir)

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent):
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
    assert queued_info["run_index"] == 0
    assert "_queued_run_index" not in queued_info


def test_task_manager_sync_status_does_not_revive_cancelled_queued_task(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("race-cancel", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        current = manager._tasks_by_name[task["name"]]
        current["status"] = "queued"
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    assert manager.cancel_task(task["name"]) is True
    assert manager._sync_status_to_disk(
        task["name"],
        "queued",
        run_index=1,
        expected_statuses={"pending"},
    ) is False

    info = load_task_info(task["dir"])
    assert info["status"] == "cancelled"
    assert manager.get_task(task["name"])["status"] == "cancelled"


def test_task_manager_submit_after_delete_does_not_recreate_or_execute(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("delete-race", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    manager = _make_task_manager(tasks_dir)

    picked, run_index = manager._pick_queued_task()
    assert picked is not None
    assert picked["name"] == task["name"]

    submitted: list[str] = []

    class CapturingExecutor:
        def __init__(self, max_workers=None):
            self.max_workers = max_workers

        def submit(self, *args, **kwargs):
            submitted.append(args[2])
            return Future()

        def shutdown(self, **kwargs):
            pass

    monkeypatch.setattr(task_manager_module, "ThreadPoolExecutor", CapturingExecutor)

    assert manager.delete_tasks([task["name"]]) == [task["name"]]
    assert not Path(task["dir"]).exists()

    manager._submit_task(picked, run_index, independent=False)

    assert submitted == []
    assert not Path(task["dir"]).exists()
    assert manager.get_task(task["name"]) is None


def test_task_manager_expired_lease_with_live_pid_is_failed_not_killed(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "expired"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "expired",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [os.getpid()],
            "records": [],
            "tracks": [],
            "runner_id": "old-runner",
            "runner_host": socket.gethostname().lower(),
            "lease_until": time.time() - 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    killed: list[int] = []
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )

    manager = _make_task_manager(tasks_dir)

    assert manager.get_task("expired")["status"] == "failed"
    assert manager.cancel_task("expired") is False
    assert killed == []


def test_task_manager_does_not_fail_runner_that_renews_during_stale_reconciliation(
    tmp_path,
    monkeypatch,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "renewed"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "renewed",
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
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() - 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir, lazy_scan=None)

    stale_info = load_task_info(str(task_dir))
    mark_failed = manager._mark_failed_on_disk

    def renew_then_mark(task, **kwargs):
        update_task_info(
            str(task_dir),
            lambda info: info.update({"lease_until": time.time() + 60}),
        )
        return mark_failed(task, **kwargs)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", renew_then_mark)

    updated, changed = manager._fail_unowned_running_info_if_needed(
        "renewed",
        str(task_dir),
        stale_info,
    )

    assert changed is False
    assert updated["status"] == "running"
    assert updated["lease_until"] > time.time()
    assert load_task_info(str(task_dir))["status"] == "running"


def test_task_manager_start_task_now_skips_active_task(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("runner", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        active = manager._tasks_by_name[task["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._mark_running_locked(task["name"], counts_for_batch=True)
        manager._recompute_processing_flag_locked()

    submitted: list[str] = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent: submitted.append(target["name"]),
    )

    manager.start_task_now(task["name"])

    assert submitted == []
    refreshed = manager.get_task(task["name"])
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 1


def test_task_manager_plain_queued_pick_computes_next_run_from_history(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("plain-queued-history", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 2,
            "start_times": ["2026-01-01_00-00-00", "2026-01-01_00-00-02"],
            "finish_times": ["2026-01-01_00-00-01", "2026-01-01_00-00-03"],
        }),
    )

    manager = _make_task_manager(tasks_dir)

    picked, run_index = manager._pick_queued_task()

    assert picked is not None
    assert picked["name"] == task["name"]
    assert run_index == 3
    assert picked["run_index"] == 3


def test_task_manager_start_batch_tasks_skips_active_tasks(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"task-{idx}", {"value": idx}) for idx in range(3)]

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        active = manager._tasks_by_name[tasks[0]["name"]]
        active["status"] = "running"
        active["run_index"] = 1
        manager._mark_running_locked(active["name"], counts_for_batch=True)
        manager._recompute_processing_flag_locked()

    submitted: list[tuple[str, int, bool]] = []

    def fake_submit(target, run_index, *, independent):
        submitted.append((target["name"], run_index, independent))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=2)

    assert [item[0] for item in submitted] == [tasks[1]["name"]]
    statuses = {task["name"]: task["status"] for task in manager.list_tasks()}
    assert statuses[tasks[0]["name"]] == "running"
    assert statuses[tasks[1]["name"]] == "running"
    assert statuses[tasks[2]["name"]] == "queued"
    assert manager.get_task(tasks[0]["name"])["run_index"] == 1
    queued_info = json.loads((Path(tasks[2]["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert queued_info["run_index"] == 0
    assert "_queued_run_index" not in queued_info


def test_task_manager_run_now_does_not_consume_batch_slots(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    run_now = generator.create_task("run-now", {"value": 1})
    batch = generator.create_task("batch", {"value": 2})

    class CapturingExecutor:
        instances = []

        def __init__(self, max_workers=None):
            self.max_workers = max_workers
            self.submitted = []
            CapturingExecutor.instances.append(self)

        def submit(self, *args, **kwargs):
            self.submitted.append((args, kwargs))
            return Future()

        def shutdown(self, **kwargs):
            pass

    manager = _make_task_manager(tasks_dir)

    monkeypatch.setattr(task_manager_module, "ThreadPoolExecutor", CapturingExecutor)

    manager.start_task_now(run_now["name"])
    assert run_now["name"] in manager._running_ids
    assert run_now["name"] not in manager._batch_running_ids

    manager.start_batch_tasks([batch["name"]], max_workers=1)

    assert manager.get_task(batch["name"])["status"] == "running"
    assert batch["name"] in manager._running_ids
    assert batch["name"] in manager._batch_running_ids
    submitted_names = [
        args[2]
        for executor in CapturingExecutor.instances
        for args, _kwargs in executor.submitted
    ]
    assert submitted_names == ["run-now", "batch"]


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

    manager = _make_task_manager(tasks_dir)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))

    manager.start_batch_tasks([task["name"]], max_workers=1)

    assert submitted == []
    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert queued["run_index"] == 0
    assert "_queued_run_index" not in queued
    queued_info = json.loads((Path(task["dir"]) / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert queued_info["status"] == "queued"
    assert queued_info["run_index"] == 0
    assert "_queued_run_index" not in queued_info
    queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
    text = queue_log.read_text(encoding="utf-8")
    assert "[PYRUNS] [GPU WAIT] Run #1 waiting for GPU resources" in text
    assert "[PYRUNS]   Run log: run1.log" in text
    assert "max wait=24h" in text


def test_task_manager_gpu_batch_run_waits_each_selected_task(tmp_path, monkeypatch):
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
                "gpu_scheduler_stable_seconds: 1",
                "gpu_scheduler_max_wait_seconds: 86400",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    tasks = [generator.create_task(f"gpu-batch-{idx}", {"lr": idx}) for idx in range(3)]

    manager = _make_task_manager(tasks_dir)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))

    manager.start_batch_tasks([task["name"] for task in tasks], max_workers=3)

    assert submitted == []
    assert manager.max_workers == 3
    for task in tasks:
        queued = manager.get_task(task["name"])
        assert queued["status"] == "queued"
        assert queued["run_index"] == 0
        queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
        queue_text = queue_log.read_text(encoding="utf-8")
        assert "[PYRUNS] [GPU WAIT] Run #1 waiting for GPU resources" in queue_text
        assert "[PYRUNS]   Run log: run1.log" in queue_text

    now = time.monotonic()
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 36000, 40960, 1)]),
        clock=lambda: now,
    )
    with manager._lock:
        for task in tasks:
            current = manager._tasks_by_name[task["name"]]
            current["_gpu_last_wait_log_at"] = 0.0

    target, _ = manager._pick_queued_task()

    assert target is None
    for task in tasks:
        queue_bytes = (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").read_bytes()
        assert b"\r[PYRUNS] Run #1 still waiting after " in queue_bytes
        assert b"blocked: GPU 0 memory" in queue_bytes


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

    manager = _make_task_manager(tasks_dir)

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
    assert target["_scheduled_env"]["PYRUNS_ASSIGNED_GPUS"] == "0,1"
    assert target["_gpu_assignment"]["run_index"] == run_index
    assert target["_gpu_assignment"]["gpu_ids"] == [0, 1]
    assert target["_gpu_assignment"]["env"] == {
        "PYRUNS_ASSIGNED_GPUS": "0,1",
        "CUDA_VISIBLE_DEVICES": "0,1",
    }
    queue_log = Path(task["dir"]) / RUN_LOGS_DIR / "queue.log"
    text = queue_log.read_text(encoding="utf-8")
    assert "[PYRUNS] [GPU ASSIGNED] Run #1 assigned GPUs 0,1" in text
    assert "Run log: run1.log" in text
    assert "CUDA_VISIBLE_DEVICES=0,1" in text
    assert "PYRUNS_ASSIGNED_GPUS=0,1" in text
    assert "Updated at " in text
    assert "Last status at " not in text


def test_task_manager_gpu_scheduler_respects_foreign_running_assignment(tmp_path):
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
                "gpu_scheduler_max_tasks_per_gpu: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    remote = generator.create_task("remote-gpu", {"lr": 0.1})
    local = generator.create_task("local-gpu", {"lr": 0.2})
    update_task_info(
        remote["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 1,
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [12345],
            "_scheduled_env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
            "_gpu_assignment": {
                "task_name": "remote-gpu",
                "run_index": 1,
                "gpu_ids": [0],
                "cuda_visible_devices": "0",
                "env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
                "waited_seconds": 0,
            },
        }),
    )
    update_task_info(local["dir"], lambda info: info.update({"status": "queued"}))

    manager = _make_task_manager(tasks_dir)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, _ = manager._pick_queued_task()

    assert target is None
    queued = manager.get_task("local-gpu")
    assert queued["status"] == "queued"
    queue_log = Path(local["dir"]) / RUN_LOGS_DIR / "queue.log"
    assert "GPU 0 reserved (1/1)" in queue_log.read_text(encoding="utf-8")


def test_task_manager_gpu_scheduler_respects_undiscovered_foreign_assignment(tmp_path):
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
                "gpu_scheduler_max_tasks_per_gpu: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    local = generator.create_task("local-gpu", {"lr": 0.2})

    manager = _make_task_manager(tasks_dir)

    remote = generator.create_task("remote-gpu", {"lr": 0.1})
    update_task_info(
        remote["dir"],
        lambda info: info.update({
            "status": "running",
            "run_index": 1,
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_until": time.time() + 60,
            "pids": [12345],
            "_scheduled_env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
            "_gpu_assignment": {
                "task_name": "remote-gpu",
                "run_index": 1,
                "gpu_ids": [0],
                "cuda_visible_devices": "0",
                "env": {"CUDA_VISIBLE_DEVICES": "0", "PYRUNS_ASSIGNED_GPUS": "0"},
                "waited_seconds": 0,
            },
        }),
    )
    update_task_info(local["dir"], lambda info: info.update({"status": "queued"}))
    manager.refresh_from_disk(task_ids=["local-gpu"], force_all=True)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, _ = manager._pick_queued_task()

    assert [task["name"] for task in manager.tasks] == ["local-gpu"]
    assert target is None
    queued = manager.get_task("local-gpu")
    assert queued["status"] == "queued"
    queue_log = Path(local["dir"]) / RUN_LOGS_DIR / "queue.log"
    assert "GPU 0 reserved (1/1)" in queue_log.read_text(encoding="utf-8")


def test_task_manager_gpu_claim_failure_restores_disk_state(tmp_path, monkeypatch):
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
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("claim-race", {"lr": 0.1})
    update_task_info(task["dir"], lambda info: info.update({"status": "queued"}))

    manager = _make_task_manager(tasks_dir)

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 2048, 40960, 1)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    monkeypatch.setattr(manager, "_claim_task_for_run", lambda *args, **kwargs: None)

    target, _ = manager._pick_queued_task()

    assert target is None
    refreshed = manager.get_task("claim-race")
    assert refreshed["status"] == "queued"
    assert "_scheduled_env" not in refreshed
    assert "_gpu_assignment" not in refreshed
    assert "claim-race" not in manager._running_ids


def test_task_manager_gpu_wait_does_not_advance_public_run_index_until_assignment(tmp_path):
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
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-public-index", {"lr": 0.1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "completed",
            "run_index": 1,
            "start_times": ["2026-01-01_00-00-00"],
            "finish_times": ["2026-01-01_00-00-01"],
        }),
    )

    manager = _make_task_manager(tasks_dir)

    manager.start_batch_tasks([task["name"]], max_workers=1)

    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert queued["run_index"] == 1
    assert "_queued_run_index" not in queued
    queued_info = load_task_info(task["dir"])
    assert queued_info["status"] == "queued"
    assert queued_info["run_index"] == 1
    assert "_queued_run_index" not in queued_info

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 2
    assert target["run_index"] == 2
    assert "_queued_run_index" not in target


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

    manager = _make_task_manager(tasks_dir)

    manager.max_workers = 1
    manager._mark_running_locked("already-running", counts_for_batch=True)
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
    update_task_info(batch_task["dir"], lambda info: info.update({"status": "queued"}))
    update_task_info(run_now_task["dir"], lambda info: info.update({"status": "queued"}))

    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    target, run_index = manager._pick_queued_task(independent_only=True)

    assert target is not None
    assert target["name"] == "run-now"
    assert run_index == 1
    assert "run-now" in manager._running_ids
    assert "run-now" not in manager._batch_running_ids
    assert manager.get_task(batch_task["name"])["status"] == "queued"


def test_task_manager_gpu_independent_submit_does_not_consume_batch_slots(tmp_path, monkeypatch):
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
                "gpu_scheduler_max_tasks_per_gpu: 2",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    generator = TaskGenerator(root_dir=str(tasks_dir))
    batch_task = generator.create_task("batch", {"lr": 0.1})
    run_now_task = generator.create_task("run-now", {"lr": 0.2})

    class CapturingExecutor:
        def __init__(self, max_workers=None):
            self.max_workers = max_workers
            self.submitted = []

        def submit(self, *args, **kwargs):
            self.submitted.append((args, kwargs))
            return Future()

        def shutdown(self, **kwargs):
            pass

    manager = _make_task_manager(tasks_dir)

    monkeypatch.setattr(task_manager_module, "ThreadPoolExecutor", CapturingExecutor)
    manager.max_workers = 1
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
    update_task_info(batch_task["dir"], lambda info: info.update({"status": "queued"}))
    update_task_info(run_now_task["dir"], lambda info: info.update({"status": "queued"}))

    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0
    independent_target, run_index = manager._pick_queued_task(independent_only=True)
    assert independent_target is not None
    independent = bool(independent_target.pop("_queued_independent", False))
    manager._submit_task(independent_target, run_index, independent=independent)

    assert run_now_task["name"] in manager._running_ids
    assert run_now_task["name"] not in manager._batch_running_ids

    batch_target, batch_run_index = manager._pick_queued_task()

    assert batch_target is not None
    assert batch_target["name"] == batch_task["name"]
    assert batch_run_index == 1
    assert batch_task["name"] in manager._running_ids
    assert batch_task["name"] in manager._batch_running_ids


def test_task_manager_start_task_now_queues_gpu_task_as_independent(tmp_path, monkeypatch):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join([
            "gpu_scheduler_enabled: true",
            "gpu_scheduler_memory_used_pct: 99",
            "gpu_scheduler_min_free_memory_gb: 0.5",
            "gpu_scheduler_compute_used_pct: 30",
            "gpu_scheduler_stable_seconds: 1",
        ])
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("run-now-gpu", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    submitted = []
    monkeypatch.setattr(manager, "_submit_task", lambda *args, **kwargs: submitted.append((args, kwargs)))
    manager.start_task_now(task["name"])

    queued = manager.get_task(task["name"])
    assert submitted == []
    assert queued["status"] == "queued"
    assert queued["_queued_independent"] is True
    assert (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").exists()


def test_task_manager_clears_stale_gpu_schedule_env_before_plain_rerun(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-stale-env", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        target = manager._tasks_by_name[task["name"]]
        target["_scheduled_env"] = {"CUDA_VISIBLE_DEVICES": "7", "PYRUNS_ASSIGNED_GPUS": "7"}
        target["_gpu_assignment"] = {"gpu_ids": [7]}
        target["_gpu_wait_started_at"] = 1.0
        target["_gpu_last_wait_log_at"] = 1.0
        target["_queued_independent"] = True

    submitted = []

    def fake_submit(target, run_index, *, independent):
        submitted.append(dict(target))

    monkeypatch.setattr(manager, "_submit_task", fake_submit)
    manager.start_batch_tasks([task["name"]], max_workers=1)

    assert len(submitted) == 1
    assert "_scheduled_env" not in submitted[0]
    assert "_gpu_assignment" not in submitted[0]
    assert "_gpu_wait_started_at" not in submitted[0]
    assert "_gpu_last_wait_log_at" not in submitted[0]
    assert "_queued_independent" not in submitted[0]


@pytest.mark.parametrize("final_status", ["completed", "failed", "cancelled"])
def test_task_manager_plain_rerun_does_not_create_gpu_wait_state(tmp_path, final_status):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("plain-rerun", {"lr": 0.1})
    update_task_info(task["dir"], lambda info: info.update({"status": final_status, "run_index": 1}))

    manager = _make_task_manager(tasks_dir)

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

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        target = manager._tasks_by_name[task["name"]]
        target["status"] = "running"
        target["_scheduled_env"] = {"CUDA_VISIBLE_DEVICES": "0"}
        target["_gpu_assignment"] = {"gpu_ids": [0]}
        target["_queued_independent"] = True
        manager._mark_running_locked(task["name"], counts_for_batch=False)

    future = Future()
    future.set_result({"status": "completed"})
    manager._on_task_done(future, task["name"])

    refreshed = manager.get_task(task["name"])
    assert "_scheduled_env" not in refreshed
    assert "_gpu_assignment" not in refreshed
    assert "_queued_independent" not in refreshed
    assert task["name"] not in manager._running_ids
    assert task["name"] not in manager._batch_running_ids


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

    manager = _make_task_manager(tasks_dir)

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

    manager = _make_task_manager(tasks_dir)

    manager.start_batch_tasks([task["name"]], max_workers=1)
    with manager._lock:
        manager._tasks_by_name[task["name"]]["_gpu_wait_started_at"] = time.monotonic() - 10

    target, _ = manager._pick_queued_task()

    assert target is None
    assert manager.get_task(task["name"])["status"] == "failed"
    info = load_task_info(task["dir"])
    assert info["run_index"] == 0
    assert info["start_times"] == []
    assert info["finish_times"] == []
    log_dir = Path(task["dir"]) / RUN_LOGS_DIR
    queue_text = (log_dir / "queue.log").read_text(encoding="utf-8")
    error_text = (log_dir / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "[PYRUNS] [GPU WAIT TIMEOUT] Run #1 GPU wait timed out" in queue_text
    assert "max wait=1s" in queue_text
    assert "Queued task failed" in error_text
    assert "Run #1 failed" not in error_text
    assert "reason=gpu_wait_timeout" in error_text


def test_task_manager_gpu_wait_timeout_preserves_task_claimed_by_foreign_runner(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join(
            [
                "gpu_scheduler_enabled: true",
                "gpu_scheduler_stable_seconds: 15",
                "gpu_scheduler_max_wait_seconds: 1",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-timeout-race", {"lr": 0.1})

    manager = _make_task_manager(tasks_dir)

    manager.start_batch_tasks([task["name"]], max_workers=1)
    with manager._lock:
        manager._tasks_by_name[task["name"]]["_gpu_wait_started_at"] = time.monotonic() - 10

    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "run_index": 1,
                "runner_id": "other-host:4321:abcdef",
                "runner_host": "other-host",
                "lease_until": time.time() + 60,
                "pids": [4321],
            }
        ),
    )

    target, run_index = manager._pick_queued_task()

    assert target is None
    assert run_index == 1
    refreshed = manager.get_task(task["name"])
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 1
    assert refreshed["runner_id"] == "other-host:4321:abcdef"
    info = load_task_info(task["dir"])
    assert info["status"] == "running"
    assert info["runner_id"] == "other-host:4321:abcdef"
    queue_text = (Path(task["dir"]) / RUN_LOGS_DIR / "queue.log").read_text(encoding="utf-8")
    assert "GPU WAIT TIMEOUT" not in queue_text
    assert not (Path(task["dir"]) / RUN_LOGS_DIR / ERROR_LOG_FILENAME).exists()


def test_task_manager_queued_placeholder_run_slot_is_trimmed_before_next_assignment(tmp_path):
    workspace = tmp_path / DEFAULT_ROOT_NAME / "train"
    tasks_dir = workspace / TASKS_DIR
    tasks_dir.mkdir(parents=True)
    (tmp_path / DEFAULT_ROOT_NAME / "_pyruns_settings.yaml").write_text(
        "\n".join([
            "gpu_scheduler_enabled: true",
            "gpu_scheduler_memory_used_pct: 99",
            "gpu_scheduler_min_free_memory_gb: 0.5",
            "gpu_scheduler_compute_used_pct: 30",
            "gpu_scheduler_stable_seconds: 1",
        ])
        + "\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-placeholder", {"lr": 0.1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 2,
            "start_times": ["2026-01-01_00-00-00", ""],
            "finish_times": ["2026-01-01_00-00-01", ""],
            "pids": [123, None],
            "records": [{"loss": 1.0}, {}],
            "tracks": [{}, {}],
            "_queued_run_index": 2,
        }),
    )

    manager = _make_task_manager(tasks_dir)

    queued = manager.get_task(task["name"])
    assert queued["status"] == "queued"
    assert queued["run_index"] == 1
    assert queued["start_times"] == ["2026-01-01_00-00-00"]
    assert "_queued_run_index" not in queued

    now = [100.0]
    manager.gpu_scheduler = GpuResourceScheduler(
        provider=_StaticGpuProvider([GpuDevice(0, "A800", "GPU-0", 1024, 40960, 0)]),
        clock=lambda: now[0],
    )
    manager.gpu_scheduler.snapshot(manager._gpu_scheduler_config())
    now[0] += 1.0

    target, run_index = manager._pick_queued_task()

    assert target is not None
    assert run_index == 2
    assert target["run_index"] == 2
    assert target["_gpu_assignment"]["run_index"] == 2


def test_task_manager_cancel_queued_task_does_not_create_run_slot(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("queued-cancel", {"lr": 0.1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 1,
            "start_times": ["2026-01-01_00-00-00", ""],
            "finish_times": ["2026-01-01_00-00-01", ""],
            "pids": [123, None],
            "records": [{}, {}],
            "tracks": [{}, {}],
        }),
    )

    manager = _make_task_manager(tasks_dir)

    assert manager.cancel_task(task["name"]) is True

    info = load_task_info(task["dir"])
    assert info["status"] == "cancelled"
    assert info["run_index"] == 1
    assert info["start_times"] == ["2026-01-01_00-00-00"]
    assert info["finish_times"] == ["2026-01-01_00-00-01"]
    error_text = (Path(task["dir"]) / RUN_LOGS_DIR / ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "Queued task stopped" in error_text
    assert "Run #2 stopped" not in error_text
    assert "reason=cancelled_by_user" in error_text
    assert "previous_status=queued" in error_text


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
    assert "GPU CONTEXT" in text
    assert "[PYRUNS] GPU assignment: 0,1" in text
    assert "[PYRUNS] Run #1 uses GPU(s): 0,1" in text
    assert "[PYRUNS] Run log: run1.log" in text
    assert "[PYRUNS] PYRUNS_ASSIGNED_GPUS=0,1" in text
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


def test_task_manager_cancel_task_persists_reason_before_verified_termination(tmp_path, monkeypatch):
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

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    events = []
    original_persist = manager._persist_pending_stop_summary

    def record_persist(*args, **kwargs):
        events.append("persist")
        return original_persist(*args, **kwargs)

    monkeypatch.setattr(manager, "_persist_pending_stop_summary", record_persist)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: events.append(
            ("kill", pid, expected_create_time)
        ) or True,
    )

    assert manager.cancel_task("runner") is True

    info = json.loads((task_dir / TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert info["status"] == "running"
    assert info["_pending_stop_summary"]["reason"] == "cancelled_by_user"
    assert info["_pending_stop_summary"]["detail_lines"] == ["previous_status=running"]
    assert events == ["persist", ("kill", 12345, 1000.0)]


def test_task_manager_cancel_task_fails_closed_when_task_info_is_busy(tmp_path, monkeypatch):
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

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    killed = []
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append((pid, expected_create_time)) or True,
    )

    with patch("pyruns.core.task_manager.update_task_info", side_effect=TimeoutError("busy")):
        assert manager.cancel_task("runner") is False

    assert manager.get_task("runner")["status"] == "running"
    assert killed == []


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

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: True,
    )
    timeout_values = []

    def record_timeout(task_dir, updater, **kwargs):
        timeout_values.append(kwargs.get("timeout_sec"))
        raise TimeoutError("busy")

    with patch("pyruns.core.task_manager.update_task_info", side_effect=record_timeout):
        assert manager.cancel_task("runner") is False

    assert timeout_values == [task_manager_module._STOP_TASK_INFO_LOCK_TIMEOUT_SEC]


def test_task_manager_cancel_task_does_not_finalize_or_kill_a_reused_pid(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "reused"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "reused",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [12345],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "reused", task_dir)

    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda _pid, expected_create_time=None: False,
    )

    assert manager.cancel_task("reused") is False
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert "_pending_stop_summary" not in info


def test_kill_process_rejects_mismatched_creation_time_without_signalling(monkeypatch):
    from pyruns.utils import process_utils

    monkeypatch.setattr(
        process_utils,
        "process_identity_matches",
        lambda pid, expected_create_time: False,
    )
    monkeypatch.setattr(
        process_utils.subprocess,
        "run",
        lambda *_args, **_kwargs: pytest.fail("a reused PID must not be signalled"),
    )

    assert process_utils.kill_process(12345, expected_create_time=1000.0) is False


def test_task_manager_cancel_task_refreshes_completed_disk_state(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "race"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "race",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "status": "completed",
                "progress": 1.0,
                "start_times": ["2026-03-20_00-00-01"],
                "finish_times": ["2026-03-20_00-00-02"],
            }
        ),
    )

    assert manager.cancel_task("race") is False
    assert manager.get_task("race")["status"] == "completed"
    assert load_task_info(str(task_dir))["status"] == "completed"


def test_task_manager_cancel_foreign_live_runner_preserves_owner_and_gpu(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "foreign"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "foreign",
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
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_heartbeat": time.time(),
            "lease_until": time.time() + 60,
            "_gpu_assignment": {"device_ids": ["0"]},
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    assert manager.cancel_task("foreign") is False
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert info["runner_id"] == "other-host:123:abcdef"
    assert info["_gpu_assignment"] == {"device_ids": ["0"]}


@pytest.mark.parametrize(
    ("task_pid", "expected_killed"),
    [(12345, [12345]), (os.getpid(), [])],
)
def test_task_manager_shutdown_does_not_recreate_deleted_owned_task(
    tmp_path, monkeypatch, task_pid, expected_killed
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "owned"
    task_dir.mkdir()
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda _pid: True)
    monkeypatch.setattr(
        "pyruns.core.task_manager.process_identity_matches",
        lambda _pid, _created_at: True,
    )

    manager = _make_task_manager(tasks_dir, lazy_scan=None)

    save_task_info(
        str(task_dir),
        {
            "name": "owned",
            "status": "running",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [task_pid],
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
            "runner_id": manager.runner_id,
            "runner_host": manager.runner_host,
            "lease_heartbeat": time.time(),
            "lease_until": time.time() + 60,
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})
    manager.scan_disk()
    shutil.rmtree(task_dir)
    killed = []
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )

    manager.shutdown()

    assert killed == expected_killed
    assert not task_dir.exists()


def test_task_manager_delete_running_task_kills_outside_lock(tmp_path, monkeypatch):
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

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    lock_checks = []

    def fake_kill(pid, expected_create_time=None):
        acquired = manager._lock.acquire(blocking=False)
        lock_checks.append(acquired)
        if acquired:
            manager._lock.release()
        assert expected_create_time == 1000.0
        update_task_info(
            str(task_dir),
            lambda info: info.update({"status": "cancelled"}),
        )
        with manager._lock:
            manager._clear_running_locked("runner")
        return True

    monkeypatch.setattr("pyruns.core.task_manager.kill_process", fake_kill)

    assert manager.delete_tasks(["runner"]) == ["runner"]
    assert lock_checks == [True]


def test_task_manager_delete_running_task_stays_put_when_process_survives(tmp_path, monkeypatch):
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
            "pid_create_times": [1000.0],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda _pid, expected_create_time=None: False,
    )

    assert manager.delete_tasks(["runner"]) == []
    assert task_dir.exists()
    assert not (tasks_dir / TRASH_DIR / "runner").exists()
    assert load_task_info(str(task_dir))["status"] == "running"


def test_task_manager_delete_active_task_fails_closed_when_task_info_is_busy(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "queued"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "queued",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda *args, **kwargs: (_ for _ in ()).throw(TimeoutError("busy")))

    assert manager.delete_tasks(["queued"]) == []
    assert manager.get_task("queued") is not None
    assert task_dir.exists()
    assert not (tasks_dir / TRASH_DIR / "queued").exists()


def test_task_manager_delete_completed_disk_state_does_not_mark_failed(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "done"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "done",
            "status": "queued",
            "created_at": "2026-03-20_00-00-00",
            "task_kind": TASK_KIND_CONFIG,
            "config_file": CONFIG_FILENAME,
            "run_index": 0,
            "start_times": [],
            "finish_times": [],
            "pids": [],
            "records": [],
            "tracks": [],
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    update_task_info(
        str(task_dir),
        lambda info: info.update(
            {
                "status": "completed",
                "progress": 1.0,
                "start_times": ["2026-03-20_00-00-01"],
                "finish_times": ["2026-03-20_00-00-02"],
            }
        ),
    )

    assert manager.delete_tasks(["done"]) == ["done"]
    moved_info = load_task_info(str(tasks_dir / TRASH_DIR / "done"))
    assert moved_info["status"] == "completed"
    assert manager.get_task("done") is None


def test_task_manager_delete_foreign_live_runner_preserves_task(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "foreign"
    task_dir.mkdir()
    save_task_info(
        str(task_dir),
        {
            "name": "foreign",
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
            "runner_id": "other-host:123:abcdef",
            "runner_host": "other-host",
            "lease_heartbeat": time.time(),
            "lease_until": time.time() + 60,
            "_gpu_assignment": {"device_ids": ["0"]},
        },
    )
    save_yaml(str(task_dir / CONFIG_FILENAME), {"lr": 0.01})

    manager = _make_task_manager(tasks_dir)

    assert manager.delete_tasks(["foreign"]) == []
    assert task_dir.exists()
    assert not (tasks_dir / TRASH_DIR / "foreign").exists()
    info = load_task_info(str(task_dir))
    assert info["status"] == "running"
    assert info["runner_id"] == "other-host:123:abcdef"
    assert info["_gpu_assignment"] == {"device_ids": ["0"]}
    assert manager.get_task("foreign")["status"] == "running"


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
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", running_dir, pids=[111, 222])

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
    manager = _make_task_manager(tasks_dir)

    manager.tasks = [{}, {"name": "missing-status"}, None]

    manager._cleanup_on_shutdown()

    assert manager.tasks == [{}, {"name": "missing-status"}, None]


def test_task_manager_shutdown_does_not_overwrite_newer_final_disk_status(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("finished-elsewhere", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        manager._tasks_by_name[task["name"]]["status"] = "running"
    update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))

    manager._cleanup_on_shutdown()

    assert load_task_info(task["dir"])["status"] == "completed"


def test_task_manager_shutdown_does_not_overwrite_new_run_claimed_during_cleanup(
    tmp_path,
    monkeypatch,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("reclaimed", {"value": 1})

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, task["name"], Path(task["dir"]))
    monkeypatch.setattr(manager, "_current_process_identity", lambda _info: (None, None))

    original_mark_failed = manager._mark_failed_on_disk

    def claim_new_run_before_terminal_write(task_ref, **kwargs):
        def _claim(info):
            info["status"] = "running"
            info["run_index"] = 2
            info["runner_id"] = "other-host:5252:new-run"
            info["runner_host"] = "other-host"
            info["lease_heartbeat"] = time.time()
            info["lease_until"] = time.time() + 60

        update_task_info(task["dir"], _claim)
        return original_mark_failed(task_ref, **kwargs)

    monkeypatch.setattr(manager, "_mark_failed_on_disk", claim_new_run_before_terminal_write)

    manager._cleanup_on_shutdown()

    info = load_task_info(task["dir"])
    assert info["status"] == "running"
    assert info["run_index"] == 2
    assert info["runner_id"] == "other-host:5252:new-run"


def test_foreign_queued_runner_lease_survives_observer_shutdown(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    first = generator.create_task("first", {"value": 1})
    second = generator.create_task("second", {"value": 2})

    with (
        patch.object(TaskManager, "_scheduler_loop", lambda self: None),
        patch.object(TaskManager, "_submit_task", lambda self, *args, **kwargs: None),
    ):
        owner = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)
        owner.start_batch_tasks([first["name"], second["name"]], max_workers=1)
        observer = TaskManager(tasks_dir=str(tasks_dir), lazy_scan=False)

    queued_info = load_task_info(second["dir"])
    assert queued_info["status"] == "queued"
    assert queued_info["runner_id"] == owner.runner_id

    observer._cleanup_on_shutdown()

    after = load_task_info(second["dir"])
    assert after["status"] == "queued"
    assert after["runner_id"] == owner.runner_id
    owner.shutdown()
    observer.shutdown()


def test_task_manager_shutdown_unregisters_atexit_callback(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    manager = _make_task_manager(tasks_dir)

    manager_ref = weakref.ref(manager)
    manager.shutdown()
    manager._scheduler_thread.join(timeout=1)
    del manager
    gc.collect()

    assert manager_ref() is None


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
            "config": {"lr": 0.1},
            "env": {"A": "1"},
            "start_times": ("s1",),
            "finish_times": ("f1",),
            "pids": (123,),
            "durations": (1.25,),
            "exit_codes": (0,),
            "source_states": ("git abc | clean | script abc",),
            "records": [{"loss": 0.1}],
            "tracks": [{"step": 1}],
        },
        summary=True,
    )
    assert summary["dir"] == "C:/tmp/task"
    assert summary["durations"] == [1.25]
    assert summary["exit_codes"] == [0]
    assert summary["source_states"] == ["git abc | clean | script abc"]
    assert summary["config"] == {}
    assert summary["records"] == []
    assert summary["tracks"] == []
    assert summary["env"] == {"A": "1"}


def test_task_manager_api_snapshots_stay_consistent_during_locked_gpu_updates(tmp_path, monkeypatch):
    manager = TaskManager(
        tasks_dir=str(tmp_path),
        lazy_scan=None,
        owns_task_lifecycle=False,
    )
    live_task = {
        "dir": str(tmp_path / "snapshot-race"),
        "name": "snapshot-race",
        "status": "queued",
        "progress": 0,
        "gpu_wait": {"generation": 0, "started_at": 0.0},
    }
    with manager._lock:
        manager.tasks = [live_task]
        manager._rebuild_indexes_locked()

    original_serialize_wait = TaskManager._serialized_gpu_wait
    sync: dict[str, threading.Event] = {}

    def pause_before_gpu_wait_copy(task):
        sync["entered"].set()
        if not sync["updated"].wait(2):
            raise AssertionError("GPU update did not complete while the API snapshot was serialized")
        return original_serialize_wait(task)

    monkeypatch.setattr(
        TaskManager,
        "_serialized_gpu_wait",
        staticmethod(pause_before_gpu_wait_copy),
    )

    def capture(call):
        with manager._lock:
            live_task["progress"] = 0
            live_task["gpu_wait"] = {"generation": 0, "started_at": 0.0}

        sync["entered"] = threading.Event()
        sync["updated"] = threading.Event()
        writer_errors: list[str] = []

        def update_gpu_wait():
            if not sync["entered"].wait(2):
                writer_errors.append("API serialization did not reach the GPU wait field")
                sync["updated"].set()
                return
            with manager._lock:
                live_task["progress"] = 1
                live_task["gpu_wait"] = {"generation": 1, "started_at": 0.0}
            sync["updated"].set()

        writer = threading.Thread(target=update_gpu_wait)
        writer.start()
        snapshot = call()
        writer.join(timeout=2)

        assert not writer.is_alive()
        assert writer_errors == []
        assert snapshot["progress"] == 0
        assert snapshot["gpu_wait"]["generation"] == 0

    capture(lambda: manager.list_tasks(summary=True)[0])
    capture(lambda: manager.get_task("snapshot-race"))


def test_task_manager_scan_and_load_task_dir_edge_cases(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    missing_info_dir = tasks_dir / "missing-info"
    missing_info_dir.mkdir()

    manager = _make_task_manager(tasks_dir)

    assert manager._load_task_dir("missing-info") is None

    empty_dir = tasks_dir / "empty-info"
    empty_dir.mkdir()
    (empty_dir / TASK_INFO_FILENAME).write_text("{}", encoding="utf-8")
    with patch("pyruns.core.task_manager.load_task_info", return_value={}):
        empty_task = manager._load_task_dir("empty-info")
    assert empty_task is not None
    assert empty_task["status"] == "failed"
    assert "metadata is empty" in empty_task["_load_error"].lower()

    with patch("pyruns.core.task_manager.load_task_info", side_effect=RuntimeError("bad info")):
        broken_task = manager._load_task_dir("empty-info")
    assert broken_task is not None
    assert broken_task["status"] == "failed"
    assert "bad info" in broken_task["_load_error"]

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

    manager = _make_task_manager(tasks_dir)

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

    manager = _make_task_manager(tasks_dir)

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

    manager = _make_task_manager(tasks_dir)

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

    manager = _make_task_manager(tasks_dir)

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("stale nfs handle")):
        assert manager.refresh_from_disk(check_all=True, discover=True) is False

    assert [task["name"] for task in manager.list_tasks()] == ["alpha"]


def test_task_manager_strict_refresh_fails_closed_on_disk_errors(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with patch("pyruns.core.task_manager.os.scandir", side_effect=OSError("stale nfs handle")):
        with pytest.raises(OSError, match="stale nfs handle"):
            manager.refresh_from_disk(
                force_all=True,
                discover=True,
                raise_on_error=True,
            )

    with patch(
        "pyruns.core.task_manager.load_task_info",
        side_effect=OSError("task metadata unavailable"),
    ):
        with pytest.raises(OSError, match="task metadata unavailable"):
            manager.refresh_from_disk(
                force_all=True,
                discover=True,
                raise_on_error=True,
            )

    broken_dir = tasks_dir / "broken"
    broken_dir.mkdir()
    (broken_dir / TASK_INFO_FILENAME).write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError):
        manager.refresh_from_disk(
            force_all=True,
            discover=True,
            raise_on_error=True,
        )

    (broken_dir / TASK_INFO_FILENAME).write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="metadata status is missing"):
        manager.refresh_from_disk(
            force_all=True,
            discover=True,
            raise_on_error=True,
        )

    (broken_dir / TASK_INFO_FILENAME).write_text(
        json.dumps({"status": "unknown"}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="metadata status is invalid"):
        manager.refresh_from_disk(
            force_all=True,
            discover=True,
            raise_on_error=True,
        )


def test_task_manager_pin_reorder_notes_env_and_rename_edges(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    beta = generator.create_task("beta", {"value": 2})

    manager = _make_task_manager(tasks_dir)

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

    assert manager.update_task_notes("missing", "x", "") == (False, "Task not found")
    assert manager.update_task_notes("alpha", "note", "") == (True, "note")
    assert manager.update_task_env("missing", {}, {}) == (False, "Task not found")
    assert manager.update_task_env("alpha", {"A": 1}, {}) == (True, {"A": "1"})
    with pytest.raises(TaskStateConflict, match="environment changed"):
        manager.update_task_env("alpha", {"B": "2"}, {})
    assert manager.update_task_env("alpha", {"B": "2"}, {"A": "1"}) == (True, {"B": "2"})
    assert manager.update_task_env("alpha", {"BAD=KEY": "x"}, {"B": "2"}) == (
        False,
        "invalid environment variable name: BAD=KEY",
    )
    assert manager.update_task_env("alpha", {"GOOD": "bad\x00value"}, {"B": "2"}) == (
        False,
        "environment variable 'GOOD' contains a null byte",
    )

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


def test_task_manager_reorder_rolls_back_partial_writes(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    alpha = generator.create_task("alpha", {"value": 1})
    beta = generator.create_task("beta", {"value": 2})

    manager = _make_task_manager(tasks_dir)

    write_count = 0

    def fail_second_write(task_dir, updater):
        nonlocal write_count
        write_count += 1
        if write_count == 2:
            raise OSError("shared filesystem write failed")
        return update_task_info(task_dir, updater)

    monkeypatch.setattr("pyruns.core.task_manager.update_task_info", fail_second_write)

    with pytest.raises(OSError, match="shared filesystem write failed"):
        manager.reorder_tasks([
            {"name": "beta", "pinned": True},
            {"name": "alpha", "pinned": False},
        ])

    assert write_count == 3
    for task in (alpha, beta):
        persisted = load_task_info(task["dir"], raise_error=True)
        assert "task_order" not in persisted
        assert persisted["pinned"] is False
        current = manager.get_task(task["name"])
        assert current["task_order"] is None
        assert current["pinned"] is False


def test_task_manager_reorder_serializes_concurrent_batches(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    for name in ("alpha", "beta", "gamma"):
        generator.create_task(name, {"value": name})

    manager_a = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager_b = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )

    real_update_task_info = update_task_info
    first_updates = threading.Barrier(2)
    calls: list[int] = []
    seen_threads: set[int] = set()
    calls_lock = threading.Lock()

    def coordinated_update(task_dir, updater):
        thread_id = threading.get_ident()
        with calls_lock:
            calls.append(thread_id)
            first_for_thread = thread_id not in seen_threads
            seen_threads.add(thread_id)
        if first_for_thread:
            try:
                first_updates.wait(timeout=0.2)
            except threading.BrokenBarrierError:
                pass
        return real_update_task_info(task_dir, updater)

    monkeypatch.setattr("pyruns.core.task_manager.update_task_info", coordinated_update)
    forward = [{"name": name} for name in ("alpha", "beta", "gamma")]
    reverse = [{"name": name} for name in ("gamma", "beta", "alpha")]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            pool.submit(manager_a.reorder_tasks, forward),
            pool.submit(manager_b.reorder_tasks, reverse),
        ]
        outcomes = [result.result(timeout=5) for result in results]

    assert all(ok for ok, _ in outcomes)
    assert len(calls) == 6
    assert len(set(calls)) == 2
    assert sum(left != right for left, right in zip(calls, calls[1:])) == 1

    persisted_order = tuple(
        load_task_info(str(tasks_dir / name), raise_error=True)["task_order"]
        for name in ("alpha", "beta", "gamma")
    )
    assert persisted_order in {(0, 1, 2), (2, 1, 0)}


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
    monkeypatch.setattr("pyruns.core.task_manager.is_pid_running", lambda pid: True)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    monkeypatch.setattr("pyruns.core.task_manager.get_now_str", lambda: "2026-03-20_00-00-02")
    monkeypatch.setattr("pyruns.core.task_manager.os.rename", lambda src, dst: (_ for _ in ()).throw(OSError("move failed")))
    monkeypatch.setattr("pyruns.core.task_manager.time.sleep", lambda delay: None)

    manager = _make_task_manager(tasks_dir)
    _mark_task_owned_by_manager(manager, "runner", task_dir)

    def settle(*_args, **_kwargs):
        update_task_info(
            str(task_dir),
            lambda info: info.update({"status": "cancelled"}),
        )
        with manager._lock:
            manager._clear_running_locked("runner")
        return load_task_info(str(task_dir))

    monkeypatch.setattr(manager, "_wait_for_task_settle", settle)

    manager.delete_tasks(["missing"])
    assert manager.get_task("runner") is not None

    deleted = manager.delete_tasks(["runner", "runner"])

    assert killed == [12345]
    assert deleted == []
    assert task_dir.exists()
    assert manager.get_task("runner")["status"] == "cancelled"


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

    manager = _make_task_manager(tasks_dir)

    assert manager.get_task("remote")["status"] == "running"


def test_task_manager_refresh_expires_foreign_runner_even_when_mtime_unchanged(tmp_path, monkeypatch):
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

    manager = _make_task_manager(tasks_dir)

    task = manager.get_task("remote")
    assert task["status"] == "running"
    original_mtime_ns = task["_mtime_ns"]

    update_task_info(str(task_dir), lambda info: info.update({"lease_until": time.time() - 60}))
    with manager._lock:
        manager._tasks_by_name["remote"]["_mtime_ns"] = (task_dir / TASK_INFO_FILENAME).stat().st_mtime_ns

    assert manager.refresh_from_disk() is True
    refreshed = manager.get_task("remote")
    assert refreshed["status"] == "failed"
    assert refreshed["_mtime_ns"] >= original_mtime_ns


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

    manager = _make_task_manager(tasks_dir)

    submitted = []

    class CapturingExecutor:
        def submit(self, *args, **kwargs):
            submitted.append((args, kwargs))

    manager._executor = CapturingExecutor()
    monkeypatch.setattr(manager, "_ensure_executor", lambda: None)
    with manager._lock:
        target = manager._tasks_by_name["alpha"]
        target["status"] = "queued"
        manager._mark_running_locked("alpha", counts_for_batch=True)

    manager._submit_task(target, 3, independent=False)

    assert submitted == []
    assert manager.get_task("alpha")["status"] == "running"
    assert "alpha" not in manager._running_ids
    assert "alpha" not in manager._batch_running_ids


def test_task_manager_start_batch_sync_conflict_keeps_foreign_runner_without_submit(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

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
        lambda target, run_index, *, independent: submitted.append(target["name"]),
    )

    claimed = manager.start_batch_tasks(["alpha"], max_workers=1)

    refreshed = manager.get_task("alpha")
    assert claimed == []
    assert submitted == []
    assert refreshed["status"] == "running"
    assert refreshed["run_index"] == 4
    assert refreshed["runner_id"] == "other-host:4321:abcdef"
    assert "alpha" not in manager._running_ids


def test_task_manager_expected_run_rejects_completed_race_without_run_two(
    tmp_path,
    monkeypatch,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("race", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    def complete_first_run(info):
        slot = ensure_run_slot(info, 1)
        info["status"] = "completed"
        info["start_times"][slot] = "2026-08-10_10-00-00"
        info["finish_times"][slot] = "2026-08-10_10-00-01"
        info["run_statuses"][slot] = "completed"
        info["exit_codes"][slot] = 0

    update_task_info(task["dir"], complete_first_run)
    submitted: list[tuple[str, int]] = []
    monkeypatch.setattr(
        manager,
        "_submit_task",
        lambda target, run_index, *, independent: submitted.append(
            (str(target["name"]), run_index)
        ),
    )

    for _ in range(2):
        assert manager.start_batch_tasks(
            ["race"],
            max_workers=1,
            expected_run_indices={"race": 1},
        ) == []

    info = load_task_info(task["dir"])
    assert submitted == []
    assert info["status"] == "completed"
    assert info["run_index"] == 1
    assert info["run_statuses"] == ["completed"]
    assert manager.get_task("race")["run_index"] == 1
    assert "race" not in manager._running_ids


def test_task_manager_gpu_queue_sync_conflict_skips_wait_log_and_clears_transient_state(tmp_path, monkeypatch):
    settings_root = tmp_path
    tasks_dir = settings_root / "tasks"
    tasks_dir.mkdir()
    (settings_root / "_pyruns_settings.yaml").write_text(
        "gpu_scheduler_enabled: true\ngpu_scheduler_stable_seconds: 1\n",
        encoding="utf-8",
    )
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("gpu-race", {"value": 1})

    manager = _make_task_manager(tasks_dir)

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

    manager = _make_task_manager(tasks_dir)

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


@pytest.mark.parametrize("entrypoint", ["batch", "start", "rerun"])
def test_task_manager_rejects_run_history_overflow_before_changing_state(
    tmp_path,
    entrypoint,
):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("history-full", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "completed",
            "run_index": MAX_RUN_HISTORY_SLOTS,
        }),
    )
    before = load_task_info(task["dir"], raise_error=True)

    manager = _make_task_manager(tasks_dir)

    with pytest.raises(ValueError, match="reached the run history limit"):
        if entrypoint == "batch":
            manager.start_batch_tasks([task["name"]])
        elif entrypoint == "start":
            manager.start_task_now(task["name"])
        else:
            manager.rerun_task(task["name"])

    assert load_task_info(task["dir"], raise_error=True) == before
    assert manager.get_task(task["name"])["status"] == "completed"


def test_task_manager_rejects_starts_after_shutdown(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("stopped", {"value": 1})
    update_task_info(task["dir"], lambda info: info.update({"status": "completed"}))

    manager = _make_task_manager(tasks_dir)

    manager.shutdown()
    monkeypatch.setattr(
        task_manager_module,
        "ThreadPoolExecutor",
        lambda *args, **kwargs: pytest.fail("executor must not be created after shutdown"),
    )

    assert manager.start_batch_tasks([task["name"]]) == []
    assert manager.start_task_now(task["name"]) is False
    assert manager.rerun_task(task["name"]) is False
    with pytest.raises(RuntimeError, match="shutting down"):
        manager._ensure_executor()
    assert manager._executor is None
    assert manager._independent_executor is None


def test_task_manager_shutdown_waits_for_start_lifecycle_section(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()

    manager = _make_task_manager(tasks_dir)

    start_entered = threading.Event()
    release_start = threading.Event()
    shutdown_entered = threading.Event()
    shutdown_done = threading.Event()

    def blocked_start(_task_id):
        start_entered.set()
        assert release_start.wait(timeout=2)
        return True

    monkeypatch.setattr(manager, "_start_task_now", blocked_start)
    start_thread = threading.Thread(target=manager.start_task_now, args=("alpha",))
    start_thread.start()
    assert start_entered.wait(timeout=1)

    def run_shutdown():
        shutdown_entered.set()
        manager.shutdown()
        shutdown_done.set()

    shutdown_thread = threading.Thread(target=run_shutdown)
    shutdown_thread.start()
    assert shutdown_entered.wait(timeout=1)
    assert manager._shutdown_event.is_set() is False
    assert shutdown_done.is_set() is False

    release_start.set()
    start_thread.join(timeout=2)
    shutdown_thread.join(timeout=2)

    assert start_thread.is_alive() is False
    assert shutdown_thread.is_alive() is False
    assert shutdown_done.is_set() is True
    assert manager._shutdown_event.is_set() is True


def test_task_manager_does_not_claim_or_queue_creation_rollback_tombstone(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("rolled-back", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "cancelled",
            "_creation_rollback": {"token": "creator-token"},
        }),
    )

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        current = manager._tasks_by_name[task["name"]]
        current["status"] = "queued"
    assert manager._claim_task_for_run(current, 1, counts_for_batch=True) is None

    with manager._lock:
        manager._tasks_by_name[task["name"]]["status"] = "pending"
    assert manager._sync_status_to_disk(
        task["name"],
        "queued",
        run_index=1,
        expected_statuses={"pending"},
    ) is False

    persisted = load_task_info(task["dir"], raise_error=True)
    assert persisted["status"] == "cancelled"
    assert persisted["_creation_rollback"] == {"token": "creator-token"}


def test_task_manager_delete_marker_blocks_concurrent_start(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    real_rename = os.rename
    start_results = []

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        deleting = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
        contender = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    def rename_after_start_attempt(source, destination):
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(task["dir"]):
            start_results.append(contender.start_task_now("alpha"))
            marked = load_task_info(task["dir"], raise_error=True)
            assert marked["_namespace_operation"]["kind"] == "delete"
        return real_rename(source, destination)

    monkeypatch.setattr("pyruns.core.task_manager.os.rename", rename_after_start_attempt)
    try:
        assert deleting.delete_tasks(["alpha"]) == ["alpha"]
        assert start_results == [False]
        trashed = load_task_info(str(tasks_dir / TRASH_DIR / "alpha"), raise_error=True)
        assert "_namespace_operation" not in trashed
    finally:
        deleting.shutdown()
        contender.shutdown()


def test_task_manager_rename_marker_blocks_concurrent_start(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    real_rename = os.rename
    start_results = []

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        renaming = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
        contender = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    def rename_after_start_attempt(source, destination):
        if os.path.normcase(os.path.abspath(source)) == os.path.normcase(task["dir"]):
            start_results.append(contender.start_task_now("alpha"))
            marked = load_task_info(task["dir"], raise_error=True)
            assert marked["_namespace_operation"]["kind"] == "rename"
        return real_rename(source, destination)

    monkeypatch.setattr("pyruns.core.task_manager.os.rename", rename_after_start_attempt)
    try:
        assert renaming.rename_task("alpha", "beta") == (True, "beta")
        assert start_results == [False]
        renamed = load_task_info(str(tasks_dir / "beta"), raise_error=True)
        assert renamed["name"] == "beta"
        assert "_namespace_operation" not in renamed
    finally:
        renaming.shutdown()
        contender.shutdown()


def test_task_manager_delete_rechecks_status_before_moving(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )

    real_begin = manager._begin_namespace_operation

    def begin_after_concurrent_rerun(*args, **kwargs):
        update_task_info(
            task["dir"],
            lambda info: info.update({"status": "queued", "run_index": 1}),
        )
        return real_begin(*args, **kwargs)

    monkeypatch.setattr(manager, "_begin_namespace_operation", begin_after_concurrent_rerun)
    try:
        assert manager.delete_tasks(["alpha"]) == []
        assert Path(task["dir"]).is_dir()
        assert load_task_info(task["dir"], raise_error=True)["status"] == "queued"
        assert not (tasks_dir / TRASH_DIR / "alpha").exists()
    finally:
        manager.shutdown()


def test_task_manager_delete_binds_stop_to_original_run(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "running",
                "run_index": 1,
                "pids": [12345],
                "pid_create_times": [1000.0],
                "runner_id": manager.runner_id,
                "runner_host": manager.runner_host,
                "lease_heartbeat": time.time(),
                "lease_until": time.time() + 60,
            }
        ),
    )
    manager.scan_disk()
    real_persist = manager._persist_pending_stop_summary

    def persist_after_concurrent_rerun(*args, **kwargs):
        update_task_info(
            task["dir"],
            lambda info: info.update(
                {
                    "status": "running",
                    "run_index": 2,
                    "runner_id": manager.runner_id,
                    "runner_host": manager.runner_host,
                    "lease_heartbeat": time.time(),
                    "lease_until": time.time() + 60,
                }
            ),
        )
        return real_persist(*args, **kwargs)

    killed = []
    monkeypatch.setattr(manager, "_persist_pending_stop_summary", persist_after_concurrent_rerun)
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    try:
        assert manager.delete_tasks(["alpha"]) == []
        persisted = load_task_info(task["dir"], raise_error=True)
        assert persisted["status"] == "running"
        assert persisted["run_index"] == 2
        assert killed == []
        assert not (tasks_dir / TRASH_DIR / "alpha").exists()
    finally:
        manager.shutdown()


def test_task_manager_discards_expired_namespace_marker_when_starting(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task = TaskGenerator(root_dir=str(tasks_dir)).create_task("alpha", {"value": 1})
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "_namespace_operation": {
                    "kind": "delete",
                    "token": "expired",
                    "host": socket.gethostname().lower(),
                    "pid": os.getpid(),
                    "pid_create_time": task_manager_module.get_process_create_time(os.getpid()),
                    "expires_at": time.time() - 1,
                }
            }
        ),
    )

    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(
            tasks_dir=str(tasks_dir),
            lazy_scan=False,
            owns_task_lifecycle=False,
        )
    try:
        assert manager._sync_status_to_disk(
            "alpha",
            "queued",
            run_index=1,
            expected_statuses={"pending"},
        ) is True
        persisted = load_task_info(task["dir"], raise_error=True)
        assert persisted["status"] == "queued"
        assert "_namespace_operation" not in persisted
    finally:
        manager.shutdown()


def test_task_manager_internal_executor_and_worker_error_paths(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    task = generator.create_task("alpha", {"value": 1})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        target = manager._tasks_by_name["alpha"]
        target["status"] = "queued"
        target["run_index"] = 2
        target["start_times"] = ["2026-01-01_00-00-00", "2026-01-01_00-00-02"]
        target["finish_times"] = ["2026-01-01_00-00-01", "2026-01-01_00-00-03"]
        manager._recompute_processing_flag_locked()
    update_task_info(
        task["dir"],
        lambda info: info.update({
            "status": "queued",
            "run_index": 2,
            "start_times": ["2026-01-01_00-00-00", "2026-01-01_00-00-02"],
            "finish_times": ["2026-01-01_00-00-01", "2026-01-01_00-00-03"],
        }),
    )

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
        manager._mark_running_locked("alpha", counts_for_batch=True)

    failed_marks = []
    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda task, **kwargs: failed_marks.append(kwargs))
    future = Future()
    future.set_exception(RuntimeError("worker failed"))
    manager._on_task_done(future, "alpha")

    assert failed_marks[0]["reason"] == "worker_exception"
    assert manager.get_task("alpha")["status"] == "failed"
    assert "alpha" not in manager._batch_running_ids


def test_task_manager_shutdown_retries_cleanup_before_unregistering_atexit(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager.owns_task_lifecycle = True
    manager._atexit_registered = True

    class RetryLock:
        def __init__(self, lock):
            self.lock = lock
            self.attempts = 0

        def acquire(self, *args, **kwargs):
            self.attempts += 1
            if self.attempts == 1:
                return False
            return self.lock.acquire(*args, **kwargs)

        def release(self):
            self.lock.release()

        def __enter__(self):
            self.lock.acquire()
            return self

        def __exit__(self, exc_type, exc, traceback):
            self.lock.release()

    retry_lock = RetryLock(manager._lock)
    manager._lock = retry_lock
    unregistered = []
    monkeypatch.setattr(task_manager_module.atexit, "unregister", unregistered.append)

    manager.shutdown()

    assert manager._shutdown_cleanup_done is False
    assert manager._atexit_registered is True
    assert unregistered == []

    manager.shutdown()

    assert retry_lock.attempts == 2
    assert manager._shutdown_cleanup_done is True
    assert manager._atexit_registered is False
    assert unregistered == [manager._atexit_callback]


def test_task_manager_shutdown_does_not_mark_in_progress_cleanup_complete(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager.owns_task_lifecycle = True
    manager._atexit_registered = True

    cleanup_started = threading.Event()
    release_cleanup = threading.Event()

    def blocking_cleanup():
        cleanup_started.set()
        assert release_cleanup.wait(timeout=2)
        return True

    unregistered = []
    monkeypatch.setattr(manager, "_perform_shutdown_cleanup", blocking_cleanup)
    monkeypatch.setattr(task_manager_module.atexit, "unregister", unregistered.append)

    first = threading.Thread(target=manager.shutdown)
    first.start()
    assert cleanup_started.wait(timeout=2)

    manager.shutdown()

    assert manager._shutdown_cleanup_in_progress is True
    assert manager._shutdown_cleanup_done is False
    assert manager._atexit_registered is True
    assert unregistered == []

    release_cleanup.set()
    first.join(timeout=2)
    assert not first.is_alive()
    assert manager._shutdown_cleanup_in_progress is False
    assert manager._shutdown_cleanup_done is True
    assert manager._atexit_registered is False
    assert unregistered == [manager._atexit_callback]


def test_task_manager_shutdown_keeps_failed_cleanup_retryable(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = TaskManager(
        tasks_dir=str(tasks_dir),
        lazy_scan=False,
        owns_task_lifecycle=False,
    )
    manager.owns_task_lifecycle = True
    manager._atexit_registered = True

    attempts = 0

    def flaky_cleanup():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("cleanup failed")
        return True

    unregistered = []
    monkeypatch.setattr(manager, "_perform_shutdown_cleanup", flaky_cleanup)
    monkeypatch.setattr(task_manager_module.atexit, "unregister", unregistered.append)

    manager.shutdown()

    assert manager._shutdown_cleanup_in_progress is False
    assert manager._shutdown_cleanup_done is False
    assert manager._atexit_registered is True
    assert unregistered == []

    manager.shutdown()

    assert attempts == 2
    assert manager._shutdown_cleanup_done is True
    assert manager._atexit_registered is False
    assert unregistered == [manager._atexit_callback]


def test_task_manager_scheduler_helpers_and_cleanup_edges(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("queued", {"value": 1})
    generator.create_task("running", {"value": 2})
    generator.create_task("remote", {"value": 3})

    manager = _make_task_manager(tasks_dir)

    with manager._lock:
        manager._tasks_by_name["queued"]["status"] = "queued"
        manager._tasks_by_name["queued"]["run_index"] = 1
        manager._tasks_by_name["queued"]["start_times"] = ["2026-01-01_00-00-00"]
        manager._tasks_by_name["queued"]["finish_times"] = ["2026-01-01_00-00-01"]
        manager._tasks_by_name["running"]["status"] = "running"
        manager._tasks_by_name["running"]["run_index"] = 1
        manager._mark_running_locked("running", counts_for_batch=True)
        manager._tasks_by_name["remote"]["status"] = "running"
        manager._tasks_by_name["remote"]["run_index"] = 1
        manager._mark_running_locked("remote", counts_for_batch=False)
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
    manager._executor_workers = 1
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
    monkeypatch.setattr(
        "pyruns.core.task_manager.kill_process",
        lambda pid, expected_create_time=None: killed.append(pid) or True,
    )
    monkeypatch.setattr(manager, "_current_process_identity", lambda info: (4321, 1000.0))
    monkeypatch.setattr(manager, "_mark_failed_on_disk", lambda task, **kwargs: task.update(cleaned=kwargs))

    manager._cleanup_on_shutdown()
    manager._cleanup_on_shutdown()

    assert killed == [4321, 4321]
    assert manager.get_task("running")["status"] == "failed"
    assert manager.get_task("remote")["status"] == "running"
    assert "running" not in manager._batch_running_ids
    assert manager._shutdown_cleanup_done is True


def test_task_manager_scan_async_and_disk_discovery_edge_paths(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    generator = TaskGenerator(root_dir=str(tasks_dir))
    generator.create_task("keep", {"value": 1})
    stale = generator.create_task("stale", {"value": 2})

    manager = _make_task_manager(tasks_dir)

    callbacks = []

    def callback():
        callbacks.append("changed")

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


def test_task_manager_rejects_symlinked_tasks_root(tmp_path):
    outside = tmp_path / "outside-tasks"
    outside.mkdir()
    tasks_link = tmp_path / "tasks"
    try:
        tasks_link.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="Tasks directory must not be"):
        TaskManager(tasks_dir=str(tasks_link), lazy_scan=None)


def test_log_writers_reject_symlinked_run_logs_directory(tmp_path):
    tasks_dir = tmp_path / "tasks"
    task_dir = tasks_dir / "safe"
    task_dir.mkdir(parents=True)
    outside_logs = tmp_path / "outside-logs"
    outside_logs.mkdir()
    victim = outside_logs / "run1.log"
    victim.write_text("keep\n", encoding="utf-8")
    run_logs = task_dir / RUN_LOGS_DIR
    try:
        run_logs.symlink_to(outside_logs, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"directory symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="Run logs directory must not be"):
        executor._get_log_path(str(task_dir), 1)
    with pytest.raises(ValueError, match="Run logs directory must not be"):
        executor._append_error_summary(
            str(task_dir),
            run_index=1,
            title="blocked",
            detail_lines=["do not write outside"],
        )

    manager = _make_task_manager(tasks_dir)
    try:
        task = {"name": "safe", "dir": str(task_dir), "run_index": 1}
        manager._append_gpu_queue_log(task, "Queued", ["waiting"])
        manager._append_error_summary(
            str(task_dir),
            title="blocked",
            detail_lines=["do not write outside"],
        )
    finally:
        manager.shutdown()

    assert victim.read_text(encoding="utf-8") == "keep\n"
    assert not (outside_logs / "queue.log").exists()
    assert not (outside_logs / ERROR_LOG_FILENAME).exists()


def test_executor_rejects_symlinked_run_log_file(tmp_path):
    task_dir = tmp_path / "tasks" / "safe"
    run_logs = task_dir / RUN_LOGS_DIR
    run_logs.mkdir(parents=True)
    victim = tmp_path / "victim.log"
    victim.write_text("keep\n", encoding="utf-8")
    linked_log = run_logs / "run1.log"
    try:
        linked_log.symlink_to(victim)
    except OSError as exc:
        pytest.skip(f"file symlink creation unavailable: {exc}")

    with pytest.raises(ValueError, match="Log file must not be"):
        executor._get_log_path(str(task_dir), 1)
    assert victim.read_text(encoding="utf-8") == "keep\n"


def test_executor_rejects_simulated_reparse_run_logs_directory(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io

    task_dir = tmp_path / "tasks" / "safe"
    run_logs = task_dir / RUN_LOGS_DIR
    run_logs.mkdir(parents=True)
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(run_logs)):
            return True
        return real_check(path)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    with pytest.raises(ValueError, match="Run logs directory must not be"):
        executor._get_log_path(str(task_dir), 1)
    assert list(run_logs.iterdir()) == []


def test_task_manager_default_root_and_lease_edges(tmp_path, monkeypatch):
    custom_root = tmp_path / "run-root"
    tasks_dir = custom_root / TASKS_DIR
    tasks_dir.mkdir(parents=True)

    monkeypatch.setattr("pyruns._config.ROOT_DIR", str(custom_root))
    with patch.object(TaskManager, "_scheduler_loop", lambda self: None):
        manager = TaskManager(tasks_dir=None, lazy_scan=None)

    assert manager.tasks_dir == str(tasks_dir)
    assert manager._disk_scan_complete is False
    assert manager.list_tasks() == []

    assert TaskManager._lease_until_value({"lease_until": "bad"}) == 0.0


def test_task_manager_logs_and_gpu_helper_branches(tmp_path, monkeypatch):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    task_dir = tasks_dir / "task"
    task_dir.mkdir()
    task = {"name": "task", "dir": str(task_dir), "run_index": 1}

    manager = _make_task_manager(tasks_dir)

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
    manager._append_gpu_wait_decision(
        task,
        1,
        config,
        GpuDecision(assignment=None, reason="busy", snapshot=[]),
        waited=10,
        now=100,
    )
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

    queue_log = task_dir / RUN_LOGS_DIR / "queue.log"
    queue_bytes = queue_log.read_bytes()
    with open(queue_log, "r", encoding="utf-8", newline="") as handle:
        queue_text = handle.read()
    refresh_line = (
        "\r[PYRUNS] Run #1 still waiting after 00:00:10 | "
        "blocked: busy | GPU snapshot: no NVIDIA GPU metrics available"
    )
    assert refresh_line.encode("utf-8") in queue_bytes
    assert refresh_line in queue_text
    assert "\n" + refresh_line in queue_text
    assert "[PYRUNS] [GPU WAIT] Run #1 still waiting after 00:00:10" not in queue_text
    assert "[PYRUNS]   Updated at " in queue_text
    assert "-------------------- RUN #2 --------------------" in queue_text
    run_one_text, run_two_text = queue_text.split("-------------------- RUN #2 --------------------", 1)
    assert not run_two_text.startswith("\n\n")
    run_two_body = run_two_text.lstrip("\n")
    assert "\n\n[PYRUNS] Last status at " not in run_one_text
    assert "\n\n[PYRUNS] [GPU ASSIGNED]" in run_one_text
    assert refresh_line + "\n\n[PYRUNS] [GPU ASSIGNED] Run #1 assigned GPUs 1,3 after 00:01:01" in run_one_text
    assert "\n\n[PYRUNS] -------------------- RUN #2 --------------------" in queue_text
    assert run_two_body.startswith("[PYRUNS] [GPU ASSIGNED]")
    assert "\n\n[PYRUNS] Last status at " not in run_two_body
    assert "GPU WAIT" in queue_text
    assert "GPU ASSIGNED" in queue_text
    assert "still waiting after 00:00:10" in queue_text
    assert "still waiting after 00:01:01" not in queue_text
    assert "CUDA_VISIBLE_DEVICES=GPU-uuid-0,MIG-GPU-uuid/0/1" in queue_text
    assert "Updated at " in queue_text
    assert "Last status at " not in queue_text

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
    manager = _make_task_manager(tasks_dir)

    task = {"name": "task", "dir": str(tasks_dir / "task"), "run_index": 1}
    config = GpuSchedulerConfig(enabled=True, stable_seconds=15)
    decision = GpuDecision(assignment=None, reason="busy", snapshot=[])

    assert manager._gpu_wait_log_interval(config) == 15

    first = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=0, now=100)
    assert first is not None
    assert manager._gpu_wait_decision_lines(task, 1, config, decision, waited=14, now=114) is None
    second = manager._gpu_wait_decision_lines(task, 1, config, decision, waited=15, now=115)
    assert second is not None
    assert "still waiting after 00:00:15" in second[0]


def test_task_manager_gpu_wait_refresh_labels_stabilizing_candidates():
    line = TaskManager._gpu_wait_refresh_line(
        [
            "Run #3 still waiting after 00:00:05",
            "Stabilizing: GPU 0 stabilizing 0.0/5s",
            "GPU 0 eligible: memory 58%, compute 2%, free 5.1 GiB",
        ]
    )

    assert line == (
        "[PYRUNS] Run #3 still waiting after 00:00:05 | "
        "stabilizing: GPU 0 stabilizing 0.0/5s | "
        "GPU 0 eligible: memory 58%, compute 2%, free 5.1 GiB"
    )
    assert "blocked:" not in line


def test_task_manager_gpu_wait_log_interval_ignores_reason_changes_until_stable_window(tmp_path):
    tasks_dir = tmp_path / "tasks"
    tasks_dir.mkdir()
    manager = _make_task_manager(tasks_dir)

    task = {"name": "task", "dir": str(tasks_dir / "task"), "run_index": 1}
    config = GpuSchedulerConfig(enabled=True, stable_seconds=15)
    first_decision = GpuDecision(assignment=None, reason="GPU 0 memory 22% > 20%", snapshot=[])
    changed_decision = GpuDecision(assignment=None, reason="GPU 0 memory 24% > 20%", snapshot=[])

    assert manager._gpu_wait_log_interval(config) == 15

    first = manager._gpu_wait_decision_lines(task, 1, config, first_decision, waited=0, now=100)
    assert first is not None
    assert manager._gpu_wait_decision_lines(task, 1, config, changed_decision, waited=1, now=101) is None
    second = manager._gpu_wait_decision_lines(task, 1, config, changed_decision, waited=15, now=115)
    assert second is not None
    assert "still waiting after 00:00:15" in second[0]


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
    assigned_log = _gpu_assignment_log({"PYRUNS_ASSIGNED_GPUS": "2"}, run_index=3)
    assert "GPU CONTEXT" in assigned_log
    assert "[PYRUNS] GPU assignment: 2" in assigned_log
    assert "Run #3 uses GPU(s): 2" in assigned_log
    assert "Run log: run3.log" in assigned_log
    assert "PYRUNS_ASSIGNED_GPUS=2" in assigned_log
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
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w", encoding="utf-8") as f:
        json.dump(task_info, f)

    def finish_with_pending_stop():
        update_task_info(
            task_dir,
            lambda info: info.update({
                "_pending_stop_summary": {
                    "run_index": 1,
                    "event": "stopped",
                    "reason": "cancelled_by_user",
                    "detail_lines": ["previous_status=running"],
                },
            }),
        )
        return 1

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.wait.side_effect = finish_with_pending_stop
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

    assert res["status"] == "cancelled"
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Run #1 stopped" in content
    assert "reason=cancelled_by_user" in content
    assert "previous_status=running" in content
    assert "exit_code=1" in content
    assert "reason=exit_code 1" not in content
    run_log = Path(task_dir, "run_logs", "run1.log").read_text(encoding="utf-8")
    assert "[PYRUNS] Final status: cancelled" in run_log
    final_info = json.loads(Path(task_dir, TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert "_pending_stop_summary" not in final_info
    assert final_info["exit_codes"] == [1]
    assert final_info["durations"][0] >= 0


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_pending_stop_summary_forces_failed_even_when_exit_code_zero(
    mock_popen,
    mock_emit,
    mock_detect,
    tmp_path,
):
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
    }
    with open(os.path.join(task_dir, TASK_INFO_FILENAME), "w", encoding="utf-8") as f:
        json.dump(task_info, f)

    def finish_with_pending_stop():
        update_task_info(
            task_dir,
            lambda info: info.update({
                "_pending_stop_summary": {
                    "run_index": 1,
                    "event": "stopped",
                    "reason": "cancelled_by_user",
                    "detail_lines": ["previous_status=running"],
                },
            }),
        )
        return 0

    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.wait.side_effect = finish_with_pending_stop
    mock_proc.returncode = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"stopped output", b""])
    mock_popen.return_value = mock_proc

    res = run_task_worker(
        task_dir=task_dir,
        name="StopTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert res["status"] == "cancelled"
    final_info = json.loads(Path(task_dir, TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert final_info["status"] == "cancelled"
    assert final_info["progress"] == 0.0
    assert final_info["exit_codes"] == [0]
    assert final_info["durations"][0] >= 0
    assert "_pending_stop_summary" not in final_info

    error_log = os.path.join(task_dir, "run_logs", "error.log")
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
    assert "Run #1 stopped" in content
    assert "reason=cancelled_by_user" in content
    assert "exit_code=0" in content
    assert "reason=exit_code 0" not in content


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_stale_completion_does_not_overwrite_new_runner(
    mock_popen,
    mock_emit,
    mock_detect,
    tmp_path,
    monkeypatch,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    save_task_info(
        task_dir,
        {
            "name": "RecoveredTask",
            "script": "script.py",
            "status": "running",
            "progress": 0.0,
            "run_index": 1,
            "runner_id": "runner-old",
            "runner_host": "host-old",
            "start_times": [""],
            "finish_times": [""],
            "run_statuses": ["running"],
            "pids": [None],
        },
    )

    original_append = executor._append_run_log_text

    def append_after_recovery(*args, **kwargs):
        def replace_owner(info):
            first_slot = ensure_run_slot(info, 1)
            second_slot = ensure_run_slot(info, 2)
            info["run_statuses"][first_slot] = "failed"
            info["finish_times"][first_slot] = "2026-03-20_00-00-02"
            info["run_statuses"][second_slot] = "running"
            info["status"] = "running"
            info["progress"] = 0.25
            info["run_index"] = 2
            info["runner_id"] = "runner-new"
            info["runner_host"] = "host-new"

        update_task_info(task_dir, replace_owner)
        return original_append(*args, **kwargs)

    monkeypatch.setattr(executor, "_append_run_log_text", append_after_recovery)
    mock_proc = MagicMock()
    mock_proc.pid = 7778
    mock_proc.wait.return_value = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"old output", b""])
    mock_popen.return_value = mock_proc

    result = run_task_worker(
        task_dir=task_dir,
        name="RecoveredTask",
        created_at="now",
        config={},
        run_index=1,
        runner_id="runner-old",
        runner_host="host-old",
    )

    assert result["status"] == "completed"
    final_info = load_task_info(task_dir)
    assert final_info["status"] == "running"
    assert final_info["progress"] == 0.25
    assert final_info["run_index"] == 2
    assert final_info["runner_id"] == "runner-new"
    assert final_info["runner_host"] == "host-new"
    assert final_info["run_statuses"] == ["failed", "running"]


@patch("pyruns.utils.parse_utils.detect_config_source_fast")
@patch("pyruns.utils.events.log_emitter.emit")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_late_stop_summary_is_not_overwritten_by_completed(
    mock_popen,
    mock_emit,
    mock_detect,
    tmp_path,
    monkeypatch,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = str(tmp_path)
    os.makedirs(os.path.join(task_dir, "run_logs"), exist_ok=True)
    save_task_info(
        task_dir,
        {
            "name": "StopTask",
            "script": "script.py",
            "status": "running",
            "run_index": 1,
            "start_times": ["2026-03-20_00-00-01"],
            "finish_times": [""],
            "pids": [7777],
        },
    )

    original_append = executor._append_run_log_text

    def append_and_cancel(*args, **kwargs):
        update_task_info(
            task_dir,
            lambda info: info.update({
                "status": "cancelled",
                "_pending_stop_summary": {
                    "run_index": 1,
                    "event": "stopped",
                    "reason": "cancelled_by_user",
                    "detail_lines": ["previous_status=running"],
                },
            }),
        )
        return original_append(*args, **kwargs)

    monkeypatch.setattr(executor, "_append_run_log_text", append_and_cancel)
    mock_proc = MagicMock()
    mock_proc.pid = 7777
    mock_proc.wait.return_value = 0
    mock_proc.stdout.read1 = MagicMock(side_effect=[b"completed output", b""])
    mock_popen.return_value = mock_proc

    res = run_task_worker(
        task_dir=task_dir,
        name="StopTask",
        created_at="now",
        config={},
        run_index=1,
    )

    assert res["status"] == "cancelled"
    final_info = json.loads(Path(task_dir, TASK_INFO_FILENAME).read_text(encoding="utf-8"))
    assert final_info["status"] == "cancelled"
    assert final_info["progress"] == 0.0
    assert "_pending_stop_summary" not in final_info
    error_log = os.path.join(task_dir, "run_logs", "error.log")
    with open(error_log, "r", encoding="utf-8") as f:
        content = f.read()
    assert "reason=cancelled_by_user" in content
    assert "reason=exit_code 0" not in content

#  TaskGenerator — task creation and file writing


#  create_task_object

class TestCreateTaskObject:
    def test_python_task_fields_and_created_at_format(self):
        obj = create_task_object("/tmp/task1", "my-task", config={"lr": 0.01})
        assert obj["dir"] == "/tmp/task1"
        assert obj["name"] == "my-task"
        assert obj["status"] == "pending"
        assert obj["config"] == {"lr": 0.01}
        assert obj["env"] == {}
        assert len(obj["created_at"]) == 19
        assert "-" in obj["created_at"]
        assert "_" in obj["created_at"]

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


#  TaskGenerator.create_task

class TestTaskGeneratorCreateTask:
    def test_create_task_writes_expected_files_and_metadata(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        cfg = {
            "lr": 0.01,
            "model": {"name": "resnet"},
            "_meta_desc": "lr=0.01",
            "_meta_other": "x",
        }
        task = gen.create_task("my-exp", cfg)

        assert os.path.isdir(task["dir"])
        assert os.path.basename(task["dir"]).startswith("my-exp")
        info_path = os.path.join(task["dir"], TASK_INFO_FILENAME)
        assert os.path.exists(info_path)
        with open(info_path, "r", encoding="utf-8") as f:
            info = json.load(f)
        assert info["name"] == "my-exp"
        assert info["status"] == "pending"
        cfg_path = os.path.join(task["dir"], CONFIG_FILENAME)
        assert os.path.exists(cfg_path)
        with open(cfg_path, "r", encoding="utf-8") as f:
            loaded = yaml.safe_load(f)
        assert loaded == {"lr": 0.01, "model": {"name": "resnet"}}
        log_dir = os.path.join(task["dir"], "run_logs")
        assert os.path.isdir(log_dir)
        assert not os.path.exists(os.path.join(log_dir, "run1.log"))

    def test_group_index_is_used_in_task_name_and_folder(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("batch-run", {"x": 1}, group_index="3-of-10")

        assert task["name"] == "batch-run_3-of-10"
        assert os.path.basename(task["dir"]) == task["name"]

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
            assert load_task_info(task["dir"])["name"] == task["name"]

    def test_empty_prefix_uses_timestamp(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task = gen.create_task("", {"x": 1})

        folder = os.path.basename(task["dir"])
        assert folder.startswith("task_")

    def test_task_kind_and_runtime_specific_shell_payload_are_persisted(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        task_cfg = gen.create_task("cfg-task", {"x": 1}, task_kind=TASK_KIND_CONFIG)
        with patch(
            "pyruns.core.task_generator.get_shell_config_filename_for_workspace",
            return_value=POWERSHELL_CONFIG_FILENAME,
        ):
            task_shell = gen.create_shell_task("shell-task", "Write-Host 'hello'\n")

        info_cfg = load_task_info(task_cfg["dir"])
        info_shell = load_task_info(task_shell["dir"])

        assert info_cfg["task_kind"] == TASK_KIND_CONFIG
        assert "config_mode" not in info_cfg
        assert info_cfg["config_file"] == CONFIG_FILENAME
        assert info_shell["task_kind"] == TASK_KIND_SHELL
        assert "config_mode" not in info_shell
        assert info_shell["config_file"] == POWERSHELL_CONFIG_FILENAME
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

    def test_invalid_task_kind_and_name_are_rejected(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        with pytest.raises(ValueError, match="Unsupported task kind"):
            gen.create_task("invalid", {"x": 1}, task_kind="unknown-kind")
        with pytest.raises(ValueError, match="invalid characters"):
            gen.create_task("bad/name", {"x": 1})


#  TaskGenerator.create_tasks (batch)

class TestTaskGeneratorCreateTasks:
    def test_single_and_batch_names_are_complete_and_unique(self, tmp_path):
        gen = TaskGenerator(root_dir=str(tmp_path))
        single = gen.create_tasks([{"x": 1}], "single")
        tasks = gen.create_tasks([{"x": i} for i in range(3)], "batch")

        assert [task["name"] for task in single] == ["single"]
        assert len(tasks) == 3
        assert [task["name"] for task in tasks] == [
            "batch_1-of-3",
            "batch_2-of-3",
            "batch_3-of-3",
        ]
        assert len({task["dir"] for task in tasks}) == 3

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

    def test_batch_tasks_persist_unresolved_interpolations(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PYRUNS_TEST_SECRET", "must-not-be-persisted")
        gen = TaskGenerator(root_dir=str(tmp_path))
        configs = generate_batch_configs(
            OmegaConf.create(
                {
                    "lr": "0.001 | 0.01",
                    "secret": "${oc.env:PYRUNS_TEST_SECRET}",
                    "output": "${secret}/results",
                    "secret_choice": "${oc.env:PYRUNS_TEST_SECRET} | public",
                    "secret_list": [
                        "${oc.env:PYRUNS_TEST_SECRET}",
                        {"nested": "${secret}"},
                    ],
                }
            )
        )

        tasks = gen.create_tasks(configs, "interpolation")

        assert len(tasks) == 4
        config_texts = []
        for task in tasks:
            config_text = Path(task["dir"], CONFIG_FILENAME).read_text(encoding="utf-8")
            config_texts.append(config_text)
            assert "must-not-be-persisted" not in config_text
            assert "secret: ${oc.env:PYRUNS_TEST_SECRET}" in config_text
            assert "output: ${secret}/results" in config_text
            saved = yaml.safe_load(config_text)
            assert saved["secret_list"] == [
                "${oc.env:PYRUNS_TEST_SECRET}",
                {"nested": "${secret}"},
            ]
        assert any(
            "secret_choice: ${oc.env:PYRUNS_TEST_SECRET}" in text
            for text in config_texts
        )
        assert any("secret_choice: public" in text for text in config_texts)

#  Report — CSV and JSON export


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
        task["durations"] = [12.345]
        task["exit_codes"] = [0]
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        rows = list(reader)
        assert len(rows) == 1
        assert rows[0]["name"] == "t1"
        assert rows[0]["run"] == "1"
        assert rows[0]["loss"] == "0.5"
        assert rows[0]["acc"] == "92"
        assert rows[0]["duration_seconds"] == "12.345"
        assert rows[0]["exit_code"] == "0"

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

    def test_uses_platform_neutral_lf_line_endings(self, tmp_path):
        task = _make_task(tmp_path, "t3", records=[{"loss": 0.5}])

        csv_str = build_export_csv([task])

        assert "\r" not in csv_str
        assert csv_str.count("\n") == 2

    def test_column_order(self, tmp_path):
        task = _make_task(tmp_path, "t3", records=[{"zeta": 1, "alpha": 2}])
        csv_str = build_export_csv([task])
        reader = csv.DictReader(io.StringIO(csv_str))
        cols = reader.fieldnames
        # Priority columns should come first
        assert cols[:4] == ["name", "status", "run", "start_time"]

    def test_monitor_fields_cannot_override_lifecycle_columns(self, tmp_path):
        task = _make_task(
            tmp_path,
            "safe-name",
            records=[{"name": "spoofed", "status": "running", "run": 999, "loss": 0.5}],
        )
        task["exit_codes"] = [0]

        row = next(csv.DictReader(io.StringIO(build_export_csv([task]))))

        assert row["name"] == "safe-name"
        assert row["status"] == "completed"
        assert row["run"] == "1"
        assert row["loss"] == "0.5"

    def test_formula_like_values_are_neutralized_for_spreadsheets(self, tmp_path):
        task = _make_task(tmp_path, "formula", records=[{"note": "+cmd", "=dangerous-header": "value"}])
        task["name"] = "=HYPERLINK(\"https://example.invalid\")"
        task["exit_codes"] = [0]

        row = next(csv.DictReader(io.StringIO(build_export_csv([task]))))

        assert row["name"].startswith("'=")
        assert row["note"] == "'+cmd"
        assert "'=dangerous-header" in row


def test_run_history_normalization_aligns_process_and_source_metadata():
    meta = {
        "run_index": 2,
        "start_times": ["started"],
        "source_states": ["git one", "git two"],
    }

    assert run_slot_count(meta) == 2
    assert normalize_run_history(meta) == 2
    assert meta["start_times"] == ["started", ""]
    assert meta["durations"] == [None, None]
    assert meta["exit_codes"] == [None, None]
    assert meta["source_states"] == ["git one", "git two"]

    assert ensure_run_slot(meta, 3) == 2
    assert all(len(meta[key]) == 3 for key in (
        "start_times",
        "finish_times",
        "pids",
        "durations",
        "exit_codes",
        "source_states",
        "records",
        "tracks",
    ))
    TaskManager._trim_run_slots(meta, 1)
    assert all(len(meta[key]) == 1 for key in (
        "start_times",
        "finish_times",
        "pids",
        "durations",
        "exit_codes",
        "source_states",
        "records",
        "tracks",
    ))


class TestBuildExportJSON:
    def test_basic(self, tmp_path):
        task = _make_task(tmp_path, "j1", records=[{"loss": 0.3}])
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["name"] == "j1"
        assert result[0]["run"] == 1
        assert result[0]["loss"] == 0.3

    def test_output_remains_ascii_safe_for_supplementary_unicode(self, tmp_path):
        task = _make_task(tmp_path, "emoji-😀", records=[{"label": "😀"}])

        document = build_export_json([task])

        assert document.isascii()
        assert json.loads(document)[0]["label"] == "😀"

    def test_no_monitor_still_exports_run_history(self, tmp_path):
        task = _make_task(tmp_path, "j2")
        result = json.loads(build_export_json([task]))
        assert len(result) == 1
        assert result[0]["name"] == "j2"
        assert result[0]["run"] == 1

    def test_never_run_task_does_not_fabricate_run_one(self, tmp_path):
        task_dir = tmp_path / "never-run"
        task_dir.mkdir()
        save_task_info(str(task_dir), {"name": "never-run", "status": "pending"})
        task = {"name": "never-run", "status": "pending", "dir": str(task_dir)}

        assert json.loads(build_export_json([task])) == []

    def test_historical_status_is_derived_per_run(self, tmp_path):
        task = _make_task(
            tmp_path,
            "rerun",
            records=[{}, {}],
            starts=["first", "second"],
            finishes=["first-done", "second-done"],
            pids=[111, 222],
        )
        task["status"] = "completed"
        task["exit_codes"] = [7, 0]
        task["run_statuses"] = ["cancelled", "completed"]

        rows = json.loads(build_export_json([task]))

        assert [row["status"] for row in rows] == ["cancelled", "completed"]
        completed_only = json.loads(build_export_json([task], statuses={"completed"}))
        assert [row["run"] for row in completed_only] == [2]

    def test_rejects_non_finite_json_metrics(self, tmp_path):
        task = _make_task(tmp_path, "nan", records=[{"loss": float("nan")}])
        task["exit_codes"] = [0]

        with pytest.raises(ValueError, match="JSON"):
            build_export_json([task])


