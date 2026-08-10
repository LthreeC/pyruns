---
name: pyruns-cli
description: Use the equivalent Git-style `pyr` and `pyruns` CLIs for tracked shell commands and Python experiment workspaces. Use one-shot commands for execution, inspection, lifecycle control, logs, configuration, metrics, exports, and explicit Web UI startup. There is no interactive terminal mode.
---

# Pyruns CLI

## Command Model

Use either official executable: short `pyr` or explicit `pyruns`. They expose the same commands, options, output contracts, and exit codes. Prefer `pyr` for brevity in repeated automation; use `pyruns` when the project name should be obvious. Every invocation performs one operation and exits.

```bash
pyr --help
pyruns --help
pyr help -a
pyr COMMAND [OPTIONS]
pyruns COMMAND [OPTIONS]
pyr -w WORKSPACE COMMAND [OPTIONS]
```

Examples in this card use `pyr`, but replacing it with `pyruns` changes nothing. Use only the formal commands below; there is no interactive REPL:

```text
init exec add run ls status show log wait stop
rm restore mv pin export config metrics ui dev help
```

Project context options must precede the command:

```text
-C, --directory PATH
-w, --workspace NAME|PATH|SCRIPT.py
--debug
--version
```

Machine-readable commands expose their own `--json` option. Put it after the command; there is no global leading form.

Use exact task names. Do not pass task indices or fuzzy names. For `show` and `log`, use `--run RUN` or append the shorter `@RUN` to select one positive historical run number; `@` is reserved and cannot appear in a new task name.

## Choose A Workspace

Use the shell workspace for arbitrary commands, repository reproduction, smoke tests, installs, preprocessing, training, evaluation, and other terminal workflows.

Use a script workspace when a Python entry point needs `argparse`, `pyruns.load()`, YAML snapshots, batch expansion, or per-task `config.yaml`.

Pyruns searches the current directory and its parents for `_pyruns_`. If it finds exactly one workspace, commands can omit `-w`. If it finds more than one, select one explicitly:

```bash
pyr -w shell ls
pyr -w train status
pyr -w path/to/train.py show baseline
pyr -w path/to/_pyruns_/train log baseline
```

Use `-C PATH` to run as if invoked from another directory:

```bash
pyr -C path/to/project -w shell status
```

## Track A Terminal Command

`exec` initializes the shell workspace automatically when needed. `--` ends Pyruns option parsing and passes every following item as an exact argument vector:

```bash
pyr exec -n env-check -- python -V
pyr exec -n smoke -- python train.py --epochs 1
```

This quoting-safe form is the default. `--` is a separator, not a shell-mode switch: pipes, redirects, variables, globs, and command chaining are not interpreted. Pyruns stores the argument vector as structured task metadata and reuses the creation working directory on every rerun. Changing the configured shell later does not reinterpret an argv task.

Pass an existing shell script file as the first exact argument to select its interpreter automatically:

```bash
pyr exec -n setup -- ./scripts/setup.sh
pyr exec -n setup-ps -- .\scripts\setup.ps1
pyr exec -n setup-cmd -- .\scripts\setup.cmd
pyr exec -n setup-bat -- .\scripts\setup.bat
```

`.sh` uses an available Bash/sh without requiring an executable bit; `.ps1` uses non-interactive PowerShell; `.cmd` and `.bat` use `cmd.exe` on Windows. Any values after the file path are arguments for that script, not Pyruns options. Paths and script arguments remain separate. This is the tracked replacement for direct `bash xxx.sh` execution: Pyruns keeps logs, elapsed time, the raw exit code, Git/source state, and the original absolute script path for reruns.

Use `-c` / `--command` only when the command intentionally depends on shell syntax such as pipes, redirects, variable expansion, or command chaining:

```bash
pyr exec -n report -c "python eval.py > metrics.txt"
```

`-c` follows the familiar `sh -c` convention and accepts exactly one quoted command string. Pyruns stores the resolved shell executable and creation working directory, then uses both for reruns. Use exact argv after `--` whenever shell syntax is not required.

Set a few task-local environment variables with one `-e` or `--env` followed by multiple `KEY=VALUE` entries. `--` marks the target-command boundary. Repeating the option remains supported:

```bash
pyr exec -n train -e CUDA_VISIBLE_DEVICES=0 TOKENIZERS_PARALLELISM=false SEED=42 -- python train.py
```

Pyruns already sets `PYTHONUNBUFFERED=1`, `PYTHONIOENCODING=utf-8`, and `PYTHONUTF8=1` for child processes. For larger sets, load one or more UTF-8 files containing blank lines, whole-line `#` comments, and `KEY=VALUE` entries:

```bash
pyr exec -n train --env-file .env.train -e SEED=42 -- python train.py
```

Later env files override earlier files; command-line `-e` values override every file. Env files do not evaluate shell syntax or interpolation. Task-local environment values are persisted in `task_info.json` and shown by `show`; do not put secrets there.

Environment variables set on the invoking shell are inherited for that invocation but are not persisted with the task. Use `-e` or `--env-file` when later CLI or Web UI reruns must receive the same value.

Foreground execution follows plain log output and returns the task result. Add `-d` or `--detach` only when the caller should return after the hidden runner accepts the task:

```bash
pyr exec -n train -d -- python train.py --epochs 100
pyr -w shell wait train
```

Ctrl+C during foreground `exec` or `run` requests cancellation of tasks submitted by that invocation. Ctrl+C during `wait` or `log -f`, and a `wait` timeout, stop observing only; the tasks continue. Use `stop` to cancel existing work.

On Windows, detached runners and their managed task processes do not open new console windows.

Preview `exec` without creating a workspace or task and without starting the user command:

```bash
pyr exec --dry-run -n smoke -- python -V
pyr exec --dry-run -n smoke --json -- python -V
```

The plan reports workspace creation, task-name availability, working directory, command mode and argv or shell expression, runtime, and environment. Treat `planned_name: null` as a signal that automatic naming needs a unique suffix. An explicit duplicate `--name` remains an error. Preview and detached execution are different operations, so `--dry-run` and `--detach` are rejected together.

## Use A Python Script Workspace

Initialize a script workspace without starting the Web UI:

```bash
pyr init train.py
pyr init train.py --config configs/default.yaml
```

Add immutable task snapshots from YAML:

```bash
pyr -w train add configs/quick.yaml
pyr -w train add configs/sweep.yaml -n ablation
```

Run existing tasks by exact name:

```bash
pyr -w train run quick
pyr -w train run ablation_a ablation_b -j 2
```

Create and run in one operation:

```bash
pyr -w train run --config configs/quick.yaml -n quick
pyr -w train run --config configs/quick.yaml -n quick --dry-run
pyr -w train run --config configs/quick.yaml -n quick --dry-run --json
```

`run --config ... --dry-run` reads and validates the YAML, reports expanded task candidates, and creates nothing. Dry-run is intentionally rejected for rerunning existing tasks and cannot be combined with `--detach`. Normal `run` waits for all requested tasks by default. It exits `1` if any task fails. `--detach` changes only waiting behavior. `-j/--jobs` is capped at the number of selected tasks. If a runner accepts only part of a batch, Pyruns reports exact `claimed` and `unclaimed` names and exits `1`.

## Inspect And Control Tasks

Use these one-shot commands after either shell or script tasks exist:

```bash
pyr -w shell ls
pyr -w shell ls -s running -s queued
pyr -w shell status
pyr -w shell show train
pyr -w shell show train@2
pyr -w shell show train --run 2
pyr -w shell log train
pyr -w shell log train -f
pyr -w shell log train@2
pyr -w shell log train --run 2
pyr -w shell log train --path
pyr -w shell wait train --timeout 600
pyr -w shell stop train
pyr -w shell mv train train-lr1e3
pyr -w shell pin train-lr1e3
pyr -w shell pin train-lr1e3 --off
pyr -w shell ls --sort name --reverse
pyr -w shell rm train-lr1e3
pyr -w shell ls --trash
pyr -w shell restore train-lr1e3
```

`rm` is immediate and non-interactive, but recoverable: it moves tasks into workspace trash. There is no confirmation prompt and no `--yes` option.

