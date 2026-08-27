"""Contract tests for the one-shot Pyruns CLI."""

from __future__ import annotations

import csv
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from pyruns._config import (
    ENV_KEY_CLI_SHELL_EXECUTABLE,
    ERROR_LOG_FILENAME,
    QUEUE_LOG_FILENAME,
    RUN_LOGS_DIR,
    SCRIPT_INFO_FILENAME,
    SHELL_CONFIG_FILENAMES,
    TASK_INFO_FILENAME,
    TASKS_DIR,
    TRASH_DIR,
)
from pyruns.cli.app import build_parser, main
from pyruns.launcher import bootstrap_shell_workspace, bootstrap_workspace
from pyruns.utils.info_io import load_task_info, update_task_info


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_cli_log_path_rejects_simulated_reparse_file(tmp_path, monkeypatch):
    import pyruns.utils.info_io as info_io
    from pyruns.cli import commands

    task_dir = tmp_path / TASKS_DIR / "safe"
    log_dir = task_dir / RUN_LOGS_DIR
    log_dir.mkdir(parents=True)
    log_path = log_dir / "run1.log"
    log_path.write_text("do not read\n", encoding="utf-8")
    (task_dir / TASK_INFO_FILENAME).write_text(
        json.dumps({"name": "safe", "status": "completed", "run_index": 1}),
        encoding="utf-8",
    )
    real_check = info_io._path_is_link_or_reparse

    def fake_reparse(path):
        if os.path.normcase(os.path.abspath(path)) == os.path.normcase(str(log_path)):
            return True
        return real_check(path)

    monkeypatch.setattr(info_io, "_path_is_link_or_reparse", fake_reparse)

    with pytest.raises(commands.CliError, match="unsafe log path"):
        commands._resolve_log_reference({"name": "safe", "dir": str(task_dir)})


def _source_cli(*args: str) -> list[str]:
    code = (
        "from pyruns.cli.app import main; "
        f"raise SystemExit(main({list(args)!r}))"
    )
    return [sys.executable, "-c", code]


def test_cli_app_module_runs_without_duplicate_import_warning():
    result = subprocess.run(
        [sys.executable, "-m", "pyruns.cli.app", "--version"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )

    assert result.returncode == 0
    assert "RuntimeWarning" not in result.stderr


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


def _run_cli(
    cwd: Path,
    *args: str,
    timeout: float = 20.0,
    env_overrides: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = _source_env()
    env.update(env_overrides or {})
    return subprocess.run(
        _source_cli(*args),
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
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
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
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
    assert "Pyruns saves terminal commands and Python experiments as named tasks" in output
    assert "pyr and pyruns are identical" in output
    assert "'ui' (normal)" in output
    assert "'dev' (development)" in output
    assert "Model: project -> workspace -> task -> numbered run" in output
    assert "nearest project found from the current directory" in output
    assert "Quick start -- track one terminal command:" in output
    assert "pyr run check" in output
    assert "pyr ui shell" in output
    assert "pyr help -a" in output
    assert "show command options (for example, ui --port)" in output
    assert "\n  --json" not in output
    assert "--no-color" not in output
    assert "    exec " in output
    assert "    show " in output
    assert "    status " in output
    assert "    wait " in output
    assert "    ui " in output
    assert "    export " not in output
    assert "Command forms for exec:" not in output
    assert "Environment values:" not in output
    assert not (tmp_path / "_pyruns_").exists()


def test_help_wraps_to_a_narrow_terminal(monkeypatch):
    monkeypatch.setenv("COLUMNS", "72")

    parser, commands = build_parser("pyr")
    help_by_topic = {"root": parser.format_help()}
    help_by_topic.update({name: command.format_help() for name, command in commands.items()})

    for topic, help_text in help_by_topic.items():
        lines = help_text.splitlines()
        assert lines, topic
        assert [line for line in lines if len(line) > 72] == [], topic


def test_help_all_lists_advanced_commands_without_workspace(tmp_path):
    result = _run_cli(tmp_path, "help", "-a")

    assert result.returncode == 0, result.stderr
    assert "All command groups:" in result.stdout
    assert "task setup" in result.stdout
    assert "create/run" not in result.stdout
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
    assert "Choose a command form:" in command_help.stdout
    assert "Exact argv (recommended for Python and ordinary programs):" in command_help.stdout
    assert "Existing shell script (tracked replacement" in command_help.stdout
    assert "Shell expression (only for pipes" in command_help.stdout
    assert "Each following token is one program argument" in command_help.stdout
    assert "-c COMMAND_STRING" in command_help.stdout
    assert "--shell" not in command_help.stdout
    assert "python eval.py > metrics.txt" in command_help.stdout
    assert "&&" in command_help.stdout
    assert "One -e accepts multiple KEY=VALUE entries" in command_help.stdout
    assert "Variables inherited from the invoking terminal" in command_help.stdout
    assert "are not saved" in command_help.stdout
    assert "PYTHONUNBUFFERED=1" in command_help.stdout
    assert f"{program} -w shell show train" in command_help.stdout
    assert f"{program} -w shell run train" in command_help.stdout


def test_help_explains_config_ui_metrics_and_help_workflows(tmp_path):
    config_help = _run_cli(tmp_path, "help", "config")
    config_set_help = _run_cli(tmp_path, "config", "set", "--help")
    metrics_help = _run_cli(tmp_path, "help", "metrics")
    ui_help = _run_cli(tmp_path, "help", "ui")
    dev_help = _run_cli(tmp_path, "help", "dev")
    help_help = _run_cli(tmp_path, "help", "help")

    for result in (
        config_help,
        config_set_help,
        metrics_help,
        ui_help,
        dev_help,
        help_help,
    ):
        assert result.returncode == 0, result.stderr
        assert "Examples:" in result.stdout

    assert "Settings are project-wide" in config_help.stdout
    assert "global_env" in config_help.stdout
    assert "global_env is persisted for Web UI runs" in config_help.stdout
    assert "CLI tasks inherit the invoking terminal" in config_help.stdout
    assert "exec -e" in config_help.stdout
    assert "used by later CLI and Web UI runs" not in config_help.stdout
    assert "Parse VALUE as YAML" in config_set_help.stdout
    assert "Environment variable names and values are validated" in config_set_help.stdout
    assert "does not require a workspace" in metrics_help.stdout
    assert "Do not write '-w shell ui'" in ui_help.stdout
    assert re.search(r"-p(?: PORT)?, --port PORT\b", ui_help.stdout)
    assert "Use 'ui --help' to discover --port" in ui_help.stdout
    assert "--no-browser keeps the server headless" in ui_help.stdout
    assert "Use 'ui' for normal use" in dev_help.stdout
    assert "Help is read-only" in " ".join(help_help.stdout.split())
    assert not (tmp_path / "_pyruns_").exists()


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
    assert "Examples:" in result.stdout


@pytest.mark.parametrize(
    "args",
    [
        ("init", "--json"),
        ("exec", "--json", "--", "python", "-V"),
        ("add", "config.yaml", "--json"),
        ("run", "task", "--json"),
        ("ls", "--json"),
        ("status", "--json"),
        ("show", "task", "--json"),
        ("log", "task", "--path", "--json"),
        ("wait", "task", "--json"),
        ("stop", "task", "--json"),
        ("rm", "task", "--json"),
        ("restore", "task", "--json"),
        ("mv", "task", "new-name", "--json"),
        ("pin", "task", "--json"),
        ("config", "list", "--json"),
        ("config", "get", "monitor_scrollback", "--json"),
        ("config", "set", "monitor_scrollback", "200000", "--json"),
        ("config", "unset", "monitor_scrollback", "--json"),
        ("config", "path", "--json"),
        ("metrics", "--json"),
    ],
)
def test_every_machine_readable_command_accepts_json_in_its_natural_scope(args):
    parser, _commands = build_parser("pyr", show_all_commands=True)

    parsed = parser.parse_args(args)

    assert parsed.json_output is True


def test_json_stays_in_command_scope_and_exec_separator_preserves_child_options():
    parser, _commands = build_parser("pyr", show_all_commands=True)

    assert parser.parse_args(("config", "list", "--json")).json_output is True

    child_args = parser.parse_args(
        ("exec", "--", "python", "train.py", "--json")
    )
    assert child_args.json_output is False
    assert child_args.command_argv == ["--", "python", "train.py", "--json"]


def test_exec_parses_timestamped_name_option_without_confusing_exact_name():
    parser, _commands = build_parser("pyr", show_all_commands=True)

    parsed = parser.parse_args(("exec", "-nt", "smoke", "--", "python", "-V"))

    assert parsed.name is None
    assert parsed.name_timestamp == "smoke"
    assert parsed.command_argv == ["--", "python", "-V"]


def test_json_output_is_strict_and_versioned(capsys):
    from pyruns.cli.commands import CliError, _json_dump

    _json_dump({"value": 1})
    assert json.loads(capsys.readouterr().out) == {"schema_version": 1, "value": 1}

    with pytest.raises(CliError, match="strict JSON"):
        _json_dump({"value": float("nan")})
    assert capsys.readouterr().out == ""


def test_broken_pipe_exits_cleanly_without_internal_error(tmp_path, monkeypatch, capsys):
    from pyruns.cli import app, commands

    monkeypatch.chdir(tmp_path)
    silenced = []
    monkeypatch.setattr(
        commands,
        "dispatch",
        lambda *_args: (_ for _ in ()).throw(BrokenPipeError()),
    )
    monkeypatch.setattr(app, "_silence_broken_pipe", lambda: silenced.append(True))

    assert app.main(["status"]) == 0
    assert silenced == [True]
    assert "internal error" not in capsys.readouterr().err


def test_run_and_export_advertise_one_clear_canonical_option_set():
    _parser, commands = build_parser("pyr", show_all_commands=True)

    run_help = commands["run"].format_help()
    assert "--config CONFIG" in run_help
    assert re.search(r"-j(?: N)?, --jobs N\b", run_help)
    assert "--backend" not in run_help
    assert "--from" not in run_help
    assert "--workers" not in run_help
    assert "--mode" not in run_help

    export_help = commands["export"].format_help()
    config_help = commands["config"].format_help()
    log_help = commands["log"].format_help()
    assert "--format {csv,json}" in export_help
    assert "--json" not in export_help
    assert "--json" not in config_help
    assert "with --path" in log_help


def test_command_help_distinguishes_cancelling_from_stopping_observation():
    _parser, commands = build_parser("pyr", show_all_commands=True)

    assert "Ctrl+C during foreground exec requests cancellation" in commands["exec"].format_help()
    assert "Ctrl+C while waiting requests cancellation" in commands["run"].format_help()
    assert "Ctrl+C stops following and returns 130; it does not stop the task" in commands[
        "log"
    ].format_help()
    assert "Timeout or Ctrl+C stops waiting only; the tasks continue running" in commands[
        "wait"
    ].format_help()


def test_removed_run_option_spellings_are_rejected():
    parser, _commands = build_parser("pyr", show_all_commands=True)

    canonical = parser.parse_args(("run", "--config", "sweep.yaml", "--jobs", "3"))
    assert canonical.config == "sweep.yaml"
    assert canonical.jobs == 3

    for removed in (
        ("run", "--from", "sweep.yaml"),
        ("run", "task", "--backend", "process"),
        ("run", "task", "--workers", "3"),
        ("run", "task", "--mode", "process"),
        ("run", "task", "-m", "process"),
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(removed)


@pytest.mark.parametrize(
    "args",
    [
        ("--json", "status"),
        ("config", "--json", "list"),
        ("export", "--json"),
        ("ui", "--json"),
        ("help", "--json"),
    ],
)
def test_removed_json_scopes_are_rejected(args, tmp_path):
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 2
    assert "--json" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("-p", "8099", "ui"), "-p/--port belongs to ui and dev"),
        (("--port=8099", "ui"), "-p/--port belongs to ui and dev"),
        (("--browser", "ui"), "--browser belongs to ui and dev"),
        (("--no-browser", "ui"), "--no-browser belongs to ui and dev"),
        (("-n", "5", "ls"), "unrecognized global option: -n"),
        (("-w", "shell", "init"), "init does not use -w/--workspace"),
        (("-w", "shell", "config", "list"), "config does not use -w/--workspace"),
        (("-w", "shell", "metrics"), "metrics does not use -w/--workspace"),
        (("-w", "shell", "dev", "train.py"), "dev does not use -w/--workspace"),
        (("-w", "shell", "help"), "help does not use -w/--workspace"),
    ],
)
def test_command_options_before_command_have_actionable_errors(args, message, tmp_path):
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 2
    assert message in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


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

    (tmp_path / "_pyruns_" / "train").rename(tmp_path / "_pyruns_" / "named.py")
    launched.clear()
    assert main(["ui", "named.py"]) == 0
    assert launched == {
        "start_path": "/",
        "port": None,
        "open_browser": None,
    }
    assert (tmp_path / "_pyruns_" / ".active_workspace").read_text(
        encoding="utf-8"
    ) == "named.py"

    removed = _run_cli(tmp_path, "ui", "--shell")
    assert removed.returncode == 2
    assert "unrecognized arguments: --shell" in removed.stderr

    old_workspace_form = _run_cli(tmp_path, "-w", "train", "ui")
    assert old_workspace_form.returncode == 2
    assert "ui does not use -w/--workspace" in old_workspace_form.stderr


def test_bare_ui_reuses_nearest_project_from_nested_directory(tmp_path, monkeypatch):
    from pyruns.cli import commands
    from pyruns.web.runtime import PyrunsRuntime

    project_root = tmp_path / "_pyruns_"
    project_root.mkdir()
    (tmp_path / "train.py").write_text("print('train')\n", encoding="utf-8")
    nested = tmp_path / "src" / "package"
    nested.mkdir(parents=True)
    launched = {}

    monkeypatch.chdir(nested)
    monkeypatch.delenv(commands.ENV_KEY_ROOT, raising=False)
    monkeypatch.setattr(
        commands,
        "_launch_ui",
        lambda **kwargs: launched.update(kwargs) or 0,
    )

    assert main(["ui", "--no-browser"]) == 0
    assert launched == {
        "start_path": commands.launcher_query(),
        "port": None,
        "open_browser": False,
    }
    assert Path(os.environ[commands.ENV_KEY_ROOT]) == project_root
    assert not (nested / "_pyruns_").exists()

    runtime = PyrunsRuntime()
    try:
        assert {item["label"] for item in runtime.list_launcher_scripts()} == {"train.py"}
    finally:
        runtime.shutdown()


def test_ui_treats_an_existing_workspace_ending_in_py_as_a_workspace(
    tmp_path,
    monkeypatch,
):
    from pyruns.cli import commands

    script = tmp_path / "train.py"
    script.write_text("print('train')\n", encoding="utf-8")
    source_workspace = Path(bootstrap_workspace(str(script)))
    dotted_workspace = source_workspace.parent / "archive.py"
    dotted_workspace.mkdir()
    (dotted_workspace / SCRIPT_INFO_FILENAME).write_text(
        (source_workspace / SCRIPT_INFO_FILENAME).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    launched = {}

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        commands,
        "_launch_ui",
        lambda **kwargs: launched.update(kwargs) or 0,
    )

    assert main(["ui", str(dotted_workspace), "--no-browser"]) == 0
    assert launched == {
        "start_path": "/",
        "port": None,
        "open_browser": False,
    }
    assert (tmp_path / "_pyruns_" / ".active_workspace").read_text(
        encoding="utf-8"
    ) == "archive.py"


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
    assert f"unknown command '{removed}'" in result.stderr
    assert "choose from" not in result.stderr
    if removed in {"create", "remove", "clis"}:
        assert "Did you mean" not in result.stderr
    assert result.stdout == ""


def test_run_rejects_workspace_flag_after_command(tmp_path):
    result = _run_cli(tmp_path, "run", "-w", "2")
    assert result.returncode == 2
    assert "unrecognized arguments: -w" in result.stderr


def test_exec_argv_requires_the_standard_separator(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["exec", sys.executable, "-V"]) == 2
    stderr = capsys.readouterr().err

    assert "usage: pyr exec" in stderr
    assert "exec argv form requires '--' before COMMAND" in stderr
    assert not (tmp_path / "_pyruns_").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("-w", "shell"), "-w/--workspace requires a command"),
    ],
)
def test_bare_context_options_are_not_silently_ignored(tmp_path, args, message):
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 2
    assert message in result.stderr
    assert result.stdout == ""


