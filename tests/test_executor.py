"""Executor environments, commands, process lifecycle, and log handling."""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from omegaconf import OmegaConf

import pyruns.core.executor as executor
from pyruns._config import (
    ENV_KEY_CONFIG,
    ENV_KEY_CLI_TERMINAL_RUNTIME,
    ENV_KEY_CONDA_ENV,
    ENV_KEY_CONDA_EXE,
    ENV_KEY_PYTHON_EXECUTABLE,
    CONFIG_FILENAME,
    ERROR_LOG_FILENAME,
    DEFAULT_ROOT_NAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAME,
    SHELL_WORKSPACE_NAME,
    TASKS_DIR,
    TASK_INFO_FILENAME,
    TASK_KIND_CONFIG,
    TASK_KIND_SHELL,
)
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
from pyruns.core.system_metrics import SystemMonitor
from pyruns.utils.info_io import (
    ensure_run_slot,
    load_task_info,
    save_task_info,
    update_task_info,
)
from pyruns.utils.config_utils import save_yaml


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


def _write_fake_pyruns_package(
    parent: Path,
    *,
    init_source: str = "__version__ = 'new-pyruns'\n",
    core_source: str = "",
) -> Path:
    package = parent / "pyruns"
    (package / "core").mkdir(parents=True)
    (package / "__init__.py").write_text(init_source, encoding="utf-8")
    (package / "core" / "__init__.py").write_text(core_source, encoding="utf-8")
    (package / "core" / "executor.py").write_text("", encoding="utf-8")
    return package


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
    launcher_pyruns = _write_fake_pyruns_package(launcher_site_packages)

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
    launcher_pyruns = _write_fake_pyruns_package(launcher_site_packages)

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
    launcher_pyruns = _write_fake_pyruns_package(launcher_site_packages)

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
    launcher_pyruns = _write_fake_pyruns_package(
        launcher_site_packages,
        init_source="import missing_pyruns_dependency\n",
    )

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
    launcher_pyruns = _write_fake_pyruns_package(
        launcher_site_packages,
        init_source="from importlib.metadata import version\n__version__ = version('pyruns')\n",
    )
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
    launcher_pyruns = _write_fake_pyruns_package(launcher_site_packages)

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
    launcher_pyruns = _write_fake_pyruns_package(
        launcher_site_packages,
        core_source="MARKER = 'launcher-core'\n",
    )

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
    launcher_pyruns = _write_fake_pyruns_package(launcher_site_packages)

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
    launcher_pyruns = _write_fake_pyruns_package(launcher_source_root)

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


