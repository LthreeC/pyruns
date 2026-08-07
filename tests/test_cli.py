"""Contract tests for the one-shot Pyruns CLI."""

from __future__ import annotations

import csv
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from pyruns._config import TASKS_DIR, TRASH_DIR
from pyruns.cli.app import main
from pyruns.launcher import bootstrap_shell_workspace, bootstrap_workspace
from pyruns.utils.info_io import load_task_info


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _source_cli(*args: str) -> list[str]:
    code = (
        "from pyruns.cli.app import main; "
        f"raise SystemExit(main({list(args)!r}))"
    )
    return [sys.executable, "-c", code]


def _source_named_cli(program: str, *args: str) -> list[str]:
    code = (
        "import sys; "
        f"sys.argv={ [program, *args]!r}; "
        "from pyruns.cli.app import main; "
        "raise SystemExit(main())"
    )
    return [sys.executable, "-c", code]


def _source_env() -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        str(PROJECT_ROOT)
        if not existing
        else os.pathsep.join([str(PROJECT_ROOT), existing])
    )
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_cli(cwd: Path, *args: str, timeout: float = 20.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _source_cli(*args),
        cwd=cwd,
        env=_source_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_named_cli(
    cwd: Path,
    program: str,
    *args: str,
    timeout: float = 20.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        _source_named_cli(program, *args),
        cwd=cwd,
        env=_source_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _wait_status(task_dir: Path, expected: set[str], timeout: float = 15.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = load_task_info(str(task_dir))
        if str(info.get("status", "")) in expected:
            return info
        time.sleep(0.05)
    pytest.fail(f"task did not reach {sorted(expected)}")


def test_no_args_prints_layered_help_without_workspace(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main([]) == 0
    output = capsys.readouterr().out
    assert "Pyruns records reproducible terminal commands" in output
    assert "pyr and pyruns are identical" in output
    assert "Quick start:" in output
    assert "Workspace selection:" in output
    assert "pyr help -a" in output
    assert "    exec " in output
    assert "    show " in output
    assert "    status " not in output
    assert "    export " not in output
    assert not (tmp_path / "_pyruns_").exists()


def test_help_all_lists_advanced_commands_without_workspace(tmp_path):
    result = _run_cli(tmp_path, "help", "-a")

    assert result.returncode == 0, result.stderr
    assert "All command groups:" in result.stdout
    assert "    status " in result.stdout
    assert "    restore " in result.stdout
    assert "    export " in result.stdout
    assert "    dev " in result.stdout
    assert not (tmp_path / "_pyruns_").exists()

    combined = _run_cli(tmp_path, "help", "exec", "-a")
    assert combined.returncode == 2
    assert "--all cannot be combined with COMMAND" in combined.stderr


@pytest.mark.parametrize(
    ("program", "alternate"),
    [("pyr", "pyruns"), ("pyruns", "pyr")],
)
def test_official_entrypoints_render_their_own_complete_help(
    program,
    alternate,
    tmp_path,
):
    result = _run_named_cli(tmp_path, program)
    assert result.returncode == 0, result.stderr
    assert f"usage: {program} " in result.stdout
    assert f"{program} and {alternate} are identical" in result.stdout
    assert f"{program} exec -n check -- python -V" in result.stdout
    assert f"{program} help COMMAND" in result.stdout
    assert not (tmp_path / "_pyruns_").exists()

    command_help = _run_named_cli(tmp_path, program, "help", "exec")
    assert command_help.returncode == 0, command_help.stderr
    assert f"usage: {program} exec" in command_help.stdout
    assert f"{program} exec -n smoke -- python -V" in command_help.stdout
    assert ".sh, .ps1, .cmd" in command_help.stdout
    assert f"{program} exec -n setup -- ./scripts/setup.sh" in command_help.stdout
    assert "--env-file" in command_help.stdout
    assert "-e CUDA_VISIBLE_DEVICES=0 SEED=42 -- python train.py" in command_help.stdout
    assert "standard -- separator" in command_help.stdout
    assert "-c COMMAND_STRING" in command_help.stdout
    assert "--shell" not in command_help.stdout
    assert "python -V > python-version.txt" in command_help.stdout
    assert "&&" in command_help.stdout


@pytest.mark.parametrize(
    "command",
    [
        "init",
        "exec",
        "add",
        "run",
        "ls",
        "status",
        "show",
        "log",
        "wait",
        "stop",
        "rm",
        "restore",
        "mv",
        "pin",
        "export",
        "config",
        "metrics",
        "ui",
        "dev",
        "help",
    ],
)
def test_every_command_has_workspace_free_help(command, tmp_path):
    result = _run_cli(tmp_path, command, "--help")
    assert result.returncode == 0, result.stderr
    assert f"usage: pyr {command}" in result.stdout


def test_ui_uses_positional_workspace_target_and_rejects_old_selectors(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import commands

    launched = {}

    def launch_ui(**kwargs):
        launched.update(kwargs)
        return 0

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_launch_ui", launch_ui)

    assert main(["ui", "shell", "--no-browser"]) == 0
    shell_workspace = tmp_path / "_pyruns_" / "_shell_"
    assert shell_workspace.is_dir()
    assert launched == {
        "start_path": "/",
        "port": None,
        "open_browser": False,
    }

    script = tmp_path / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    launched.clear()

    assert main(["ui", str(script), "--port", "8123"]) == 0
    assert launched == {
        "start_path": "/",
        "port": 8123,
        "open_browser": None,
    }
    launched.clear()

    assert main(["ui", "train"]) == 0
    assert launched == {
        "start_path": "/",
        "port": None,
        "open_browser": None,
    }
    assert (tmp_path / "_pyruns_" / ".active_workspace").read_text(
        encoding="utf-8"
    ) == "train"

    removed = _run_cli(tmp_path, "ui", "--shell")
    assert removed.returncode == 2
    assert "unrecognized arguments: --shell" in removed.stderr

    old_workspace_form = _run_cli(tmp_path, "-w", "train", "ui")
    assert old_workspace_form.returncode == 2
    assert "ui does not use -w/--workspace" in old_workspace_form.stderr


@pytest.mark.parametrize(
    "removed",
    [
        "create",
        "list",
        "logs",
        "cancel",
        "remove",
        "rename",
        "runs",
        "fg",
        "stat",
        "gen",
        "delete",
        "repl",
        "cli",
        "clis",
    ],
)
def test_removed_commands_are_not_accepted(removed, tmp_path):
    result = _run_cli(tmp_path, removed)
    assert result.returncode == 2
    assert "invalid choice" in result.stderr
    assert result.stdout == ""


def test_run_rejects_old_workspace_conflicting_workers_short_flag(tmp_path):
    result = _run_cli(tmp_path, "run", "-w", "2")
    assert result.returncode == 2
    assert "unrecognized arguments: -w" in result.stderr


def test_exec_argv_requires_the_standard_separator(tmp_path):
    result = _run_cli(tmp_path, "exec", sys.executable, "-V")

    assert result.returncode == 2
    assert "exec argv form requires '--' before COMMAND" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("-w", "shell", "init"), "init does not use -w/--workspace"),
        (("-w", "shell", "config", "list"), "config does not use -w/--workspace"),
        (("-w", "shell", "metrics"), "metrics does not use -w/--workspace"),
        (("-w", "shell", "dev", "train.py"), "dev does not use -w/--workspace"),
        (("--json", "ui"), "--json is not supported by ui or dev"),
        (("--json", "dev", "train.py"), "--json is not supported by ui or dev"),
    ],
)
def test_commands_reject_global_options_they_do_not_use(tmp_path, args, message):
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_unknown_options_fail_with_usage_on_stderr(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(tmp_path, "-w", "shell", "ls", "--unknown")
    assert result.returncode == 2
    assert "unrecognized arguments: --unknown" in result.stderr
    assert result.stdout == ""


def test_invalid_directory_fails_as_usage(tmp_path):
    missing = tmp_path / "missing"
    result = _run_cli(tmp_path, "-C", str(missing), "status")
    assert result.returncode == 2
    assert "directory does not exist" in result.stderr


@pytest.mark.parametrize("value", ["nan", "inf", "-inf"])
def test_timeout_rejects_non_finite_values(tmp_path, value):
    result = _run_cli(tmp_path, "wait", "task", f"--timeout={value}")
    assert result.returncode == 2
    assert "value must be a finite number that is zero or greater" in result.stderr


def test_init_config_requires_a_script(tmp_path):
    result = _run_cli(tmp_path, "init", "--config", "config.yaml")
    assert result.returncode == 2
    assert "--config requires SCRIPT" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_init_shell_outputs_workspace_and_status_is_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["--json", "init"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["kind"] == "shell"
    assert Path(created["workspace"]).is_dir()

    assert main(["--json", "-w", "shell", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["kind"] == "shell"
    assert status["total"] == 0


def test_exec_with_explicit_shell_selector_bootstraps_fresh_project(tmp_path):
    result = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "exec",
        "-n",
        "explicit-shell",
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["tasks"][0]["status"] == "completed"
    assert (tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "explicit-shell").is_dir()


def test_exec_dry_run_is_stable_json_and_has_no_workspace_side_effect(tmp_path):
    result = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "exec",
        "--dry-run",
        "-n",
        "planned",
        "--env",
        "MODE=check",
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "dry_run": True,
        "operation": "exec",
        "workspace": str(tmp_path / "_pyruns_" / "_shell_").replace("\\", "/"),
        "workspace_exists": False,
        "creates_workspace": True,
        "task": {
            "requested_name": "planned",
            "planned_name": "planned",
            "name_is_exact": True,
            "name_available": True,
        },
        "workdir": str(tmp_path).replace("\\", "/"),
        "command_mode": "argv",
        "command_argv": [sys.executable, "-V"],
        "shell_expression": None,
        "shell_executable": None,
        "shell_kind": None,
        "script": None,
        "env": {"MODE": "check"},
        "detach": False,
    }
    assert not (tmp_path / "_pyruns_").exists()


def test_exec_dry_run_reports_automatic_name_collision_without_writing(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "command", "echo old\n"
    )
    before = {path.name for path in (workspace / TASKS_DIR).iterdir()}

    result = _run_cli(tmp_path, "--json", "exec", "--dry-run", "--", sys.executable, "-V")

    assert result.returncode == 0, result.stdout + result.stderr
    task = json.loads(result.stdout)["task"]
    assert task == {
        "requested_name": "command",
        "planned_name": None,
        "name_is_exact": False,
        "name_available": False,
    }
    assert {path.name for path in (workspace / TASKS_DIR).iterdir()} == before


def test_exec_command_string_dry_run_does_not_evaluate_the_expression(tmp_path):
    marker = tmp_path / "must-not-exist.txt"
    expression = f'echo touched > "{marker}"'

    result = _run_cli(
        tmp_path,
        "exec",
        "--dry-run",
        "-c",
        expression,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing was created or run" in result.stdout
    assert not marker.exists()
    assert not (tmp_path / "_pyruns_").exists()


def test_follow_task_retries_final_log_until_size_is_stable(monkeypatch):
    from pyruns.cli import commands

    offsets = iter([0, 7, 7, 7, 7])
    calls = []

    monkeypatch.setattr(commands, "_task_record", lambda _task: {"status": "completed"})
    monkeypatch.setattr(commands, "_log_path", lambda _task: "run1.log")

    def read_log(_path, offset):
        calls.append(offset)
        return next(offsets)

    monkeypatch.setattr(commands, "_write_available_log", read_log)

    assert commands._follow_task({"name": "fast"})["status"] == "completed"
    assert calls == [0, 0, 7, 7, 7]


def test_workspace_discovery_walks_upward(tmp_path, monkeypatch, capsys):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert main(["--json", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["workspace"]).name == "_shell_"


def test_multiple_workspaces_require_explicit_selection(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    bootstrap_workspace(str(script))
    result = _run_cli(tmp_path, "status")
    assert result.returncode == 1
    assert "multiple workspaces found" in result.stderr
    assert "-w/--workspace" in result.stderr


def test_exact_target_names_reject_indices_and_fuzzy_matches(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "alpha-long", "echo ok\n"
    )
    by_index = _run_cli(tmp_path, "-w", "shell", "show", "1")
    fuzzy = _run_cli(tmp_path, "-w", "shell", "show", "alpha")
    assert by_index.returncode == 1
    assert fuzzy.returncode == 1
    assert "task not found" in by_index.stderr
    assert "task not found" in fuzzy.stderr


def test_exec_exact_argv_runs_and_returns_task_result(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "argv-smoke",
        "--",
        sys.executable,
        "-c",
        "print('hello exact argv')",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "hello exact argv" in result.stdout

    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "argv-smoke"
    assert load_task_info(str(task_dir))["status"] == "completed"
    assert len(list(task_dir.glob("config.*"))) == 1


def test_exec_auto_initializes_shell_workspace(tmp_path, monkeypatch, capsys):
    from pyruns.cli import commands

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_find_project_root", lambda: None)
    assert main(
        [
            "exec",
            "--name",
            "auto-init",
            "--",
            sys.executable,
            "-c",
            "print('auto initialized')",
        ]
    ) == 0
    assert "auto initialized" in capsys.readouterr().out
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "auto-init"
    assert load_task_info(str(task_dir))["status"] == "completed"
    expected_suffix = ".ps1" if os.name == "nt" else ".sh"
    assert next(task_dir.glob("config.*")).suffix == expected_suffix


def test_exact_argv_rendering_matches_each_shell_runtime(monkeypatch):
    from pyruns.cli import commands

    parts = ["python", "script with space.py", "--label", "a'b", "$HOME", "x&y"]

    monkeypatch.setattr(
        commands,
        "get_shell_runtime_for_workspace",
        lambda _workspace: {"terminal_kind": "powershell"},
    )
    assert commands._render_argument_command(parts, "workspace") == (
        "& 'python' 'script with space.py' '--label' 'a''b' '$HOME' 'x&y'"
    )

    monkeypatch.setattr(
        commands,
        "get_shell_runtime_for_workspace",
        lambda _workspace: {"terminal_kind": "cmd"},
    )
    assert commands._render_argument_command(parts, "workspace") == (
        'python "script with space.py" --label a\'b $HOME "x&y"'
    )

    monkeypatch.setattr(
        commands,
        "get_shell_runtime_for_workspace",
        lambda _workspace: {"terminal_kind": "bash"},
    )
    assert shlex.split(commands._render_argument_command(parts, "workspace")) == parts


def test_exec_missing_script_file_reports_clear_error(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    suffix = ".ps1" if os.name == "nt" else ".sh"
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "missing-script",
        "--",
        f"missing file{suffix}",
    )
    assert result.returncode == 1
    assert f"script file not found: missing file{suffix}" in result.stderr
    assert not (
        tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "missing-script"
    ).exists()


@pytest.mark.skipif(os.name == "nt", reason="requires a native POSIX shell")
def test_exec_runs_sh_file_without_executable_bit_and_can_rerun(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script_dir = tmp_path / "scripts with spaces"
    script_dir.mkdir()
    script = script_dir / "run check.sh"
    script.write_text(
        "#!/bin/sh\nprintf 'script=%s|%s\\n' \"$1\" \"$2\"\n",
        encoding="utf-8",
    )

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "direct-sh",
        "--",
        str(script.relative_to(tmp_path)),
        "value with spaces",
        "x&y",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "script=value with spaces|x&y" in result.stdout

    rerun = _run_cli(tmp_path, "-w", "shell", "run", "direct-sh")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "script=value with spaces|x&y" in rerun.stdout
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "direct-sh"
    assert len(load_task_info(str(task_dir))["start_times"]) == 2


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Bash or WSL")
def test_exec_runs_sh_file_on_windows_when_bash_is_available(tmp_path):
    from pyruns.utils.shell_runtime import build_script_file_argv

    workspace = bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script = tmp_path / "run check.sh"
    script.write_text(
        "#!/bin/sh\nprintf 'script=%s|%s\\n' \"$1\" \"$2\"\n",
        encoding="utf-8",
    )
    try:
        build_script_file_argv(str(script), [], workspace)
    except RuntimeError as exc:
        pytest.skip(str(exc))

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "direct-windows-sh",
        "--",
        str(script),
        "value with spaces",
        "x&y",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "script=value with spaces|x&y" in result.stdout

@pytest.mark.skipif(os.name != "nt", reason="requires PowerShell")
def test_exec_runs_powershell_file_with_arguments_and_can_rerun(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script_dir = tmp_path / "scripts with spaces"
    script_dir.mkdir()
    script = script_dir / "run check.ps1"
    script.write_text(
        "param([string]$First, [string]$Second)\n"
        'Write-Output "script=$First|$Second"\n',
        encoding="utf-8",
    )

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "direct-ps1",
        "--",
        str(script.relative_to(tmp_path)),
        "value with spaces",
        "x&y",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "script=value with spaces|x&y" in result.stdout

    rerun = _run_cli(tmp_path, "-w", "shell", "run", "direct-ps1")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "script=value with spaces|x&y" in rerun.stdout
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "direct-ps1"
    info = load_task_info(str(task_dir))
    assert len(info["start_times"]) == 2
    assert Path(info["script"]) == script
    assert all("script none" not in state for state in info["source_states"])


@pytest.mark.skipif(os.name != "nt", reason="requires PowerShell and cmd.exe")
def test_exact_argv_rerun_ignores_changed_shell_runtime(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    first = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "cross-shell-argv",
        "--",
        sys.executable,
        "-c",
        "print('cross-shell-ok')",
    )
    assert first.returncode == 0, first.stdout + first.stderr

    settings = tmp_path / "_pyruns_" / "_pyruns_settings.yaml"
    settings.write_text(
        "shell_mode: custom\nshell_executable: cmd.exe\n",
        encoding="utf-8",
    )
    rerun = _run_cli(tmp_path, "-w", "shell", "run", "cross-shell-argv")

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "cross-shell-ok" in rerun.stdout


@pytest.mark.skipif(os.name != "nt", reason="requires PowerShell and cmd.exe")
def test_shell_expression_rerun_uses_creation_shell_runtime(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    first = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "stored-shell",
        "-c",
        "Write-Output stored-shell-ok",
    )
    assert first.returncode == 0, first.stdout + first.stderr

    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "stored-shell"
    info = load_task_info(str(task_dir))
    assert Path(str(info["shell_executable"])).name.lower() in {"powershell.exe", "pwsh.exe"}

    settings = tmp_path / "_pyruns_" / "_pyruns_settings.yaml"
    settings.write_text(
        "shell_mode: custom\nshell_executable: cmd.exe\n",
        encoding="utf-8",
    )
    rerun = _run_cli(tmp_path, "-w", "shell", "run", "stored-shell")

    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert "stored-shell-ok" in rerun.stdout


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
@pytest.mark.parametrize("suffix", [".cmd", ".bat"])
def test_exec_runs_windows_command_file_with_arguments(tmp_path, suffix):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script_dir = tmp_path / "scripts with spaces"
    script_dir.mkdir()
    script = script_dir / f"run check{suffix}"
    script.write_text(
        "@echo off\n"
        "setlocal DisableDelayedExpansion\n"
        "set \"first=%~1\"\n"
        "set \"second=%~2\"\n"
        "setlocal EnableDelayedExpansion\n"
        "echo script=!first!^|!second!\n",
        encoding="utf-8",
    )

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        f"direct-{suffix[1:]}",
        "--",
        str(script.relative_to(tmp_path)),
        "value with spaces",
        "x&y",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "script=value with spaces|x&y" in result.stdout


@pytest.mark.skipif(os.name != "nt", reason="requires cmd.exe")
def test_cmd_exact_argv_round_trip_preserves_batch_metacharacters(tmp_path, monkeypatch):
    from pyruns.cli import commands

    monkeypatch.setattr(
        commands,
        "get_shell_runtime_for_workspace",
        lambda _workspace: {"terminal_kind": "cmd"},
    )
    expected = [
        "space value",
        "x&y",
        "%PATH%",
        "bang!",
        "caret^",
        'quote"value',
        "trailing\\",
        "中文参数",
    ]
    parts = [
        sys.executable,
        "-c",
        "import json,sys; print(json.dumps(sys.argv[1:], ensure_ascii=False))",
        *expected,
    ]
    batch = tmp_path / "argv-round-trip.cmd"
    batch.write_text(
        "@echo off\n"
        "setlocal DisableDelayedExpansion\n"
        "chcp 65001 >nul\n"
        + commands._render_argument_command(parts, "workspace")
        + "\n",
        encoding="utf-8-sig",
    )
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/v:on", "/c", str(batch)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_exec_command_string_preserves_expression(tmp_path, monkeypatch, capsys):
    from pyruns.cli import commands

    expression = (
        "Write-Output alpha; Write-Output beta"
        if os.name == "nt"
        else "printf 'alpha\\n'; printf 'beta\\n'"
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_find_project_root", lambda: None)
    assert main(["exec", "--name", "shell-expression", "-c", expression]) == 0
    output = capsys.readouterr().out
    assert "alpha" in output
    assert "beta" in output
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "shell-expression"
    info = load_task_info(str(task_dir))
    payload = task_dir / info["config_file"]
    assert payload.read_text(encoding="utf-8").strip() == expression


def test_exec_failure_propagates_nonzero(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "argv-failure",
        "--",
        sys.executable,
        "-c",
        "raise SystemExit(7)",
    )
    assert result.returncode == 1
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "argv-failure"
    info = load_task_info(str(task_dir))
    assert info["status"] == "failed"
    assert info["exit_codes"] == [7]
    assert len(info["durations"]) == 1
    assert info["durations"][0] >= 0
    assert "[PYRUNS] Exit code: 7" in result.stdout
    assert "[PYRUNS] Duration:" in result.stdout

    shown = _run_cli(tmp_path, "--json", "-w", "shell", "show", "argv-failure@1")
    detail = json.loads(shown.stdout)
    assert detail["exit_codes"] == [7]
    assert detail["durations"] == info["durations"]
    assert detail["source_states"] == info["source_states"]
    assert detail["records"] == info["records"]
    assert detail["tracks"] == info["tracks"]
    assert detail["selected_run"]["exit_code"] == 7
    assert detail["selected_run"]["duration_seconds"] == info["durations"][0]
    assert detail["selected_run"]["source_state"] == info["source_states"][0]


def test_exec_detach_returns_before_completion(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    started = time.monotonic()
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "detached",
        "--detach",
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(3); print('done')",
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stderr
    assert elapsed < 2.5
    assert result.stdout.strip() == "detached"
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "detached"
    info = _wait_status(task_dir, {"completed", "failed"})
    assert info["status"] == "completed"


def test_exec_environment_is_persisted_and_used(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "env-task",
        "--env",
        "PYRUNS_V1_VALUE=works",
        "--",
        sys.executable,
        "-c",
        "import os; print(os.environ['PYRUNS_V1_VALUE'])",
    )
    assert result.returncode == 0
    assert "works" in result.stdout
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "env-task"
    assert load_task_info(str(task_dir))["env"] == {"PYRUNS_V1_VALUE": "works"}


@pytest.mark.parametrize("env_flag", ["-e", "--env"])
def test_exec_accepts_multiple_values_after_one_env_flag(tmp_path, env_flag):
    result = _run_cli(
        tmp_path,
        "--json",
        "exec",
        env_flag,
        "PYRUNS_ENV_A=one",
        "PYRUNS_ENV_B=two",
        "-e",
        "PYRUNS_ENV_C=three",
        "--dry-run",
        "--name",
        "compact-env",
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["task"]["planned_name"] == "compact-env"
    assert payload["env"] == {
        "PYRUNS_ENV_A": "one",
        "PYRUNS_ENV_B": "two",
        "PYRUNS_ENV_C": "three",
    }
    assert not (tmp_path / "_pyruns_").exists()


def test_exec_compact_env_stops_at_command_string_option(tmp_path):
    expression = "echo compact-env"
    result = _run_cli(
        tmp_path,
        "--json",
        "exec",
        "-e",
        "PYRUNS_ENV_A=one",
        "PYRUNS_ENV_B=two",
        "--dry-run",
        "-c",
        expression,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["command_mode"] == "shell"
    assert payload["shell_expression"] == expression
    assert payload["env"] == {
        "PYRUNS_ENV_A": "one",
        "PYRUNS_ENV_B": "two",
    }


def test_exec_env_files_merge_in_order_and_cli_env_takes_precedence(tmp_path):
    (tmp_path / "base.env").write_text(
        "# shared training defaults\n"
        "PYRUNS_ENV_A=base\n"
        "PYRUNS_ENV_SHARED=base\n"
        "PYRUNS_ENV_TOKEN=left=right\n",
        encoding="utf-8",
    )
    (tmp_path / "override.env").write_text(
        "\nPYRUNS_ENV_SHARED=file-two\nPYRUNS_ENV_B=second\n",
        encoding="utf-8",
    )

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "env-files",
        "--env-file",
        "base.env",
        "--env-file",
        "override.env",
        "-e",
        "PYRUNS_ENV_SHARED=command-line",
        "--",
        sys.executable,
        "-c",
        (
            "import json,os; "
            "print(json.dumps({k: os.environ[k] for k in "
            "['PYRUNS_ENV_A','PYRUNS_ENV_SHARED','PYRUNS_ENV_TOKEN','PYRUNS_ENV_B']}))"
        ),
    )

    assert result.returncode == 0, result.stdout + result.stderr
    expected = {
        "PYRUNS_ENV_A": "base",
        "PYRUNS_ENV_SHARED": "command-line",
        "PYRUNS_ENV_TOKEN": "left=right",
        "PYRUNS_ENV_B": "second",
    }
    assert json.dumps(expected) in result.stdout
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "env-files"
    assert load_task_info(str(task_dir))["env"] == expected


@pytest.mark.parametrize(
    ("filename", "content", "message"),
    [
        ("missing.env", None, "environment file not found"),
        ("broken.env", "BROKEN\n", "must use KEY=VALUE"),
        ("bad-name.env", "1INVALID=value\n", "invalid environment variable name"),
    ],
)
def test_exec_validates_env_files_before_creating_workspace(
    tmp_path,
    filename,
    content,
    message,
):
    if content is not None:
        (tmp_path / filename).write_text(content, encoding="utf-8")

    result = _run_cli(
        tmp_path,
        "exec",
        "--env-file",
        filename,
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_exec_validates_environment_before_creating_workspace(tmp_path):
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "invalid-env",
        "--env",
        "BROKEN",
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 2
    assert "environment value must use KEY=VALUE" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_exec_rejects_an_explicit_duplicate_name(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    first = _run_cli(tmp_path, "exec", "--name", "duplicate", "--", sys.executable, "-V")
    second = _run_cli(tmp_path, "exec", "--name", "duplicate", "--", sys.executable, "-V")

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 1
    assert "already exists" in second.stderr
    tasks_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR
    assert [path.name for path in tasks_dir.iterdir() if path.is_dir()] == ["duplicate"]


def test_exec_persists_exact_argv_and_creation_workdir(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    code = "import json,os,sys; print(json.dumps([os.getcwd(), *sys.argv[1:]]))"
    command = [sys.executable, "-c", code, "value with spaces", "x&y"]

    first = _run_cli(nested, "exec", "--name", "cwd-argv", "--", *command)
    assert first.returncode == 0, first.stdout + first.stderr
    first_payload = next(
        json.loads(line)
        for line in first.stdout.splitlines()
        if line.startswith('["')
    )
    assert first_payload == [str(nested), "value with spaces", "x&y"]

    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "cwd-argv"
    info = load_task_info(str(task_dir))
    assert info["command_mode"] == "argv"
    assert info["cmd"] == command
    assert Path(info["workdir"]) == nested

    rerun = _run_cli(tmp_path, "-w", "shell", "run", "cwd-argv")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    rerun_payload = next(
        json.loads(line)
        for line in rerun.stdout.splitlines()
        if line.startswith('["')
    )
    assert rerun_payload == [str(nested), "value with spaces", "x&y"]


def test_exec_command_string_rejects_an_unquoted_tail_and_removed_shell_option(tmp_path):
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "bad-shell",
        "-c",
        "echo",
        "hello",
    )

    assert result.returncode == 2
    assert "-c/--command accepts exactly one command string" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()

    removed = _run_cli(tmp_path, "exec", "--shell", "echo hello")
    assert removed.returncode == 2
    assert "unrecognized arguments: --shell" in removed.stderr


def test_add_is_noninteractive_and_run_from_waits_for_all(tmp_path):
    script = tmp_path / "train.py"
    script.write_text(
        "import argparse\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--value', type=int)\n"
        "a=p.parse_args()\n"
        "print(f'value={a.value}')\n",
        encoding="utf-8",
    )
    config = tmp_path / "sweep.yaml"
    config.write_text("value: 1 | 2\n", encoding="utf-8")
    bootstrap_workspace(str(script))

    created = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "train",
        "add",
        str(config),
        "--name",
        "created",
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    assert [item["name"] for item in payload["created"]] == [
        "created_[1-of-2]",
        "created_[2-of-2]",
    ]

    run = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "train",
        "run",
        "--from",
        str(config),
        "--name",
        "run",
        "-j",
        "2",
        timeout=30,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    results = json.loads(run.stdout)["tasks"]
    assert len(results) == 2
    assert {item["status"] for item in results} == {"completed"}


def test_run_from_dry_run_previews_batch_without_creating_tasks(tmp_path):
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    config = tmp_path / "sweep.yaml"
    config.write_text("value: 1 | 2\n", encoding="utf-8")
    workspace = Path(bootstrap_workspace(str(script)))
    tasks_dir = workspace / TASKS_DIR
    before = {path.name for path in tasks_dir.iterdir()}

    result = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "train",
        "run",
        "--from",
        str(config),
        "--name",
        "preview",
        "--workers",
        "2",
        "--dry-run",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["operation"] == "run-from"
    assert payload["task_count"] == 2
    assert payload["mode"] == "thread"
    assert payload["workers"] == 2
    assert [task["planned_name"] for task in payload["tasks"]] == [
        "preview_[1-of-2]",
        "preview_[2-of-2]",
    ]
    assert not any(task["name_is_exact"] for task in payload["tasks"])
    assert {path.name for path in tasks_dir.iterdir()} == before


def test_run_dry_run_rejects_existing_task_mode_as_usage(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    result = _run_cli(tmp_path, "run", "existing", "--dry-run")

    assert result.returncode == 2
    assert "run --dry-run requires --from CONFIG" in result.stderr


def test_batch_run_waits_and_aggregates_failure(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    generator = TaskGenerator(root_dir=str(workspace / TASKS_DIR))
    if os.name == "nt":
        generator.create_shell_task(
            "batch-ok", f'& "{sys.executable}" -c "print(1)"\n'
        )
        generator.create_shell_task(
            "batch-bad", f'& "{sys.executable}" -c "exit 5"\n'
        )
    else:
        executable = shlex.quote(sys.executable)
        generator.create_shell_task("batch-ok", f"{executable} -c 'print(1)'\n")
        generator.create_shell_task("batch-bad", f"{executable} -c 'exit 5'\n")
    result = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "run",
        "batch-ok",
        "batch-bad",
        "--workers",
        "2",
        timeout=30,
    )
    assert result.returncode == 1
    payload = json.loads(result.stdout)
    assert {item["status"] for item in payload["tasks"]} == {"completed", "failed"}


def test_ls_show_log_and_status_have_machine_contracts(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    completed = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "inspect-me",
        "--",
        sys.executable,
        "-c",
        "print('machine-readable')",
    )
    assert completed.returncode == 0

    listing = _run_cli(
        tmp_path, "--json", "-w", "shell", "ls", "--status", "completed"
    )
    assert listing.returncode == 0
    listed = json.loads(listing.stdout)
    assert listed["count"] == 1
    assert listed["tasks"][0]["name"] == "inspect-me"

    shown = _run_cli(tmp_path, "--json", "-w", "shell", "show", "inspect-me")
    detail = json.loads(shown.stdout)
    assert detail["command"]
    assert detail["latest_log"].endswith("run1.log")

    log_path = _run_cli(
        tmp_path, "-w", "shell", "log", "inspect-me", "--path"
    )
    assert log_path.returncode == 0
    assert Path(log_path.stdout.strip()).is_file()

    log = _run_cli(tmp_path, "-w", "shell", "log", "inspect-me")
    assert log.returncode == 0
    assert "machine-readable" in log.stdout

    conflicting = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "log",
        "inspect-me",
        "--follow",
        "--run",
        "1",
    )
    assert conflicting.returncode == 2
    assert "either --follow or --run" in conflicting.stderr


def test_show_and_log_accept_task_run_references(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    created = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "versioned",
        "--",
        sys.executable,
        "-c",
        "import os; print(os.environ['PYRUNS_RUN_INDEX'])",
    )
    rerun = _run_cli(tmp_path, "-w", "shell", "run", "versioned")
    assert created.returncode == 0, created.stdout + created.stderr
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr

    first_log = _run_cli(tmp_path, "-w", "shell", "log", "versioned@1")
    second_log = _run_cli(tmp_path, "-w", "shell", "log", "versioned@2")
    assert first_log.returncode == 0, first_log.stderr
    assert second_log.returncode == 0, second_log.stderr
    assert "1" in first_log.stdout.splitlines()
    assert "2" in second_log.stdout.splitlines()

    path_result = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "log",
        "versioned@1",
        "--path",
    )
    path_payload = json.loads(path_result.stdout)
    assert path_payload["task"] == "versioned"
    assert path_payload["run"] == 1
    assert path_payload["path"].endswith("run1.log")

    shown = _run_cli(tmp_path, "--json", "-w", "shell", "show", "versioned@1")
    detail = json.loads(shown.stdout)
    assert detail["run_index"] == 2
    assert detail["selected_run"]["index"] == 1
    assert detail["selected_run"]["start_time"]
    assert detail["selected_run"]["finish_time"]
    assert detail["selected_run"]["duration_seconds"] >= 0
    assert detail["selected_run"]["exit_code"] == 0
    assert detail["selected_run"]["source_state"]
    assert isinstance(detail["selected_run"]["record"], dict)
    assert isinstance(detail["selected_run"]["track"], dict)
    assert detail["selected_run"]["log"].endswith("run1.log")

    conflict = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "log",
        "versioned@1",
        "--run",
        "2",
    )
    missing = _run_cli(tmp_path, "-w", "shell", "show", "versioned@3")
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr
    assert missing.returncode == 1
    assert "available runs: 1-2" in missing.stderr


def test_task_names_reserve_at_for_run_references(tmp_path):
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "invalid@name",
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 2
    assert "reserved for TASK@RUN references" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_log_follow_rejects_pending_task_instead_of_waiting_forever(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "pending-log", "echo ok\n"
    )
    result = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "log",
        "pending-log",
        "-f",
        timeout=3,
    )
    assert result.returncode == 1
    assert "cannot follow pending task" in result.stderr


def test_stop_reaches_detached_runner(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    submitted = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "cancel-me",
        "--detach",
        "--",
        sys.executable,
        "-c",
        "import time; print('started', flush=True); time.sleep(20)",
    )
    assert submitted.returncode == 0
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "cancel-me"
    _wait_status(task_dir, {"queued", "running"})

    cancelled = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "stop",
        "cancel-me",
        "--timeout",
        "10",
    )
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
    payload = json.loads(cancelled.stdout)
    assert payload["stopped"][0]["status"] == "cancelled"


def test_rm_ls_trash_and_restore(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "recoverable",
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )
    assert result.returncode == 0

    removed = _run_cli(tmp_path, "-w", "shell", "rm", "recoverable")
    assert removed.returncode == 0
    trash = (
        tmp_path
        / "_pyruns_"
        / "_shell_"
        / TASKS_DIR
        / TRASH_DIR
        / "recoverable"
    )
    assert trash.is_dir()

    listing = _run_cli(
        tmp_path, "--json", "-w", "shell", "ls", "--trash"
    )
    assert json.loads(listing.stdout)["tasks"][0]["name"] == "recoverable"
    filtered = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "ls",
        "does-not-match",
        "--trash",
        "--status",
        "failed",
    )
    assert json.loads(filtered.stdout)["count"] == 0

    restored = _run_cli(tmp_path, "-w", "shell", "restore", "recoverable")
    assert restored.returncode == 0
    assert (
        tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "recoverable"
    ).is_dir()


def test_rm_is_noninteractive_and_rejects_removed_yes_flag(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "keep", "echo ok\n"
    )
    old_flag = _run_cli(tmp_path, "-w", "shell", "rm", "keep", "--yes")
    assert old_flag.returncode == 2
    assert "unrecognized arguments: --yes" in old_flag.stderr
    assert _run_cli(tmp_path, "-w", "shell", "rm", "keep").returncode == 0
    assert not (workspace / TASKS_DIR / "keep").exists()


def test_restore_preflights_all_destination_conflicts(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    generator = TaskGenerator(root_dir=str(workspace / TASKS_DIR))
    generator.create_shell_task("first", "echo first\n")
    generator.create_shell_task("second", "echo second\n")
    removed = _run_cli(tmp_path, "-w", "shell", "rm", "first", "second")
    assert removed.returncode == 0, removed.stderr

    generator.create_shell_task("second", "echo replacement\n")
    restored = _run_cli(tmp_path, "-w", "shell", "restore", "first", "second")
    assert restored.returncode == 1
    assert "an active task has that name" in restored.stderr
    assert not (workspace / TASKS_DIR / "first").exists()
    assert (workspace / TASKS_DIR / TRASH_DIR / "first").is_dir()


def test_rm_rejects_active_batch_before_deleting_anything(monkeypatch):
    from pyruns.cli import commands

    deleted: list[list[str]] = []
    manager = SimpleNamespace(delete_tasks=lambda names: deleted.append(names) or names)
    monkeypatch.setattr(
        commands,
        "_resolve_exact_tasks",
        lambda _manager, _names: [
            {"name": "finished", "status": "completed"},
            {"name": "active", "status": "running"},
        ],
    )
    with pytest.raises(commands.CliError, match="stop them first: active"):
        commands.cmd_rm(
            SimpleNamespace(json_output=False),
            SimpleNamespace(tasks=["finished", "active"]),
            manager,
        )
    assert deleted == []


def test_mv_and_pin_use_exact_names(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "before", "echo ok\n"
    )
    renamed = _run_cli(tmp_path, "-w", "shell", "mv", "before", "after")
    assert renamed.returncode == 0
    pinned = _run_cli(
        tmp_path, "--json", "-w", "shell", "pin", "after"
    )
    assert json.loads(pinned.stdout)["pinned"] is True
    unpinned = _run_cli(
        tmp_path, "--json", "-w", "shell", "pin", "after", "--off"
    )
    assert json.loads(unpinned.stdout)["pinned"] is False


def test_export_defaults_to_stdout_and_can_write_file(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "exportable",
        "--",
        sys.executable,
        "-c",
        "print('ok')",
    )
    assert result.returncode == 0
    stdout_export = _run_cli(
        tmp_path, "-w", "shell", "export", "--format", "csv"
    )
    assert stdout_export.returncode == 0
    rows = list(csv.DictReader(stdout_export.stdout.splitlines()))
    assert rows[0]["name"] == "exportable"

    output = tmp_path / "report.json"
    file_export = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "export",
        "--format",
        "json",
        "--output",
        str(output),
    )
    assert file_export.returncode == 0
    assert output.is_file()

    conflicting = _run_cli(
        tmp_path,
        "--json",
        "-w",
        "shell",
        "export",
        "--format",
        "csv",
    )
    assert conflicting.returncode == 2
    assert "requires '--format json'" in conflicting.stderr


def test_config_get_set_unset_and_path(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    path = _run_cli(tmp_path, "config", "path")
    assert path.returncode == 0
    assert Path(path.stdout.strip()).is_file()
    json_path = _run_cli(tmp_path, "--json", "config", "path")
    assert Path(json.loads(json_path.stdout)["path"]).is_file()

    set_result = _run_cli(
        tmp_path,
        "config",
        "set",
        "manager_max_workers",
        "7",
    )
    assert set_result.returncode == 0
    get_result = _run_cli(
        tmp_path,
        "config",
        "get",
        "manager_max_workers",
    )
    assert get_result.stdout.strip() == "7"
    unset_result = _run_cli(
        tmp_path,
        "config",
        "unset",
        "manager_max_workers",
    )
    assert unset_result.returncode == 0
    assert unset_result.stdout.strip() != "7"


def test_config_rejects_unknown_keys_and_wrong_types(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    unknown = _run_cli(tmp_path, "config", "get", "unknown")
    wrong_type = _run_cli(
        tmp_path,
        "config",
        "set",
        "manager_max_workers",
        "text",
    )
    assert unknown.returncode == 1
    assert wrong_type.returncode == 2


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ui_port", "70000"),
        ("manager_max_workers", "-3"),
        ("shell_mode", "nonsense"),
        ("gpu_scheduler_memory_used_pct", "101"),
    ],
)
def test_config_rejects_out_of_range_and_unknown_choice_values(tmp_path, key, value):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(tmp_path, "config", "set", key, value)

    assert result.returncode == 2


def test_project_config_does_not_require_workspace_selection(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    bootstrap_workspace(str(script))

    result = _run_cli(tmp_path, "config", "path")

    assert result.returncode == 0, result.stderr
    assert Path(result.stdout.strip()) == tmp_path / "_pyruns_" / "_pyruns_settings.yaml"


def test_metrics_does_not_require_workspace(tmp_path):
    result = _run_cli(tmp_path, "--json", "metrics")
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert "cpu_percent" in payload
    assert "memory" in payload


def test_help_topic_and_version_do_not_touch_workspace(tmp_path):
    help_result = _run_cli(tmp_path, "help", "run")
    version_result = _run_cli(tmp_path, "--version")
    assert help_result.returncode == 0
    assert "By default Pyruns waits for every task" in help_result.stdout
    assert version_result.returncode == 0
    assert version_result.stdout.startswith("pyr ")
    assert not (tmp_path / "_pyruns_").exists()