`stop` asks the owning runner to terminate active work. A successful stop ends in `cancelled`, distinct from an execution failure, and the task remains rerunnable. `stop --timeout 0` waits indefinitely.

Pinned tasks show a `PIN` marker in human listings and a `pinned` boolean in JSON. They remain first for every active-task sort; `--reverse` changes order inside the pinned and unpinned groups without moving pinned tasks below normal tasks.

`log -f` streams bytes to stdout until the task finishes. It is not a prompt, REPL, full-screen viewer, or other interactive terminal mode. Ctrl+C stops following without stopping the task.

`show` and `log` both accept `TASK@RUN` and `TASK --run RUN`. Do not combine `TASK@RUN` with `--run`, and do not follow a historical log. A selected `show` run includes its start time, finish time, duration, raw exit code, PID, source state, record, track, and log path.

## Machine-Readable Operations

Put `--json` after commands that explicitly support it:

```bash
pyr -w shell ls --json
pyr -w shell status --json
pyr -w shell show train --json
pyr -w shell show train@2 --json
pyr -w shell log train --path --json
pyr -w shell log train@2 --path --json
pyr config list --json
pyr metrics --json
```

`log` prints the raw log by default and intentionally does not wrap it in JSON. Use `log --path` with `--json` when an automation client needs a machine-readable log reference. A task without an existing log is an operation error; Pyruns never returns a fabricated future path.

Strict JSON preserves YAML dates and timestamps as ISO 8601 strings. Unsupported objects, NaN, and Infinity remain errors instead of being stringified silently.

Export records to stdout by default:

```bash
pyr -w train export -f csv
pyr -w train export baseline --format json
pyr -w train export -s completed -o report.csv
```

CSV and JSON both emit one record per task run. Select the record format with `--format` (or `-f`); output filename extensions do not infer it. Runs without monitor metrics are still included with their lifecycle fields.

## Configuration And Metrics

```bash
pyr config list
pyr config get monitor_scrollback
pyr config set monitor_scrollback 200000
pyr config unset monitor_scrollback
pyr config path
pyr metrics
```

`config` edits project-level settings shared by the project's workspaces, so it does not require `-w`. Values passed to `config set` are parsed as YAML scalars, lists, or mappings and validated against known settings.

Concurrency is a per-run choice, not a project setting. Select it explicitly with `run -j/--jobs`. Pyruns coordinates tasks with threads while each tracked command still runs in its own child process.

## Start The Web UI Explicitly

The Web UI never starts from a bare `pyr` or `pyruns` call. Use:

```bash
pyr ui
pyr ui train.py
pyr ui train.py --config configs/default.yaml
pyr ui shell
pyr dev train.py
```

Pass an exact existing workspace name/path, `shell`, or a Python script path directly after `ui`. `ui` and `dev` do not accept `-w` or `--json`. Use `--no-browser` for headless servers.

The UI listens on loopback and prints a fresh tokenized URL on every start. The first browser request exchanges that token for an HttpOnly, SameSite session cookie and redirects to a clean URL. With `--no-browser`, open the complete printed URL and do not share it; this is a local UI, not a remote multi-user service.

## Disk Layout

```text
<project>/_pyruns_/
|-- _pyruns_settings.yaml
|-- _shell_/
|   |-- script_info.json
|   `-- tasks/<task>/
|       |-- task_info.json
|       |-- config.ps1 | config.cmd | config.sh
|       `-- run_logs/runN.log
`-- <script>/
    |-- script_info.json
    |-- config_default.yaml
    `-- tasks/<task>/
        |-- task_info.json
        |-- config.yaml
        `-- run_logs/runN.log
```

## Exit Status

```text
0    command and requested tasks succeeded
1    workspace, target, runtime, or task failure
2    invalid command-line usage
130  command interrupted by Ctrl+C
```

## Report Back

Report the exact `pyr` or `pyruns` command used, selected workspace, exact task name, final status, payload path, and latest log path. For long work, submit with `-d`, then use `wait`, `show`, and `log`; never drive a stateful prompt.

## Skill Limits

This skill is an instruction card only. Use the local Pyruns CLI and normal shell/Python tools for execution and validation.