@pytest.mark.parametrize(
    ("relative_path", "sources", "probe", "expected"),
    [
        pytest.param(
            "__init__.py",
            ("__version__ = 'first-pyruns'\n", "__version__ = 'second-pyruns'\n"),
            "import pyruns; print(pyruns.__version__)",
            ("first-pyruns", "second-pyruns"),
            id="package-root",
        ),
        pytest.param(
            "core/config_manager.py",
            ("MARKER = 'first-module'\n", "MARKER = 'second-module'\n"),
            "from pyruns.core import config_manager; print(config_manager.MARKER)",
            ("first-module", "second-module"),
            id="nested-module",
        ),
    ],
)
def test_prepare_env_refreshes_isolated_pyruns_root_after_source_change(
    tmp_path,
    monkeypatch,
    relative_path,
    sources,
    probe,
    expected,
):
    launcher_site_packages = tmp_path / "launcher" / "Lib" / "site-packages"
    launcher_pyruns = _write_fake_pyruns_package(launcher_site_packages)
    changed_file = launcher_pyruns / relative_path

    monkeypatch.setattr(executor, "__file__", str(launcher_pyruns / "core" / "executor.py"))
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.setenv(ENV_KEY_CLI_TERMINAL_RUNTIME, "1")

    for run_index, (source, output) in enumerate(
        zip(sources, expected, strict=True),
        start=1,
    ):
        changed_file.write_text(source, encoding="utf-8")
        env = _prepare_env(
            task_dir=str(tmp_path / f"task{run_index}"),
            task_kind=TASK_KIND_CONFIG,
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == output


def test_prepare_env_reuses_isolated_pyruns_root_for_same_package_fingerprint(tmp_path, monkeypatch):
    """Repeated task launches should not recopy pyruns when package files are unchanged."""

    launcher_source_root = tmp_path / "launcher-source"
    launcher_pyruns = _write_fake_pyruns_package(launcher_source_root)

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
    assert "_pending_stop_summary" not in executor.load_task_metadata(str(task_dir))

    save_task_info(str(task_dir), {"name": "task", "_pending_stop_summary": "bad"})
    assert executor._consume_pending_stop_summary(str(task_dir), 3) is None

    assert executor._build_run_source_state(task_dir=str(task_dir), script_path=None, workdir=str(tmp_path)).startswith("git ")


@pytest.mark.parametrize("chunk_size", [1, 7, 4096])
def test_shell_pipe_fallback_filters_controls_before_logging_and_emitting(tmp_path, monkeypatch, chunk_size):
    task_dir = _write_worker_task_info(tmp_path, "pipe-colors")
    update_task_info(task_dir, lambda info: info.update({"task_kind": TASK_KIND_SHELL, "command_mode": "shell"}))
    raw = b"\x1b[2J\x1b[38;5;9mred-text\x1b[0m\x1b]0;title\x07\r\n"
    process = MagicMock(pid=99999, returncode=0)
    process.stdout.read1.side_effect = [raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)] + [b""]
    process.wait.return_value = 0
    process.poll.return_value = 0
    monkeypatch.setattr(executor, "_build_command", lambda *a, **k: (["shell"], task_dir, []))
    monkeypatch.setattr(executor, "_build_run_source_state", lambda **kwargs: "git test | clean")
    monkeypatch.setattr(executor, "collect_run_environment", lambda *a, **k: {})
    monkeypatch.setattr(executor, "get_process_create_time", lambda _pid: None)
    terminal_spawn = MagicMock(side_effect=RuntimeError("no attached console"))
    monkeypatch.setattr(executor, "spawn_terminal_process", terminal_spawn)
    monkeypatch.setattr(executor.subprocess, "Popen", lambda *a, **k: process)
    emitted = []
    monkeypatch.setattr(executor.log_emitter, "emit", lambda _name, text, **metadata: emitted.append((text, metadata)))

    result = executor.run_task_worker(task_dir, "pipe-colors", "now", {})

    assert result["status"] == "completed"
    terminal_spawn.assert_called_once()
    log_path = Path(task_dir) / RUN_LOGS_DIR / "run1.log"
    for output in (log_path.read_text(encoding="utf-8"), "".join(text for text, _ in emitted)):
        assert "\x1b[38;5;9mred-text\x1b[0m" in output
        assert "\x1b[2J" not in output
        assert "\x1b]0;" not in output
    assert sum(metadata["byte_length"] for _, metadata in emitted) == log_path.stat().st_size


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
    assert info["launch_command"]
    assert info["launch_workdir"]
    assert isinstance(info["launch_started_at"], float)
    assert info["launch_run_index"] == 1
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
@patch("pyruns.core.executor.kill_process")
@patch("pyruns.core.executor.subprocess.Popen")
def test_run_task_worker_waits_for_process_that_survives_internal_error(
    mock_popen,
    mock_kill,
    _mock_emit,
    mock_detect,
    tmp_path,
    monkeypatch,
):
    mock_detect.return_value = ("pyruns_load", None)
    task_dir = _write_worker_task_info(tmp_path, "SurvivingTask")
    alive = True
    observations = []

    mock_proc = MagicMock()
    mock_proc.pid = 9123
    mock_proc.stdout.read1.return_value = b""

    def poll():
        return None if alive else 7

    wait_calls = 0

    def wait(timeout=None):
        nonlocal alive, wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise OSError("wait failed")
        info = load_task_info(task_dir)
        observations.append((info["status"], info["pids"][0]))
        alive = False
        mock_proc.returncode = 7
        return 7

    mock_proc.poll.side_effect = poll
    mock_proc.wait.side_effect = wait
    mock_popen.return_value = mock_proc
    mock_kill.return_value = False
    monkeypatch.setattr(executor, "get_process_create_time", lambda _pid: 1000.0)

    with patch(
        "pyruns.core.executor._build_run_source_state",
        return_value="git none | unknown | script none",
    ):
        result = run_task_worker(
            task_dir=task_dir,
            name="SurvivingTask",
            created_at="now",
            config={},
            run_index=1,
        )

    assert observations == [("running", 9123)]
    assert result["status"] == "failed"
    assert load_task_info(task_dir)["status"] == "failed"
    error_text = Path(task_dir, RUN_LOGS_DIR, ERROR_LOG_FILENAME).read_text(encoding="utf-8")
    assert "child_process_terminated=False" in error_text
    assert "child_process_survived_termination=True" in error_text


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
        patch("pyruns.core.run_environment.SystemMonitor._query_nvidia_smi", return_value=""),
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
    monkeypatch.setattr(SystemMonitor, "_query_nvidia_smi", lambda *args, **kwargs: "")

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

    original_update = executor.update_task_metadata
    update_calls = {"count": 0}

    def flaky_update(*args, **kwargs):
        update_calls["count"] += 1
        if update_calls["count"] == 1:
            raise RuntimeError("task info update failed")
        return original_update(*args, **kwargs)

    captured_create_times = []
    killed = []
    monkeypatch.setattr(executor, "update_task_metadata", flaky_update)
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
    mock_proc.close_output.assert_called_once_with()
    final_info = load_task_info(str(task_dir))
    assert final_info["status"] == "running"
    assert final_info["progress"] == 0.5
    assert final_info["run_index"] == 2
    assert final_info["runner_id"] == "runner-new"
    assert final_info["run_statuses"] == ["failed", "running"]


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