@pytest.mark.parametrize(
    "args",
    [
        ("-C", "", "ls"),
        ("-w", "", "ls"),
        ("init", ""),
        ("init", "train.py", "--config", ""),
        ("exec", "--name", "", "--", "python", "-V"),
        ("exec", "--name-timestamp", "", "--", "python", "-V"),
        ("exec", "--env-file", "", "--", "python", "-V"),
        ("add", ""),
        ("add", "config.yaml", "--name", ""),
        ("run", "task", "--config", ""),
        ("run", "--config", "config.yaml", "--name", ""),
        ("ui", ""),
        ("ui", "train.py", "--config", ""),
        ("dev", ""),
        ("dev", "train.py", "--config", ""),
    ],
)
def test_explicit_empty_values_fail_instead_of_selecting_a_default(tmp_path, args):
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 2
    assert "value must not be empty" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (("run", "task", "--config", "config.yaml"), "either exact TASK names or --config"),
        (("run", "--name", "named"), "--name is only valid together with --config"),
        (("run",), "run requires at least one TASK or --config CONFIG"),
        (("exec", "--dry-run", "--detach", "--", "python", "-V"), "either --dry-run or --detach"),
        (("run", "--config", "config.yaml", "--dry-run", "--detach"), "either --dry-run or --detach"),
        (("log", "task", "--follow", "--run", "2"), "--follow cannot be combined with --run"),
        (("log", "task", "--follow", "--path"), "--follow cannot be combined with --path"),
        (("log", "task", "--json"), "log --json requires --path"),
        (("config",), "config requires an action"),
    ],
)
def test_invalid_option_combinations_fail_before_workspace_lookup(
    tmp_path,
    args,
    message,
):
    result = _run_cli(tmp_path, *args)

    assert result.returncode == 2
    assert message in result.stderr
    assert "no Pyruns project found" not in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_unknown_options_fail_with_usage_on_stderr(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(tmp_path, "-w", "shell", "ls", "--unknown")
    assert result.returncode == 2
    assert "unrecognized arguments: --unknown" in result.stderr
    assert result.stdout == ""


def test_exec_rejects_exact_and_timestamped_names_together(tmp_path):
    result = _run_cli(
        tmp_path,
        "exec",
        "-n",
        "exact",
        "-nt",
        "prefixed",
        "--",
        "python",
        "-V",
    )

    assert result.returncode == 2
    assert "not allowed with argument" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_json_is_command_scoped_and_long_options_require_exact_spelling(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    misplaced = _run_cli(tmp_path, "--json", "status")
    assert misplaced.returncode == 2
    assert "--json is command-specific" in misplaced.stderr

    abbreviated = _run_cli(tmp_path, "--js", "status")
    assert abbreviated.returncode == 2
    assert "--js" in abbreviated.stderr

    exact = _run_cli(tmp_path, "status", "--json")
    assert exact.returncode == 0, exact.stderr
    assert json.loads(exact.stdout)["kind"] == "shell"


def test_json_is_available_only_after_supported_commands(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    command_form = _run_cli(tmp_path, "-w", "shell", "status", "--json")
    assert command_form.returncode == 0, command_form.stderr
    assert json.loads(command_form.stdout)["kind"] == "shell"

    nested_form = _run_cli(tmp_path, "config", "list", "--json")
    assert nested_form.returncode == 0, nested_form.stderr
    assert isinstance(json.loads(nested_form.stdout), dict)

    dry_run = _run_cli(
        tmp_path,
        "exec",
        "--dry-run",
        "--json",
        "-n",
        "natural-json",
        "--",
        sys.executable,
        "-V",
    )
    assert dry_run.returncode == 0, dry_run.stderr
    assert json.loads(dry_run.stdout)["dry_run"] is True

    status_help = _run_cli(tmp_path, "status", "--help")
    ui_help = _run_cli(tmp_path, "ui", "--help")
    assert "--json" in status_help.stdout
    assert "\n  --json" not in ui_help.stdout


def test_command_long_options_require_exact_spelling(tmp_path):
    abbreviated = _run_cli(
        tmp_path,
        "exec",
        "--dry",
        "-n",
        "planned",
        "--",
        sys.executable,
        "-V",
    )
    assert abbreviated.returncode == 2
    assert "--dry" in abbreviated.stderr

    exact = _run_cli(
        tmp_path,
        "exec",
        "--dry-run",
        "-n",
        "planned",
        "--",
        sys.executable,
        "-V",
    )
    assert exact.returncode == 0, exact.stderr
    assert not (tmp_path / "_pyruns_").exists()


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


def test_init_missing_config_does_not_leave_a_workspace(tmp_path):
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")

    result = _run_cli(
        tmp_path,
        "init",
        str(script),
        "--config",
        "missing.yaml",
    )

    assert result.returncode == 1
    assert "not found" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_init_invalid_yaml_does_not_leave_a_workspace(tmp_path):
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    config = tmp_path / "invalid.yaml"
    config.write_text("broken: [\n", encoding="utf-8")

    result = _run_cli(tmp_path, "init", str(script), "--config", str(config))

    assert result.returncode == 1
    assert "Invalid YAML" in result.stderr
    assert "invalid.yaml" in result.stderr
    assert "internal error" not in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_init_load_script_without_template_does_not_leave_a_workspace(tmp_path):
    script = tmp_path / "train.py"
    script.write_text("import pyruns\nconfig = pyruns.load()\n", encoding="utf-8")

    result = _run_cli(tmp_path, "init", str(script))

    assert result.returncode == 1
    assert "needs a YAML template" in result.stderr
    assert not (tmp_path / "_pyruns_").exists()


def test_init_shell_outputs_workspace_and_status_is_json(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    assert main(["init", "--json"]) == 0
    created = json.loads(capsys.readouterr().out)
    assert created["schema_version"] == 1
    assert created["kind"] == "shell"
    assert Path(created["workspace"]).is_dir()

    assert main(["-w", "shell", "status", "--json"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["schema_version"] == 1
    assert status["kind"] == "shell"
    assert status["total"] == 0


def test_exec_with_explicit_shell_selector_bootstraps_fresh_project(tmp_path):
    result = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "exec",
        "--json",
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
        "-w",
        "shell",
        "exec",
        "--dry-run",
        "--json",
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
        "schema_version": 1,
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


def test_exec_dry_run_reports_automatic_name_collision_without_writing(
    tmp_path,
    monkeypatch,
    capsys,
):
    from pyruns.cli import commands
    from pyruns.core.task_generator import TaskGenerator

    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "get_now_str", lambda: "2026-08-15_12-34-56")
    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "task_2026-08-15_12-34-56", "echo old\n"
    )
    before = {path.name for path in (workspace / TASKS_DIR).iterdir()}

    result = main(["exec", "--dry-run", "--json", "--", sys.executable, "-V"])
    captured = capsys.readouterr()

    assert result == 0, captured.out + captured.err
    task = json.loads(captured.out)["task"]
    assert task == {
        "requested_name": "task_2026-08-15_12-34-56",
        "planned_name": None,
        "name_is_exact": False,
        "name_available": False,
    }
    assert {path.name for path in (workspace / TASKS_DIR).iterdir()} == before


def test_exec_dry_run_appends_timestamp_to_requested_prefix(tmp_path):
    result = _run_cli(
        tmp_path,
        "exec",
        "--dry-run",
        "--json",
        "-nt",
        "smoke",
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    task = json.loads(result.stdout)["task"]
    assert re.fullmatch(
        r"smoke_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}",
        task["requested_name"],
    )
    assert task["planned_name"] == task["requested_name"]
    assert task["name_is_exact"] is False
    assert task["name_available"] is True
    assert not (tmp_path / "_pyruns_").exists()


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

    identity = commands._TaskRunIdentity(run_index=1, runner_id="host:1:token")
    monkeypatch.setattr(
        commands,
        "_bound_task_record",
        lambda _task, observed: {"status": "completed"}
        if observed == identity
        else pytest.fail("follow must retain the captured run identity"),
    )
    monkeypatch.setattr(
        commands,
        "_resolve_log_reference",
        lambda _task, **_kwargs: commands._LogReference("run1.log", 1, "run"),
    )

    def read_log(_path, offset):
        calls.append(offset)
        return next(offsets)

    monkeypatch.setattr(commands, "_write_available_log", read_log)

    assert commands._follow_task({"name": "fast"}, identity=identity)["status"] == "completed"
    assert calls == [0, 0, 7, 7, 7]


def test_follow_task_reads_only_new_queue_bytes_and_expected_run(monkeypatch):
    from pyruns.cli import commands

    records = iter([
        {"status": "queued"},
        {"status": "running"},
        {"status": "completed"},
    ])
    reads = []

    identity = commands._TaskRunIdentity(
        run_index=2,
        runner_id="host:1:token",
        started_queued=True,
    )
    monkeypatch.setattr(
        commands,
        "_bound_task_record",
        lambda _task, observed: next(records)
        if observed == identity
        else pytest.fail("follow must retain the captured run identity"),
    )
    monkeypatch.setattr(
        commands,
        "_resolve_log_reference",
        lambda _task, run_index=None, **_kwargs: commands._LogReference(
            "queue.log" if run_index is None else f"run{run_index}.log",
            None if run_index is None else run_index,
            "queue" if run_index is None else "run",
        ),
    )

    def read_log(path, offset):
        reads.append((path, offset))
        return offset

    monkeypatch.setattr(commands, "_write_available_log", read_log)
    monkeypatch.setattr(
        commands.os.path,
        "isfile",
        lambda path: path == "run2.log",
    )
    monkeypatch.setattr(commands.time, "sleep", lambda _seconds: None)

    result = commands._follow_task(
        {"name": "rerun"},
        identity=identity,
        initial_queue_offset=41,
    )

    assert result["status"] == "completed"
    assert reads[0] == ("queue.log", 41)
    assert all(path != "run1.log" for path, _offset in reads)
    assert any(path == "run2.log" and offset == 0 for path, offset in reads)


def test_write_available_log_does_not_duplicate_crlf_rows(tmp_path, capsys):
    from pyruns.cli import commands

    log_path = tmp_path / "run.log"
    content = b"first\r\nsecond\r\nprogress 1%\rprogress 100%"
    log_path.write_bytes(content)

    offset = commands._write_available_log(str(log_path), 0)

    assert offset == len(content)
    assert capsys.readouterr().out == "first\nsecond\nprogress 1%\rprogress 100%"


def test_explicit_run_log_never_falls_back_to_an_older_log(tmp_path):
    from pyruns.cli import commands

    task_dir = tmp_path / TASKS_DIR / "exact-log"
    log_dir = task_dir / RUN_LOGS_DIR
    log_dir.mkdir(parents=True)
    (task_dir / TASK_INFO_FILENAME).write_text(
        json.dumps({"name": "exact-log", "status": "running", "run_index": 2}),
        encoding="utf-8",
    )
    (log_dir / "run1.log").write_text("old run\n", encoding="utf-8")

    reference = commands._resolve_log_reference(
        {"name": "exact-log", "dir": str(task_dir)},
        run_index=2,
    )

    assert reference.path == str(log_dir / "run2.log")
    assert reference.run_index == 2
    assert reference.kind == "run"


def test_workspace_discovery_walks_upward(tmp_path, monkeypatch, capsys):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert Path(payload["workspace"]).name == "_shell_"


def test_exec_uses_current_directory_instead_of_ancestor_workspace(tmp_path):
    ancestor_workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    nested = tmp_path / "nested"
    nested.mkdir()

    result = _run_cli(
        nested,
        "exec",
        "--name",
        "local-shell",
        "--",
        sys.executable,
        "-c",
        "print('local workspace')",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    local_task = nested / "_pyruns_" / "_shell_" / TASKS_DIR / "local-shell"
    assert local_task.is_dir()
    assert not (ancestor_workspace / TASKS_DIR / "local-shell").exists()


def test_multiple_workspaces_require_explicit_selection(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    script = tmp_path / "train.py"
    script.write_text("print('ok')\n", encoding="utf-8")
    bootstrap_workspace(str(script))
    result = _run_cli(tmp_path, "status")
    assert result.returncode == 1
    assert "multiple workspaces found" in result.stderr
    assert "-w/--workspace" in result.stderr


def test_typo_suggestions_preserve_exact_command_workspace_and_task_matching(tmp_path):
    command_typo = _run_cli(tmp_path, "staus")
    help_topic_typo = _run_cli(tmp_path, "help", "staus")
    config_action_typo = _run_cli(tmp_path, "config", "gt")
    assert command_typo.returncode == 2
    assert "unknown command 'staus'" in command_typo.stderr
    assert "Did you mean 'status'?" in command_typo.stderr
    assert "choose from" not in command_typo.stderr
    assert help_topic_typo.returncode == 2
    assert "unknown command 'staus'" in help_topic_typo.stderr
    assert "Did you mean 'status'?" in help_topic_typo.stderr
    assert config_action_typo.returncode == 2
    assert "unknown action 'gt'" in config_action_typo.stderr
    assert "Did you mean 'get'?" in config_action_typo.stderr
    assert not (tmp_path / "_pyruns_").exists()

    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "train", "echo ok\n"
    )
    workspace_typo = _run_cli(tmp_path, "-w", "sheel", "status")
    workspace_whitespace = _run_cli(tmp_path, "-w", " shell", "status")
    by_index = _run_cli(tmp_path, "-w", "shell", "show", "1")
    task_typo = _run_cli(tmp_path, "-w", "shell", "show", "trian")
    task_whitespace = _run_cli(tmp_path, "-w", "shell", "show", " train")

    assert workspace_typo.returncode == 1
    assert "workspace not found: sheel" in workspace_typo.stderr
    assert "Did you mean 'shell'?" in workspace_typo.stderr
    assert workspace_whitespace.returncode == 1
    assert "workspace not found:  shell" in workspace_whitespace.stderr
    assert by_index.returncode == 1
    assert task_typo.returncode == 1
    assert "task not found" in by_index.stderr
    assert "task not found: trian" in task_typo.stderr
    assert "Did you mean 'train'?" in task_typo.stderr
    assert task_whitespace.returncode == 2
    assert "Task name cannot start or end with whitespace" in task_whitespace.stderr


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
    info = load_task_info(str(task_dir))
    assert info["status"] == "completed"
    assert info["config_file"] in SHELL_CONFIG_FILENAMES
    assert (task_dir / info["config_file"]).is_file()


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


_NATIVE_SCRIPT_CASES = (
    [("powershell", ".ps1"), ("cmd", ".cmd"), ("cmd", ".bat")]
    if os.name == "nt"
    else [("sh", ".sh")]
)


def _native_script_contract(
    kind: str,
    script: Path,
    arguments: list[str],
) -> tuple[str, list[str] | str]:
    if kind == "sh":
        executable = shutil.which("sh") or shutil.which("bash")
        if not executable:
            pytest.skip("No native sh or Bash executable is available")
        text = (
            "#!/bin/sh\n"
            "[ \"$1\" = fail ] && exit 7\n"
            "printf 'cwd=%s\\n' \"$PWD\"\n"
            "printf 'script=%s|%s|%s\\n' \"$1\" \"$2\" \"$3\"\n"
            "printf 'env=%s\\n' \"$PYRUNS_SCRIPT_ENV\"\n"
        )
        return text, [executable, str(script), *arguments]

    if kind == "powershell":
        executable = shutil.which("pwsh") or shutil.which("powershell")
        if not executable:
            pytest.skip("PowerShell is unavailable")
        text = (
            "param([string]$First, [string]$Second, [string]$Third)\n"
            "if ($First -eq 'fail') { exit 7 }\n"
            'Write-Output "cwd=$((Get-Location).Path)"\n'
            'Write-Output "script=$First|$Second|$Third"\n'
            'Write-Output "env=$env:PYRUNS_SCRIPT_ENV"\n'
        )
        return text, [
            executable,
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
            *arguments,
        ]

    executable = os.environ.get("COMSPEC") or shutil.which("cmd.exe")
    if not executable:
        pytest.skip("cmd.exe is unavailable")
    text = (
        "@echo off\n"
        "if \"%~1\"==\"fail\" exit /b 7\n"
        "setlocal DisableDelayedExpansion\n"
        "set \"first=%~1\"\n"
        "set \"second=%~2\"\n"
        "set \"third=%~3\"\n"
        "setlocal EnableDelayedExpansion\n"
        "echo cwd=!CD!\n"
        "echo script=!first!^|!second!^|!third!\n"
        "echo env=!PYRUNS_SCRIPT_ENV!\n"
    )
    quoted_command = " ".join(f'"{value}"' for value in [str(script), *arguments])
    return text, f'{subprocess.list2cmdline([executable])} /d /s /v:off /c "{quoted_command}"'


def _script_contract_lines(output: str) -> list[str]:
    return [
        line.strip()
        for line in output.splitlines()
        if line.startswith(("cwd=", "script=", "env="))
    ]


@pytest.mark.parametrize(("kind", "suffix"), _NATIVE_SCRIPT_CASES)
def test_exec_script_file_matches_direct_execution_and_rerun(tmp_path, kind, suffix):
    project = tmp_path / "workspace with spaces"
    project.mkdir()
    bootstrap_shell_workspace(str(project / "_pyruns_"))
    script_dir = project / "scripts with spaces"
    script_dir.mkdir()
    script = script_dir / f"run check{suffix}"
    literal_variable = "%PYRUNS_UNDEFINED_ARG%" if kind == "cmd" else "dollar$HOME"
    arguments = ["value with spaces", "x&y", literal_variable]
    script_text, direct_command = _native_script_contract(kind, script, arguments)
    script.write_text(script_text, encoding="utf-8")

    direct_env = _source_env()
    direct_env.pop("PYRUNS_UNDEFINED_ARG", None)
    direct_env["PYRUNS_SCRIPT_ENV"] = "persisted-env"
    direct_options = {
        "cwd": project,
        "env": direct_env,
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": 20,
        "creationflags": subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    }
    direct = subprocess.run(direct_command, **direct_options)
    assert direct.returncode == 0, direct.stdout + direct.stderr
    direct_lines = _script_contract_lines(direct.stdout)
    assert direct_lines == [
        f"cwd={project}",
        f"script=value with spaces|x&y|{literal_variable}",
        "env=persisted-env",
    ]

    task_name = f"direct-{kind}"
    first = _run_cli(
        project,
        "exec",
        "--name",
        task_name,
        "--env",
        "PYRUNS_SCRIPT_ENV=persisted-env",
        "--",
        str(script.relative_to(project)),
        *arguments,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    assert _script_contract_lines(first.stdout) == direct_lines

    rerun_cwd = tmp_path / "rerun elsewhere"
    rerun_cwd.mkdir()
    workspace = project / "_pyruns_" / "_shell_"
    rerun = _run_cli(rerun_cwd, "-w", str(workspace), "run", task_name)
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert _script_contract_lines(rerun.stdout) == direct_lines

    task_dir = workspace / TASKS_DIR / task_name
    info = load_task_info(str(task_dir))
    assert info["exit_codes"] == [0, 0]
    assert info["env"] == {"PYRUNS_SCRIPT_ENV": "persisted-env"}
    assert Path(info["workdir"]) == project
    assert Path(info["script"]) == script
    assert all("script none" not in state for state in info["source_states"])

    _, direct_failure_command = _native_script_contract(kind, script, ["fail"])
    direct_failure = subprocess.run(direct_failure_command, **direct_options)
    assert direct_failure.returncode == 7

    failure_name = f"fail-{kind}"
    first_failure = _run_cli(
        project,
        "exec",
        "--name",
        failure_name,
        "--",
        str(script.relative_to(project)),
        "fail",
    )
    assert first_failure.returncode == 1
    rerun_failure = _run_cli(
        rerun_cwd,
        "-w",
        str(workspace),
        "run",
        failure_name,
    )
    assert rerun_failure.returncode == 1
    failure_info = load_task_info(str(workspace / TASKS_DIR / failure_name))
    assert failure_info["status"] == "failed"
    assert failure_info["exit_codes"] == [7, 7]


@pytest.mark.skipif(os.name != "nt", reason="requires Windows Bash or WSL")
def test_exec_runs_sh_file_on_windows_when_bash_is_available(tmp_path):
    wsl = shutil.which("wsl.exe")
    if not wsl:
        pytest.skip("wsl.exe is unavailable")

    project = tmp_path / "workspace with spaces"
    project.mkdir()
    workspace = Path(bootstrap_shell_workspace(str(project / "_pyruns_")))
    settings = workspace.parent / "_pyruns_settings.yaml"
    settings.write_text(
        "shell_mode: custom\n"
        f"shell_executable: {json.dumps(wsl)}\n",
        encoding="utf-8",
    )
    script = project / "run check.sh"
    script.write_text(
        "#!/bin/sh\n"
        "printf 'cwd=%s\\n' \"$PWD\"\n"
        "printf 'script=%s|%s\\n' \"$1\" \"$2\"\n"
        "printf 'env=%s\\n' \"$PYRUNS_SCRIPT_ENV\"\n",
        encoding="utf-8",
    )
    translated = subprocess.run(
        [wsl, "wslpath", "-a", str(script)],
        capture_output=True,
        text=True,
        timeout=10,
        check=True,
        creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout.strip()
    direct_env = _source_env()
    direct_env["PYRUNS_SCRIPT_ENV"] = "wsl-env-ok"
    direct_env["WSLENV"] = "PYRUNS_SCRIPT_ENV"
    direct = subprocess.run(
        [wsl, "--exec", "/bin/bash", translated, "value with spaces", "x&y"],
        cwd=project,
        env=direct_env,
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    assert direct.returncode == 0, direct.stdout + direct.stderr

    result = _run_cli(
        project,
        "exec",
        "--name",
        "direct-windows-sh",
        "--env",
        "PYRUNS_SCRIPT_ENV=wsl-env-ok",
        "--",
        str(script),
        "value with spaces",
        "x&y",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    markers = [
        f"cwd={translated.rsplit('/', 1)[0]}",
        "script=value with spaces|x&y",
        "env=wsl-env-ok",
    ]
    assert all(marker in direct.stdout for marker in markers)
    assert all(marker in result.stdout for marker in markers)

    rerun_cwd = tmp_path / "rerun elsewhere"
    rerun_cwd.mkdir()
    rerun = _run_cli(
        rerun_cwd,
        "-w",
        str(workspace),
        "run",
        "direct-windows-sh",
    )
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    assert all(marker in rerun.stdout for marker in markers)
    info = load_task_info(str(workspace / TASKS_DIR / "direct-windows-sh"))
    assert info["exit_codes"] == [0, 0]
    assert info["env"] == {"PYRUNS_SCRIPT_ENV": "wsl-env-ok"}
    assert Path(info["workdir"]) == project


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
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is unavailable")
    first = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "stored-shell",
        "-c",
        "Write-Output stored-shell-ok",
        env_overrides={ENV_KEY_CLI_SHELL_EXECUTABLE: powershell},
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


@pytest.mark.skipif(os.name != "nt", reason="requires PowerShell")
def test_powershell_shell_expression_flushes_formatted_object_output(tmp_path):
    powershell = shutil.which("pwsh") or shutil.which("powershell")
    if not powershell:
        pytest.skip("PowerShell is unavailable")

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "formatted-output",
        "-c",
        "Get-Location; Write-Output 'flush-object-123'",
        env_overrides={ENV_KEY_CLI_SHELL_EXECUTABLE: powershell},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert str(tmp_path) in result.stdout
    assert "flush-object-123" in result.stdout
    log_text = (
        tmp_path
        / "_pyruns_"
        / "_shell_"
        / TASKS_DIR
        / "formatted-output"
        / RUN_LOGS_DIR
        / "run1.log"
    ).read_text(encoding="utf-8")
    assert str(tmp_path) in log_text
    assert "flush-object-123" in log_text


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
        encoding="utf-8",
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
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == expected


def test_exec_command_string_preserves_expression(tmp_path, monkeypatch, capsys):
    from pyruns.cli import commands

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(commands, "_find_project_root", lambda: None)
    shell_kind = commands.get_shell_runtime_for_workspace()["terminal_kind"]
    if shell_kind == "powershell":
        expression = "Write-Output alpha; Write-Output beta"
    elif shell_kind == "cmd":
        expression = "echo alpha & echo beta"
    else:
        expression = "printf 'alpha\\n'; printf 'beta\\n'"
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

    shown = _run_cli(tmp_path, "-w", "shell", "show", "argv-failure@1", "--json")
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
    release_file = tmp_path / "release-detached-task"
    child_code = "\n".join([
        "from pathlib import Path",
        "import time",
        f"release_file = Path({str(release_file)!r})",
        "deadline = time.monotonic() + 15",
        "while not release_file.exists():",
        "    if time.monotonic() >= deadline:",
        "        raise SystemExit(2)",
        "    time.sleep(0.05)",
    ])
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "detached",
        "--detach",
        "--",
        sys.executable,
        "-c",
        child_code,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "detached"
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "detached"
    assert load_task_info(str(task_dir))["status"] in {"queued", "running"}
    release_file.touch()
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
        "exec",
        "--json",
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
        "exec",
        "--json",
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


def test_exec_without_name_creates_timestamped_task(tmp_path):
    result = _run_cli(tmp_path, "exec", "--", sys.executable, "-V")

    assert result.returncode == 0, result.stdout + result.stderr
    tasks_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR
    names = [path.name for path in tasks_dir.iterdir() if path.is_dir()]
    assert len(names) == 1
    assert re.fullmatch(
        r"task_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}",
        names[0],
    )
    assert f"created {names[0]}" in result.stderr


def test_exec_missing_program_preserves_workspace_shell_error(tmp_path):
    missing = "__pyruns_missing_program_9f0d8a__"
    result = _run_cli(tmp_path, "exec", "--name", "missing-program", "--", missing)

    assert result.returncode == 1
    assert missing in result.stdout
    if os.name == "nt":
        assert any(
            marker in result.stdout
            for marker in (
                "CommandNotFoundException",
                "is not recognized as a name",
                "无法将",
            )
        )
    else:
        assert "not found" in result.stdout.lower()
    assert "Command:" not in result.stdout
    assert "Hint:" not in result.stdout
    assert "Full details:" not in result.stdout

    error_log = (
        tmp_path
        / "_pyruns_"
        / "_shell_"
        / TASKS_DIR
        / "missing-program"
        / RUN_LOGS_DIR
        / ERROR_LOG_FILENAME
    )
    error_text = error_log.read_text(encoding="utf-8")
    assert "reason=exit_code" in error_text
    assert "Traceback:" not in error_text


def test_exec_matches_direct_argv_environment_and_creation_workdir(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    nested = tmp_path / "nested" / "deeper"
    nested.mkdir(parents=True)
    code = (
        "import json,os,sys; "
        "print(json.dumps({'cwd':os.getcwd(),'argv':sys.argv[1:],"
        "'marker':os.environ.get('PYRUNS_ENV_MARKER')},sort_keys=True))"
    )
    arguments = [
        "value with spaces",
        "x&y",
        "%PATH%",
        "$HOME",
        'quote"value',
        "trailing\\",
        "\u4e2d\u6587\u53c2\u6570",
    ]
    command = [sys.executable, "-c", code, *arguments]
    direct_env = _source_env()
    direct_env["PYRUNS_ENV_MARKER"] = "same-env"
    direct = subprocess.run(
        command,
        cwd=nested,
        env=direct_env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    assert direct.returncode == 0, direct.stdout + direct.stderr
    direct_payload = json.loads(direct.stdout)

    first = _run_cli(
        nested,
        "exec",
        "--name",
        "cwd-argv",
        "--env",
        "PYRUNS_ENV_MARKER=same-env",
        "--",
        *command,
    )
    assert first.returncode == 0, first.stdout + first.stderr
    first_payload = next(
        json.loads(line)
        for line in first.stdout.splitlines()
        if line.startswith("{")
    )
    assert first_payload == direct_payload == {
        "argv": arguments,
        "cwd": str(nested),
        "marker": "same-env",
    }

    workspace = nested / "_pyruns_" / "_shell_"
    task_dir = workspace / TASKS_DIR / "cwd-argv"
    info = load_task_info(str(task_dir))
    assert info["command_mode"] == "argv"
    assert info["cmd"] == command
    assert info["env"] == {"PYRUNS_ENV_MARKER": "same-env"}
    assert Path(info["workdir"]) == nested

    rerun_cwd = tmp_path / "rerun"
    rerun_cwd.mkdir()
    rerun = _run_cli(rerun_cwd, "-w", str(workspace), "run", "cwd-argv")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    rerun_payload = next(
        json.loads(line)
        for line in rerun.stdout.splitlines()
        if line.startswith("{")
    )
    assert rerun_payload == direct_payload


def test_exec_command_string_consumes_an_unquoted_tail_and_rejects_separator(tmp_path):
    result = _run_cli(
        tmp_path,
        "exec",
        "--dry-run",
        "--json",
        "-c",
        "echo",
        "hello",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["command_mode"] == "shell"
    assert payload["shell_expression"] == "echo hello"
    assert not (tmp_path / "_pyruns_").exists()

    separator = _run_cli(tmp_path, "exec", "-c", "echo hello", "--")
    assert separator.returncode == 2
    assert "-c/--command cannot be combined with '--' or argv arguments" in separator.stderr
    assert not (tmp_path / "_pyruns_").exists()

    removed = _run_cli(tmp_path, "exec", "--shell", "echo hello")
    assert removed.returncode == 2
    assert "unrecognized arguments: --shell" in removed.stderr


def test_windows_legacy_command_line_recovery_preserves_shell_quotes():
    from pyruns.cli.commands import _extract_windows_shell_command_from_raw

    expression = (
        '$colors=@("Red","Green"); '
        'Write-Host "Test message - $_" -ForegroundColor $colors[0]'
    )
    raw = f'"C:\\Tools\\pyr.exe" exec -c "{expression}"'

    assert _extract_windows_shell_command_from_raw(raw) == expression


@pytest.mark.skipif(os.name != "nt", reason="requires Windows ConPTY")
def test_exec_powershell_shell_expression_preserves_host_colors(tmp_path):
    import importlib.util

    if importlib.util.find_spec("winpty") is None:
        pytest.skip("pywinpty is unavailable")

    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "host-color",
        "-c",
        "Write-Host 'red-text' -ForegroundColor Red",
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "red-text" in result.stdout
    assert "\x1b[38;" in result.stdout
    assert "\x1b[2J" not in result.stdout
    assert "\x1b]0;" not in result.stdout
    log_text = (
        tmp_path
        / "_pyruns_"
        / "_shell_"
        / TASKS_DIR
        / "host-color"
        / RUN_LOGS_DIR
        / "run1.log"
    ).read_text(encoding="utf-8")
    assert "\x1b[38;" in log_text
    assert "\x1b[2J" not in log_text


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
        "-w",
        "train",
        "add",
        "--json",
        str(config),
        "--name",
        "created",
    )
    assert created.returncode == 0, created.stderr
    payload = json.loads(created.stdout)
    assert [item["name"] for item in payload["created"]] == [
        "created_1-of-2",
        "created_2-of-2",
    ]

    run = _run_cli(
        tmp_path,
        "-w",
        "train",
        "run",
        "--json",
        "--config",
        str(config),
        "--name",
        "run",
        "-j",
        "99",
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
        "-w",
        "train",
        "run",
        "--json",
        "--config",
        str(config),
        "--name",
        "preview",
        "--jobs",
        "99",
        "--dry-run",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["dry_run"] is True
    assert payload["operation"] == "run-config"
    assert payload["task_count"] == 2
    assert payload["jobs"] == 2
    assert [task["planned_name"] for task in payload["tasks"]] == [
        "preview_1-of-2",
        "preview_2-of-2",
    ]
    assert not any(task["name_is_exact"] for task in payload["tasks"])
    assert {path.name for path in tasks_dir.iterdir()} == before


def test_run_dry_run_rejects_existing_task_mode_as_usage(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    result = _run_cli(tmp_path, "run", "existing", "--dry-run")

    assert result.returncode == 2
    assert "run --dry-run requires --config CONFIG" in result.stderr


def test_batch_run_waits_and_aggregates_failure(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.cli import commands
    from pyruns.core.task_generator import TaskGenerator

    generator = TaskGenerator(root_dir=str(workspace / TASKS_DIR))
    ok_command = commands._render_argument_command(
        [sys.executable, "-c", "print(1)"], str(workspace)
    )
    bad_command = commands._render_argument_command(
        [sys.executable, "-c", "raise SystemExit(5)"], str(workspace)
    )
    generator.create_shell_task("batch-ok", ok_command + "\n")
    generator.create_shell_task("batch-bad", bad_command + "\n")
    result = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "run",
        "--json",
        "batch-ok",
        "batch-bad",
        "--jobs",
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
        tmp_path, "-w", "shell", "ls", "--status", "completed", "--json"
    )
    assert listing.returncode == 0
    listed = json.loads(listing.stdout)
    assert listed["count"] == 1
    assert listed["tasks"][0]["name"] == "inspect-me"

    shown = _run_cli(tmp_path, "-w", "shell", "show", "inspect-me", "--json")
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
    assert "--follow cannot be combined with --run" in conflicting.stderr


def test_log_path_rejects_a_pending_task_without_a_log(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "pending-log",
        "echo later\n",
    )

    result = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "log",
        "pending-log",
        "--path",
        "--json",
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert "log does not exist" in result.stderr


def test_pinned_tasks_are_visible_and_stay_first_when_reversed(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    generator = TaskGenerator(root_dir=str(workspace / TASKS_DIR))
    pinned = generator.create_shell_task("alpha-pinned", "echo pinned\n")
    generator.create_shell_task("zulu-normal", "echo normal\n")
    update_task_info(pinned["dir"], lambda info: info.update({"pinned": True}))

    listing = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "ls",
        "--sort",
        "name",
        "--reverse",
        "--json",
    )

    assert listing.returncode == 0, listing.stderr
    records = json.loads(listing.stdout)["tasks"]
    assert [record["name"] for record in records] == ["alpha-pinned", "zulu-normal"]
    assert [record["pinned"] for record in records] == [True, False]

    human = _run_cli(tmp_path, "-w", "shell", "ls", "--sort", "name", "--reverse")
    assert human.returncode == 0, human.stderr
    lines = human.stdout.splitlines()
    assert lines[0].startswith("PIN  STATUS")
    assert lines[1].startswith("*    pending")
    assert "alpha-pinned" in lines[1]

    shown = _run_cli(tmp_path, "-w", "shell", "show", "alpha-pinned")
    assert shown.returncode == 0, shown.stderr
    assert "Pinned:     yes" in shown.stdout


def test_show_json_encodes_yaml_dates_as_iso_8601(tmp_path):
    script = tmp_path / "dated.py"
    script.write_text("print('dated')\n", encoding="utf-8")
    workspace = Path(bootstrap_workspace(str(script)))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_task(
        "dated-config",
        {
            "day": date(2026, 1, 2),
            "started": datetime(2026, 1, 2, 3, 4, 5),
        },
    )

    result = _run_cli(tmp_path, "-w", "dated", "show", "dated-config", "--json")

    assert result.returncode == 0, result.stderr
    config = json.loads(result.stdout)["config"]
    assert config["day"] == "2026-01-02"
    assert config["started"] == "2026-01-02T03:04:05"


def test_human_task_table_truncates_long_names_without_changing_json(capsys):
    from pyruns.cli.commands import _print_human_task_table

    long_name = "experiment-" + "x" * 80
    _print_human_task_table([
        {"name": long_name, "status": "completed", "created_at": "2026-08-09"},
    ])

    lines = capsys.readouterr().out.splitlines()
    assert long_name not in lines[1]
    assert "...  2026-08-09" in lines[1]
    assert len(lines[1].split("  2026", 1)[0]) <= 64


def test_corrupt_task_metadata_remains_visible_and_cannot_run(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    task_dir = workspace / TASKS_DIR / "broken-metadata"
    task_dir.mkdir()
    (task_dir / TASK_INFO_FILENAME).write_text("{not-json", encoding="utf-8")

    listing = _run_cli(tmp_path, "-w", "shell", "ls", "--json")
    assert listing.returncode == 0, listing.stderr
    record = json.loads(listing.stdout)["tasks"][0]
    assert record["name"] == "broken-metadata"
    assert record["status"] == "failed"
    assert "Could not load task metadata" in record["load_error"]

    run = _run_cli(tmp_path, "-w", "shell", "run", "broken-metadata")
    assert run.returncode == 1
    assert "Could not load task metadata" in run.stderr


def test_human_and_json_output_survive_ascii_terminal_encoding(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "unicode-😀",
        "echo ok\n",
    )
    ascii_env = {"PYTHONIOENCODING": "ascii"}

    human = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "ls",
        env_overrides=ascii_env,
    )
    assert human.returncode == 0
    assert r"unicode-\U0001f600" in human.stdout

    machine = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "ls",
        "--json",
        env_overrides=ascii_env,
    )
    assert machine.returncode == 0
    assert json.loads(machine.stdout)["tasks"][0]["name"] == "unicode-😀"


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
        "-w",
        "shell",
        "log",
        "versioned@1",
        "--path",
        "--json",
    )
    path_payload = json.loads(path_result.stdout)
    assert path_payload["task"] == "versioned"
    assert path_payload["run"] == 1
    assert path_payload["kind"] == "run"
    assert path_payload["path"].endswith("run1.log")

    shown = _run_cli(tmp_path, "-w", "shell", "show", "versioned@1", "--json")
    detail = json.loads(shown.stdout)
    assert detail["run_index"] == 2
    assert detail["selected_run"]["index"] == 1
    assert detail["selected_run"]["status"] == "completed"
    assert detail["selected_run"]["start_time"]
    assert detail["selected_run"]["finish_time"]
    assert detail["selected_run"]["duration_seconds"] >= 0
    assert detail["selected_run"]["exit_code"] == 0
    assert detail["selected_run"]["source_state"]
    assert isinstance(detail["selected_run"]["record"], dict)
    assert isinstance(detail["selected_run"]["track"], dict)
    assert detail["selected_run"]["log"].endswith("run1.log")

    shown_with_option = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "show",
        "versioned",
        "--run",
        "1",
        "--json",
    )
    assert shown_with_option.returncode == 0, shown_with_option.stderr
    assert json.loads(shown_with_option.stdout)["selected_run"] == detail["selected_run"]

    conflict = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "log",
        "versioned@1",
        "--run",
        "2",
    )
    show_conflict = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "show",
        "versioned@1",
        "--run",
        "2",
    )
    missing = _run_cli(tmp_path, "-w", "shell", "show", "versioned@3")
    assert conflict.returncode == 2
    assert "cannot be combined" in conflict.stderr
    assert show_conflict.returncode == 2
    assert "cannot be combined" in show_conflict.stderr
    assert missing.returncode == 1
    assert "available runs: 1-2" in missing.stderr


def test_show_reports_the_selected_historical_run_status(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    task = TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "history-status",
        "echo ok\n",
    )

    def set_history(info):
        info.update(
            {
                "status": "completed",
                "run_index": 2,
                "run_statuses": ["failed", "completed"],
                "start_times": ["2026-08-10 10:00:00", "2026-08-10 10:01:00"],
                "finish_times": ["2026-08-10 10:00:01", "2026-08-10 10:01:01"],
                "durations": [1.0, 1.0],
                "exit_codes": [7, 0],
            }
        )

    update_task_info(task["dir"], set_history)

    result = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "show",
        "history-status@1",
        "--json",
    )

    assert result.returncode == 0, result.stderr
    detail = json.loads(result.stdout)
    assert detail["status"] == "completed"
    assert detail["run_index"] == 2
    assert detail["selected_run"]["index"] == 1
    assert detail["selected_run"]["status"] == "failed"
    assert detail["selected_run"]["exit_code"] == 7


def test_log_path_json_reports_the_actual_log_run_and_kind(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    generator = TaskGenerator(root_dir=str(workspace / TASKS_DIR))
    old_log_task = generator.create_shell_task("old-log", "echo old\n")
    error_task = generator.create_shell_task("error-log", "echo error\n")
    pre_run_error_task = generator.create_shell_task(
        "pre-run-error",
        "echo pre-run-error\n",
    )
    queue_task = generator.create_shell_task("queue-log", "echo queue\n")

    update_task_info(
        old_log_task["dir"],
        lambda info: info.update(
            {
                "status": "completed",
                "run_index": 2,
                "run_statuses": ["completed", "completed"],
            }
        ),
    )
    update_task_info(
        error_task["dir"],
        lambda info: info.update(
            {
                "status": "failed",
                "run_index": 1,
                "run_statuses": ["failed"],
            }
        ),
    )
    update_task_info(
        queue_task["dir"],
        lambda info: info.update({"status": "queued", "run_index": 0}),
    )
    update_task_info(
        pre_run_error_task["dir"],
        lambda info: info.update({"status": "failed", "run_index": 0}),
    )

    old_log_dir = Path(old_log_task["dir"]) / RUN_LOGS_DIR
    error_log_dir = Path(error_task["dir"]) / RUN_LOGS_DIR
    pre_run_error_log_dir = Path(pre_run_error_task["dir"]) / RUN_LOGS_DIR
    queue_log_dir = Path(queue_task["dir"]) / RUN_LOGS_DIR
    old_log_dir.mkdir(exist_ok=True)
    error_log_dir.mkdir(exist_ok=True)
    pre_run_error_log_dir.mkdir(exist_ok=True)
    queue_log_dir.mkdir(exist_ok=True)
    (old_log_dir / "run1.log").write_text("old\n", encoding="utf-8")
    (error_log_dir / ERROR_LOG_FILENAME).write_text("error\n", encoding="utf-8")
    (pre_run_error_log_dir / ERROR_LOG_FILENAME).write_text(
        "pre-run-error\n",
        encoding="utf-8",
    )
    (queue_log_dir / QUEUE_LOG_FILENAME).write_text("queued\n", encoding="utf-8")

    def log_reference(name):
        result = _run_cli(
            tmp_path,
            "-w",
            "shell",
            "log",
            name,
            "--path",
            "--json",
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    old_reference = log_reference("old-log")
    error_reference = log_reference("error-log")
    pre_run_error_reference = log_reference("pre-run-error")
    queue_reference = log_reference("queue-log")

    assert old_reference["run"] == 1
    assert old_reference["kind"] == "run"
    assert old_reference["path"].endswith("run1.log")
    assert error_reference["run"] is None
    assert error_reference["kind"] == "error"
    assert error_reference["path"].endswith(ERROR_LOG_FILENAME)
    assert pre_run_error_reference["run"] is None
    assert pre_run_error_reference["kind"] == "error"
    assert pre_run_error_reference["path"].endswith(ERROR_LOG_FILENAME)
    assert queue_reference["run"] is None
    assert queue_reference["kind"] == "queue"
    assert queue_reference["path"].endswith(QUEUE_LOG_FILENAME)

    shown = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "show",
        "pre-run-error",
        "--json",
    )
    assert shown.returncode == 0, shown.stderr
    assert json.loads(shown.stdout)["latest_log"].endswith(ERROR_LOG_FILENAME)


def test_oversized_task_run_reference_is_a_usage_error(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "bounded-run",
        "echo ok\n",
    )
    reference = "bounded-run@" + ("9" * 5000)

    result = _run_cli(tmp_path, "-w", "shell", "show", reference)

    assert result.returncode == 2
    assert "RUN must be between 1 and 1000" in result.stderr
    assert "internal error" not in result.stderr.lower()


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


@pytest.mark.parametrize("name", [" leading-space", "trailing-space "])
def test_task_names_reject_boundary_whitespace_without_creating_workspace(tmp_path, name):
    result = _run_cli(
        tmp_path,
        "exec",
        "--name",
        name,
        "--",
        sys.executable,
        "-V",
    )

    assert result.returncode == 2
    assert "Task name cannot start or end with whitespace" in result.stderr
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


def test_wait_aggregates_results_and_timeout_does_not_stop_the_task(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    for name, code in [
        ("wait-ok", "import time; time.sleep(0.2)"),
        ("wait-failed", "import time; time.sleep(0.2); raise SystemExit(7)"),
    ]:
        submitted = _run_cli(
            tmp_path,
            "exec",
            "--name",
            name,
            "--detach",
            "--",
            sys.executable,
            "-c",
            code,
        )
        assert submitted.returncode == 0, submitted.stderr

    successful = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "wait",
        "--json",
        "wait-ok",
        "--timeout",
        "10",
        timeout=20,
    )
    assert successful.returncode == 0
    assert json.loads(successful.stdout)["tasks"][0]["status"] == "completed"

    aggregate = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "wait",
        "--json",
        "wait-ok",
        "wait-failed",
        "--timeout",
        "10",
        timeout=20,
    )
    assert aggregate.returncode == 1
    assert {item["status"] for item in json.loads(aggregate.stdout)["tasks"]} == {
        "completed",
        "failed",
    }

    long_task = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "wait-timeout",
        "--detach",
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(20)",
    )
    assert long_task.returncode == 0, long_task.stderr

    timed_out = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "wait",
        "wait-timeout",
        "--timeout",
        "0.05",
    )
    assert timed_out.returncode == 1
    assert "timed out waiting for: wait-timeout" in timed_out.stderr
    assert _wait_status(
        tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "wait-timeout",
        {"queued", "running"},
    )["status"] in {"queued", "running"}

    stopped = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "stop",
        "wait-timeout",
        "--timeout",
        "10",
        timeout=15,
    )
    assert stopped.returncode == 0, stopped.stderr


def test_wait_sigint_returns_130_without_stopping_task(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    submitted = _run_cli(
        tmp_path,
        "exec",
        "--name",
        "interrupt-wait",
        "--detach",
        "--",
        sys.executable,
        "-c",
        "import time; time.sleep(20)",
    )
    assert submitted.returncode == 0, submitted.stderr
    task_dir = tmp_path / "_pyruns_" / "_shell_" / TASKS_DIR / "interrupt-wait"
    _wait_status(task_dir, {"queued", "running"})

    wait_args = ["-w", "shell", "wait", "interrupt-wait", "--timeout", "30"]
    code = (
        "import signal, threading\n"
        "threading.Timer(0.3, lambda: signal.raise_signal(signal.SIGINT)).start()\n"
        + "from pyruns.cli.app import main\n"
        + f"raise SystemExit(main({wait_args!r}))\n"
    )
    waiter = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_source_env(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    )
    try:
        _stdout, stderr = waiter.communicate(timeout=10)
        assert waiter.returncode == 130
        assert stderr == ""
        assert _wait_status(task_dir, {"queued", "running"})["status"] in {
            "queued",
            "running",
        }
    finally:
        if waiter.poll() is None:
            waiter.kill()
            waiter.communicate(timeout=5)
        _run_cli(
            tmp_path,
            "-w",
            "shell",
            "stop",
            "interrupt-wait",
            "--timeout",
            "10",
            timeout=15,
        )


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
        "-w",
        "shell",
        "stop",
        "--json",
        "cancel-me",
        "--timeout",
        "10",
    )
    assert cancelled.returncode == 0, cancelled.stdout + cancelled.stderr
    payload = json.loads(cancelled.stdout)
    assert payload["stopped"][0]["status"] == "cancelled"


@pytest.mark.parametrize(
    ("initial_status", "final_status", "return_code"),
    [("queued", "cancelled", 0), ("running", "failed", 1)],
)
def test_stop_reconciles_tasks_from_expired_foreign_runner(
    tmp_path,
    initial_status,
    final_status,
    return_code,
):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    task = TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        f"expired-{initial_status}", "echo stale\n"
    )

    def make_stale(info):
        info.update(
            {
                "status": initial_status,
                "runner_id": "other-host:123:expired",
                "runner_host": "other-host",
                "lease_heartbeat": time.time() - 120,
                "lease_until": time.time() - 60,
            }
        )
        if initial_status == "running":
            info.update(
                {
                    "run_index": 1,
                    "start_times": ["2026-03-20_00-00-01"],
                    "finish_times": [""],
                    "pids": [987654321],
                }
            )

    update_task_info(task["dir"], make_stale)

    stopped = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "stop",
        "--json",
        task["name"],
        "--timeout",
        "0.2",
    )

    assert stopped.returncode == return_code, stopped.stdout + stopped.stderr
    assert json.loads(stopped.stdout)["stopped"][0]["status"] == final_status
    info = load_task_info(task["dir"])
    assert info["status"] == final_status
    assert info["cancel_requested_at"]
    assert "runner_id" not in info
    assert "lease_until" not in info
    if initial_status == "running":
        assert info["finish_times"][0]

    removed = _run_cli(tmp_path, "-w", "shell", "rm", task["name"])
    assert removed.returncode == 0, removed.stdout + removed.stderr


def test_stop_only_requests_cancellation_from_live_foreign_runner(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    task = TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "live-foreign", "echo live\n"
    )
    update_task_info(
        task["dir"],
        lambda info: info.update(
            {
                "status": "queued",
                "runner_id": "other-host:123:live",
                "runner_host": "other-host",
                "lease_heartbeat": time.time(),
                "lease_until": time.time() + 60,
            }
        ),
    )

    stopped = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "stop",
        task["name"],
        "--timeout",
        "0.1",
    )

    assert stopped.returncode == 1
    assert "timed out waiting" in stopped.stderr
    info = load_task_info(task["dir"])
    assert info["status"] == "queued"
    assert info["runner_id"] == "other-host:123:live"
    assert info["cancel_requested_at"]


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
        tmp_path, "-w", "shell", "ls", "--trash", "--json"
    )
    assert json.loads(listing.stdout)["tasks"][0]["name"] == "recoverable"
    filtered = _run_cli(
        tmp_path,
        "-w",
        "shell",
        "ls",
        "does-not-match",
        "--trash",
        "--json",
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


def test_restore_rejects_tampered_task_name_outside_tasks_directory(tmp_path):
    workspace = Path(bootstrap_shell_workspace(str(tmp_path / "_pyruns_")))
    from pyruns.core.task_generator import TaskGenerator

    TaskGenerator(root_dir=str(workspace / TASKS_DIR)).create_shell_task(
        "recoverable", "echo ok\n"
    )
    assert _run_cli(tmp_path, "-w", "shell", "rm", "recoverable").returncode == 0
    trash_entry = workspace / TASKS_DIR / TRASH_DIR / "recoverable"
    info_path = trash_entry / TASK_INFO_FILENAME
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["name"] = "../escaped"
    info_path.write_text(json.dumps(info), encoding="utf-8")

    restored = _run_cli(tmp_path, "-w", "shell", "restore", "recoverable")

    assert restored.returncode == 1
    assert "cannot restore" in restored.stderr
    assert trash_entry.is_dir()
    assert not (workspace / "escaped").exists()


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
        tmp_path, "-w", "shell", "pin", "after", "--json"
    )
    assert json.loads(pinned.stdout)["pinned"] is True
    unpinned = _run_cli(
        tmp_path, "-w", "shell", "pin", "after", "--off", "--json"
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
    json_rows = json.loads(output.read_text(encoding="utf-8"))
    assert json_rows[0]["name"] == "exportable"
    assert json_rows[0]["run"] == 1

    json_stdout = _run_cli(
        tmp_path, "-w", "shell", "export", "--format", "json"
    )
    assert json_stdout.returncode == 0, json_stdout.stderr
    assert json.loads(json_stdout.stdout)[0]["name"] == "exportable"


def test_atomic_export_preserves_existing_file_when_replace_fails(tmp_path, monkeypatch):
    from pyruns.cli import commands

    output = tmp_path / "report.json"
    output.write_text("old report\n", encoding="utf-8")

    def fail_replace(_source, _destination):
        raise OSError("replace blocked")

    monkeypatch.setattr(commands.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace blocked"):
        commands._atomic_write_export(str(output), "new report\n")

    assert output.read_text(encoding="utf-8") == "old report\n"
    assert list(tmp_path.iterdir()) == [output]


def test_config_get_set_unset_and_path(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    path = _run_cli(tmp_path, "config", "path")
    assert path.returncode == 0
    assert Path(path.stdout.strip()).is_file()
    json_path = _run_cli(tmp_path, "config", "path", "--json")
    assert Path(json.loads(json_path.stdout)["path"]).is_file()

    set_result = _run_cli(
        tmp_path,
        "config",
        "set",
        "monitor_scrollback",
        "200000",
    )
    assert set_result.returncode == 0
    get_result = _run_cli(
        tmp_path,
        "config",
        "get",
        "monitor_scrollback",
    )
    assert get_result.stdout.strip() == "200000"
    unset_result = _run_cli(
        tmp_path,
        "config",
        "unset",
        "monitor_scrollback",
    )
    assert unset_result.returncode == 0
    assert unset_result.stdout.strip() != "200000"
    settings_data = yaml.safe_load(
        (tmp_path / "_pyruns_" / "_pyruns_settings.yaml").read_text(encoding="utf-8")
    )
    assert "monitor_scrollback" not in settings_data

    listed = _run_cli(tmp_path, "config", "list", "--json")
    values = json.loads(listed.stdout)
    assert values["schema_version"] == 1
    for removed_key in (
        "generator_form_columns",
        "generator_auto_timestamp",
        "generator_mode",
        "manager_columns",
        "manager_max_workers",
        "manager_execution_mode",
        "ui_page_size",
        "pinned_params",
    ):
        assert removed_key not in values

    structured_set = _run_cli(
        tmp_path,
        "config",
        "set",
        "global_env",
        "{FOO: bar}",
    )
    assert structured_set.returncode == 0, structured_set.stderr
    assert structured_set.stdout.strip() == "FOO: bar"
    structured_get = _run_cli(tmp_path, "config", "get", "global_env")
    assert structured_get.returncode == 0, structured_get.stderr
    assert structured_get.stdout.strip() == "FOO: bar"

    enabled_logs = _run_cli(tmp_path, "config", "set", "log_enabled", "true")
    assert enabled_logs.returncode == 0, enabled_logs.stderr
    logged_json = _run_cli(tmp_path, "-w", "shell", "ls", "--json")
    assert logged_json.returncode == 0, logged_json.stderr
    assert json.loads(logged_json.stdout)["schema_version"] == 1
    assert "TaskManager initialised" in logged_json.stderr


def test_config_set_does_not_resolve_environment_interpolations(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    result = _run_cli(
        tmp_path,
        "config",
        "set",
        "global_env",
        "{TOKEN: '${oc.env:PYRUNS_TEST_SECRET}'}",
        env_overrides={"PYRUNS_TEST_SECRET": "must-not-be-persisted"},
    )

    assert result.returncode == 0, result.stderr
    settings_text = (
        tmp_path / "_pyruns_" / "_pyruns_settings.yaml"
    ).read_text(encoding="utf-8")
    assert "must-not-be-persisted" not in settings_text
    assert "${oc.env:PYRUNS_TEST_SECRET}" in settings_text


def test_config_set_rejects_multiline_value_with_extra_yaml_keys(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    result = _run_cli(
        tmp_path,
        "config",
        "set",
        "conda_env",
        "foo\nother: x",
    )

    assert result.returncode == 2
    assert "single YAML value" in result.stderr


def test_config_rejects_unknown_keys_and_wrong_types(tmp_path):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))
    unknown = _run_cli(tmp_path, "config", "get", "manager_max_workers")
    wrong_type = _run_cli(
        tmp_path,
        "config",
        "set",
        "monitor_scrollback",
        "text",
    )
    assert unknown.returncode == 1
    assert wrong_type.returncode == 2


@pytest.mark.parametrize(
    "value",
    ["{'BAD=KEY': value}", '{GOOD: "bad\\0value"}'],
)
def test_config_rejects_invalid_global_environment(tmp_path, value):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    result = _run_cli(tmp_path, "config", "set", "global_env", value)

    assert result.returncode == 2
    assert "environment" in result.stderr.lower()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("monitor_line_height", ".nan"),
        ("monitor_line_height", ".inf"),
        ("global_env", "{SAFE: value, BROKEN: .nan}"),
        ("gpu_scheduler_device_ids", "[0, .inf]"),
    ],
)
def test_config_rejects_non_finite_numbers(tmp_path, key, value):
    bootstrap_shell_workspace(str(tmp_path / "_pyruns_"))

    result = _run_cli(tmp_path, "config", "set", key, value)

    assert result.returncode == 2
    assert "finite" in result.stderr.lower()


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("ui_port", "70000"),
        ("monitor_chunk_size", "-3"),
        ("monitor_chunk_size", str(4 * 1024 * 1024 + 1)),
        ("monitor_scrollback", "1000001"),
        ("monitor_line_height", "2.5001"),
        ("shell_mode", "nonsense"),
        ("gpu_scheduler_memory_used_pct", "101"),
        ("gpu_scheduler_stable_seconds", "0"),
        ("gpu_scheduler_max_wait_seconds", "0"),
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


def test_directory_context_uses_target_project_logging_settings(tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    bootstrap_shell_workspace(str(source / "_pyruns_"))
    bootstrap_shell_workspace(str(target / "_pyruns_"))
    (source / "_pyruns_" / "_pyruns_settings.yaml").write_text(
        "log_enabled: true\nlog_level: INFO\n",
        encoding="utf-8",
    )
    (target / "_pyruns_" / "_pyruns_settings.yaml").write_text(
        "log_enabled: false\nlog_level: INFO\n",
        encoding="utf-8",
    )

    result = _run_cli(source, "-C", str(target), "-w", "shell", "ls", "--json")

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["schema_version"] == 1
    assert "TaskManager initialised" not in result.stderr


def test_metrics_does_not_require_workspace(tmp_path):
    result = _run_cli(tmp_path, "metrics", "--json")
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
