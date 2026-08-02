# pyruns

![logo](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/pyruns_logo2.png)

English | [简体中文](README.md)

[![PyPI version](https://img.shields.io/pypi/v/pyruns.svg)](https://pypi.org/project/pyruns/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyruns.svg)](https://pypi.org/project/pyruns/)
[![License](https://img.shields.io/pypi/l/pyruns.svg)](https://github.com/LthreeC/pyruns/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2563eb.svg)](https://lthreec.github.io/pyruns/)

Pyruns is a disk-first runner for reproducible experiments and terminal workloads. It stores commands, configurations, logs, environments, run history, and metrics under the project's `_pyruns_` directory, then exposes them through a Git-style one-shot CLI and an optional Web UI.

![Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

## Start in 30 seconds

```bash
pip install pyruns

# Both official entrypoints are identical; examples below use short `pyr`
pyr --help
pyruns --help
pyr help -a  # list every command

# Initialize the current project's shell workspace
pyr init

# Run and track one command
pyr exec -n smoke -- python -V

# Inspect the result
pyr -w shell ls
pyr -w shell show smoke
pyr -w shell log smoke
```

Detach long-running work, then control it with separate commands:

```bash
pyr exec -n train -d -- python train.py --epochs 100
pyr -w shell status
pyr -w shell wait train
pyr -w shell log train
```

Start the Web UI explicitly when needed:

```bash
pyr ui
pyr ui train.py
pyr ui --shell
```

`pyr` and `pyruns` are identical official entrypoints: the former is faster to type, while the latter makes the project name explicit. Bare commands print concise help and `help -a` expands the full command index; neither starts a stateful interactive REPL.

## Why it is useful

- Every task has a stable directory instead of disappearing into terminal scrollback.
- Commands, arguments, environment variables, logs, metrics, and artifacts stay together.
- Shell commands and Python configuration experiments share one lifecycle.
- One command performs one operation, making the same interface useful to people, scripts, CI, and AI agents.
- Foreground execution returns the real task result; a batch returns non-zero if any task fails.
- Detached runners outlive the invoking terminal and do not open extra console windows on Windows.
- The CLI and Web UI share the same disk state.

## Git-style CLI

```text
pyr [GLOBAL OPTIONS] COMMAND [COMMAND OPTIONS]
```

Global options:

```text
-C, --directory PATH
-w, --workspace NAME|PATH|SCRIPT.py
--json
--no-color
--debug
--version
```

Commands:

| Command | Purpose |
| --- | --- |
| `init` | initialize a shell or Python script workspace |
| `exec` | create and run one tracked terminal command or shell script |
| `add` | add immutable task snapshots from YAML |
| `run` | run one or more exact task names |
| `ls` | filter and sort tasks deterministically |
| `status` | summarize one workspace |
| `show` | show task metadata and paths |
| `log` | print, follow, or locate logs |
| `wait` | wait for already active tasks |
| `stop` | ask the owning runner to stop tasks and mark them `cancelled` |
| `rm` / `restore` | soft-delete and restore tasks |
| `mv` / `pin` | manage task names and pinned state |
| `export` | export CSV or JSON records |
| `config` | inspect and update project settings |
| `metrics` | print one CPU, memory, and GPU snapshot |
| `ui` / `dev` | start the Web UI explicitly |
| `help` | show top-level or command help |

See the [complete CLI guide](docs/cli-guide.md) for every option and contract.

## Two workspace types

### Shell Workspace

Use it for arbitrary terminal commands, repository reproduction, installation, preprocessing, training, evaluation, and pipelines:

```bash
pyr init
pyr exec -n env-check -- python -V
pyr exec -n install -- python -m pip install -r requirements.txt
pyr exec -n baseline -d -- python train.py --config baseline.yaml
```

Pass shell script files directly to `exec`; there is no need to spell out the interpreter:

```bash
pyr exec -n setup -- ./scripts/setup.sh
pyr exec -n setup-ps -- .\scripts\setup.ps1
pyr exec -n setup-cmd -- .\scripts\setup.cmd
pyr exec -n setup-bat -- .\scripts\setup.bat
```

Pyruns maps `.sh`, `.ps1`, `.cmd`, and `.bat` to Bash/sh, PowerShell, or `cmd.exe`. Values after the file path are arguments for that script, not Pyruns options. Pyruns preserves those argument boundaries and records the script content hash for every run. Reruns still use the original script path, so relative-path behavior based on the script directory remains intact.

Use `--shell` only when the command intentionally relies on pipes, redirects, expansion, or command chaining:

```bash
pyr exec -n report --shell "python eval.py > metrics.txt"
```

For ordinary programs, prefer the exact argument vector after `--`.

Preview an execution with no filesystem or process side effects; add global `--json` for a stable plan object:

```bash
pyr exec --dry-run -n report -- python eval.py
pyr --json exec --dry-run -n report -- python eval.py
```

The preview does not create `_pyruns_`, tasks, or settings files, and it does not start the user command.

```text
<project>/_pyruns_/_shell_/tasks/<task>/
├── task_info.json
├── config.ps1 | config.cmd | config.sh
└── run_logs/runN.log
```

### Script Workspace

Use it for `argparse`, `pyruns.load()`, YAML configuration, batch expansion, and parameterized experiments:

```bash
pyr init train.py
pyr -w train add configs/quick.yaml
pyr -w train run quick
pyr -w train run --from configs/sweep.yaml -n sweep -j 4
pyr -w train run --from configs/sweep.yaml -n sweep -j 4 --dry-run
```

`run --from ... --dry-run` validates the YAML and lists expanded task candidates without creating or running them.

```text
<project>/_pyruns_/train/tasks/<task>/
├── task_info.json
├── config.yaml
├── run_logs/runN.log
└── artifacts/runN/
```

## Workspace selection

Pyruns walks from the current directory toward its parents to find the nearest `_pyruns_`. It selects the workspace automatically only when exactly one exists. Multiple workspaces require `-w`:

```bash
pyr -w shell ls
pyr -w train status
pyr -w ./train.py show baseline
pyr -w ./_pyruns_/train log baseline
```

Task names must be exact. Indices and fuzzy target resolution are not supported. `show` and `log` additionally accept `TASK@RUN` for a historical run, so `@` is reserved in new task names.

## Python integration

### Zero-intrusion `argparse`

```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--lr", type=float, default=1e-3)
parser.add_argument("--epochs", type=int, default=10)
args = parser.parse_args()
```

```bash
pyr init train.py
pyr ui train.py
```

### `pyruns.load()` configuration

```python
import os

import pyruns

cfg = pyruns.load()
print(cfg.training.lr)
```

```bash
pyr init train.py --config configs/default.yaml
pyr -w train add configs/default.yaml -n baseline
pyr -w train run baseline
```

## In-script API

| API | Purpose |
| --- | --- |
| `pyruns.load()` | load the current task configuration as a dot-accessible object |
| `pyruns.read(path=None)` | explicitly read YAML or JSON |
| `pyruns.record(**kwargs)` | save final metrics for the current run |
| `pyruns.track(**kwargs)` | append time-series metrics |
| `pyruns.get_task_dir()` | return the current task directory |
| `pyruns.get_run_index()` | return the current run number |
| `pyruns.artifact_dir()` | create and return `artifacts/runN` |

```python
import pyruns

cfg = pyruns.load()

for epoch in range(cfg.training.epochs):
    loss = train_one_epoch()
    pyruns.track(epoch=epoch, loss=loss)

pyruns.record(final_loss=loss, seed=cfg.training.seed)
model.save(os.path.join(pyruns.artifact_dir(), "model.pt"))
```

## Inspection and lifecycle

```bash
pyr -w train ls -s running --status queued
pyr -w train status
pyr -w train show baseline
pyr -w train show baseline@2
pyr -w train log baseline -f
pyr -w train log baseline@2
pyr -w train wait baseline --timeout 600
pyr -w train stop baseline
pyr -w train mv baseline baseline-lr1e3
pyr -w train pin baseline-lr1e3
pyr -w train rm baseline-lr1e3
pyr -w train ls --trash
pyr -w train restore baseline-lr1e3
```

`rm` executes immediately without confirmation, but remains a recoverable soft delete.

## JSON and automation

Place global `--json` before the command:

```bash
pyr --json -w shell ls
pyr --json -w shell status
pyr --json -w shell show smoke
pyr --json -w shell show smoke@2
pyr --json -w shell log smoke --path
pyr --json -w shell log smoke@2 --path
pyr --json -w shell config list
pyr --json metrics
```

Logs are raw stdout by default; use `log --path` for a structured reference. Exports go to stdout unless a file is selected:

```bash
pyr -w train export -f csv
pyr -w train export baseline -f json
pyr -w train export -s completed -o results.csv
```

Exit status:

```text
0    command and all waited tasks succeeded
1    workspace, target, runtime, or task failure
2    invalid command-line usage
130  interrupted while waiting or following logs
```

## Web UI

![Manager](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

```bash
pyr ui
pyr ui train.py
pyr ui train.py --config configs/default.yaml
pyr ui --shell
pyr ui --shell --no-browser
pyr dev train.py
```

- Generator edits script configurations or shell payloads and creates tasks.
- Manager searches, filters, runs, cancels, renames, pins, and removes tasks.
- Monitor shows logs, metrics, and task details.
- Dashboard presents project status and resource summaries.

## Disk is the source of truth

```text
<project>/_pyruns_/
├── _pyruns_settings.yaml
├── _shell_/
│   ├── script_info.json
│   └── tasks/
└── <script_name>/
    ├── script_info.json
    ├── config_default.yaml
    └── tasks/
```

The CLI and Web UI operate on the same state, so tasks survive closing either interface and remain inspectable by version control, backup tools, and automation.

## Documentation

- [Getting started](docs/getting-started.md)
- [CLI guide](docs/cli-guide.md)
- [Configuration](docs/configuration.md)
- [Web UI guide](docs/ui-guide.md)
- [Batch syntax](docs/batch-syntax.md)
- [Script API](docs/api-reference.md)
- [Architecture](docs/architecture.md)

## License

MIT