def test_executor_rejects_simulated_reparse_run_logs_directory(tmp_path, simulate_reparse):
    task_dir = tmp_path / "tasks" / "safe"
    run_logs = task_dir / RUN_LOGS_DIR
    run_logs.mkdir(parents=True)
    simulate_reparse(run_logs)

    with pytest.raises(ValueError, match="Run logs directory must not be"):
        executor._get_log_path(str(task_dir), 1)
    assert list(run_logs.iterdir()) == []


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


def test_run_environment_collection_does_not_delay_log_stream(tmp_path, monkeypatch):
    task_dir = _write_worker_task_info(tmp_path, "environment-stream")
    monkeypatch.setattr(executor, "_build_command", lambda *a, **k: ([sys.executable], task_dir, []))
    monkeypatch.setattr(executor, "_build_run_source_state", lambda **kwargs: "git test | clean")
    monkeypatch.setattr(executor, "get_process_create_time", lambda _pid: None)
    process = MagicMock(pid=99999, returncode=0)
    process.stdout.read1.side_effect = [b"training started\r\n", b""]
    process.wait.return_value = 0
    monkeypatch.setattr(executor, "_spawn_captured_process", lambda *a, **k: process)
    output_emitted = threading.Event()
    streamed_during_collection = []
    emitted_byte_lengths = []

    def collect(*args, **kwargs):
        # Simulate a GPU query waiting while the command produces output.
        streamed_during_collection.append(output_emitted.wait(2))
        return {"host": "gpu-server"}

    def emit(_name, text, **kwargs):
        emitted_byte_lengths.append(kwargs["byte_length"])
        if "training started" in text:
            output_emitted.set()

    monkeypatch.setattr(executor, "collect_run_environment", collect)
    monkeypatch.setattr(executor.log_emitter, "emit", emit)
    result = executor.run_task_worker(task_dir, "environment-stream", "now", {}, run_index=1)

    assert result["status"] == "completed"
    assert streamed_during_collection == [True]
    assert load_task_info(task_dir)["run_environments"] == [{"host": "gpu-server"}]
    assert sum(emitted_byte_lengths) == (Path(task_dir) / "run_logs" / "run1.log").stat().st_size


def test_run_environment_survives_rerun_and_runner_cleanup(tmp_path, monkeypatch):
    from pyruns.core import executor

    task_dir = _write_worker_task_info(tmp_path, "environment-history")
    monkeypatch.setattr(executor, "_build_command", lambda *a, **k: ([sys.executable, "-c", "pass"], task_dir, []))
    monkeypatch.setattr(executor, "_build_run_source_state", lambda **kwargs: "git test | clean")
    environments = [{"host": "server-one"}, {"host": "server-two"}]
    captured = []

    def collect(command, env, workdir, **kwargs):
        captured.append({"command": command, **kwargs})
        return environments[len(captured) - 1]

    monkeypatch.setattr(executor, "collect_run_environment", collect)
    for run_index in (1, 2):
        def claim(info):
            ensure_run_slot(info, run_index)
            info["runner_id"] = "test-runner"
            info["runner_host"] = "current-host"
            info["_gpu_assignment"] = {"gpu_ids": [run_index]}
        update_task_info(task_dir, claim)
        result = executor.run_task_worker(
            task_dir, "environment-history", "now", {}, run_index=run_index,
            runner_id="test-runner", runner_host="current-host",
        )
        assert result["status"] == "completed"

    info = load_task_info(task_dir)
    assert info["run_environments"] == environments
    assert "runner_host" not in info
    assert "_gpu_assignment" not in info
    assert [entry["assigned_gpu_ids"] for entry in captured] == [[1], [2]]
