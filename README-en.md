# pyruns

![logo](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/pyruns_logo2.png)

[简体中文](README.md) | **English**

[![PyPI version](https://img.shields.io/pypi/v/pyruns.svg)](https://pypi.org/project/pyruns/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyruns.svg)](https://pypi.org/project/pyruns/)
[![License](https://img.shields.io/pypi/l/pyruns.svg)](https://github.com/LthreeC/pyruns/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2563eb.svg)](https://lthreec.github.io/pyruns/)

> A local Web UI for Python experiment work: visual parameter editing, batch task generation, task orchestration, real-time terminal logs, and CSV metric export.  
> Everything runs locally, everything is disk-backed, and everything stays close to the scripts and terminals you already use.

![Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

Pyruns is designed around a simple idea: experiment tooling should feel closer to your real workflow, not further away from it.

It tries to stay aligned with what you already have:

- your original scripts
- your current terminal / shell
- your conda environment and environment variables
- a disk-backed workspace under `_pyruns_`

**No accounts · No cloud dependency · No database required · Fully local execution**

```bash
pip install pyruns
pyr train.py
pyr train.py my_config.yaml
pyr train.py -p 9000
pyruns train.py -p 9000
pyruns train.py -p 9000 --no-browser
pyr
```

In tmux, SSH, or headless sessions, Pyruns prints the local URL and skips browser auto-open by default. Use `--browser` or `PYRUNS_OPEN_BROWSER=1` when you explicitly want to force browser opening.

The recommended first-glance entrypoints are:

- `pyr train.py`
- `pyr train.py my_config.yaml`

These are the primary paths new users should see first.  
`pyr` is still available, but it works better as a secondary entry for shell and command-task workflows.

The newer Home view also gives the workspace a clearer entry surface: system status, task overview, and GPU usage are visible before you dive into generation, orchestration, or logs.

![Home](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_home.png)

## Why it feels useful

Many experiment tools are powerful, but everyday friction still comes from the same places.

### Pain point 1: hyperparameter search still turns into hand-written loops

Without Pyruns, you often end up writing nested shell loops like this:

```bash
for lr in 0.001 0.01 0.1; do
  for bs in 32 64 128; do
    for opt in adam sgd; do
      python train.py --lr $lr --batch_size $bs --optimizer $opt \
        > logs/lr${lr}_bs${bs}_${opt}.log 2>&1 &
    done
  done
done
wait
```

With Pyruns, the same intent can live directly in `Form` mode:

```yaml
lr: 0.001 | 0.01 | 0.1
batch_size: 32 | 64 | 128
optimizer: adam | sgd
```

That expands into isolated tasks with their own configs, logs, and histories.

### Pain point 2: experiment history lives in your memory instead of your workspace

“Which `batch_size` did I use for that `lr=0.01` run last week?”  
“Where did that shell task log go?”  
“What environment did it use?”

Pyruns keeps that history close:

- `config.yaml` or `config.sh`
- `task_info.json`
- `run_logs/runN.log`
- timestamps, PIDs, notes, and environment details

The result is simple: tasks do not disappear after they finish. They leave behind a usable history.

### Pain point 3: concurrent logs become unreadable

When several tasks run at once, mixed stdout is the fastest way to lose context. Pyruns isolates task output into separate log files and gives it a dedicated terminal-like surface in `Monitor`.

![Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_monitor.png)

For shell-driven workflows, this matters even more:

![Shell Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_monitor.png)

## Core capabilities

| Capability | What it gives you |
| --- | --- |
| `React Generator` | `Form`, `YAML`, and `Shell` entry modes for Python tasks and shell tasks. |
| `Form batch expansion` | Batch syntax with `|`, `(|)`, and `start:stop:step` for cartesian, zip, and range expansion. |
| `YAML single-task mode` | `YAML` mode stays focused on creating one clean `config.yaml` task at a time. |
| `Shell Workspace` | Shell tasks are stored as `config.sh` and follow the terminal that launched `pyr` by default. |
| `Manager control room` | Search, filter, batch run, batch delete, pin, inspect details, and jump into logs. |
| `Monitor terminal` | Real-time xterm.js view, historical log switching, clipboard support, and CSV export. |
| `Metric export` | Record metrics with `pyruns.record()` and export selected task results as CSV. |
| `Disk-backed workspace` | Task state lives on disk, making refresh, recovery, CLI/Web sharing, and backup much easier. |

## Two workspace modes

The shortest way to distinguish them is:

- `script` mode builds a workspace around a Python script
- `shell` mode builds a workspace around command tasks in a directory

| Mode | Entry | What you select | Task file | Best for |
| --- | --- | --- | --- | --- |
| `script` | `pyr train.py` / `pyr train.py config.yaml` | a Python script | `config.yaml` | `argparse`, `pyruns.load()`, config-driven experiments |
| `shell` | `pyr` | the current directory | `config.sh` | PowerShell / cmd / bash command tasks and terminal workflows |

The key difference is not visual. It is what Pyruns is actually managing:

- `script` mode manages “a script plus Python tasks”
- `shell` mode manages “a directory plus command tasks”

### 1. Script Workspace

When you open a normal Python script, Pyruns builds a dedicated workspace around it:

```text
project/
├─ train.py
└─ _pyruns_/
   ├─ _pyruns_settings.yaml
   └─ train/
      ├─ script_info.json
      ├─ config_default.yaml
      └─ tasks/
```

This is the right fit for:

- `argparse` scripts
- `pyruns.load()` / `pyruns.read()` workflows
- Python tasks that should keep a separate `config.yaml` snapshot per run
- workflows where parameter editing, templates, and batch generation come first

### 2. Shell Workspace

When you switch into shell mode, Pyruns is no longer centered on a `.py` file.  
It becomes centered on a directory.

When you switch into shell mode, the workspace becomes:

```text
project/
└─ _pyruns_/
   └─ _shell_/
      ├─ script_info.json
      └─ tasks/
```

Each shell task is persisted as:

```text
_pyruns_/_shell_/tasks/<task_name>/config.sh
```

The key idea is not “bash compatibility”. It is this:

- default `shell_mode: follow`
- follow the terminal that launched `pyr`
- inherit the current Python process environment
- do not auto-translate syntax across shells

So shell mode is better understood as:

- turning terminal history into managed tasks
- grouping command workflows from one directory into Manager / Monitor
- preserving the feel of “run this in the terminal I already use”

So the target behavior of a shell task is:

> as close as possible to manually running the same command in the terminal that started `pyr`

![Shell Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_generator.png)

## How to connect Pyruns

### Mode 1: zero-intrusion `argparse` integration

If your script already uses `argparse`:

```bash
pyr train.py
```

Pyruns will try to extract parameter definitions, build an editable form, and send modified values back as command-line arguments.

### Mode 2: YAML template driven workflow

If your script reads config with `pyruns.load()`:

```bash
pyr train.py my_config.yaml
```

On the first run, Pyruns stores that file as `config_default.yaml`. After that:

```bash
pyr train.py
```

the same workspace can continue handling generation, scheduling, and execution.

The UI launcher follows the same rule. `argparse` scripts can open directly.
For `pyruns.load()` scripts, the first launch needs a YAML file when
`config_default.yaml` does not exist yet. After a default template exists, Pyruns
reuses it directly; for `argparse` scripts, the default template is refreshed
from the current script arguments.

## In-script API

The API exposed to your training script is intentionally small:

| API | Purpose |
| --- | --- |
| `pyruns.load()` | Load the current task YAML / JSON config and return a dot-accessible config object. |
| `pyruns.read(path=None)` | Explicitly read a config file; most scripts can just call `pyruns.load()`. |
| `pyruns.record(**kwargs)` | Store final metrics for this run, such as `final_loss`, `acc`, or `seed`. Values from the same run are merged into one records slot. |
| `pyruns.track(**kwargs)` | Append time-series metrics, such as per-epoch `loss` or `acc`, into tracks. |
| `pyruns.get_task_dir()` | Return the current task directory, or `None` outside a Pyruns task. |
| `pyruns.get_run_index()` | Return the current run slot, useful when one task is run multiple times. |
| `pyruns.artifact_dir()` | Return the current run output directory, `artifacts/runN`, and create it automatically. |

Typical usage:

```python
import os
import pyruns

cfg = pyruns.load()

for epoch in range(cfg.training.epochs):
    loss = train_one_epoch(cfg)
    pyruns.track(loss=loss)

pyruns.record(final_loss=loss, seed=cfg.training.seed)

artifact_dir = pyruns.artifact_dir()
metrics_path = os.path.join(artifact_dir, "metrics.json")
with open(metrics_path, "w", encoding="utf-8") as f:
    f.write("{}")
```

### Mode 3: CLI interactive mode

If you work on a headless server or simply prefer a terminal workflow:

```bash
pyr cli train.py
```

The CLI and the Web UI share the same workspace and task data on disk.

### Mode 4: open a shell workspace directly

If you do not want to start from a Python entry script and only want to manage command tasks in the current directory:

```bash
pyr
```

This creates and opens:

```text
<current_dir>/_pyruns_/_shell_
```

This is especially useful for:

- command-driven experiments
- PowerShell / cmd / bash task collections
- workflows that begin as shell commands before becoming more structured scripts

## Practical examples

The repository already includes runnable examples under `examples/`.

### Example 1: native `argparse` support

Directory:

```text
examples/1_argparse_script/
```

This path is perfect for showcasing:

- the `Generator` form page
- the expanded task grid in `Manager`

### Example 2: `pyruns.load()` with YAML

Directory:

```text
examples/2_pyruns_config/
```

This one is especially good for showing:

- per-task `config.yaml` snapshots
- task detail tabs such as `Task Info` and environment metadata

![Task Detail](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/task_info.png)

## Interface modules

### Home

Home is the first-glance overview after entering a workspace.  
Its job is to answer “what is happening right now?” before pushing you into a task list.

![Home Preview](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_home.png)

This page is useful for quickly scanning:

- task totals and status distribution
- active vs done vs failed state
- GPU and system resource usage
- where to jump next: Generator, Manager, or Monitor

### Generator

The Generator is where parameter editing and task creation happen.  
It should feel fast, dense, and focused, not bloated.

![Generator Preview](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

You can use it to:

- switch templates
- edit parameters in `Form`
- edit complete config text in `YAML`
- write shell scripts in `Shell`
- pin important fields
- preview batch expansion before creating tasks

### Manager

Manager is the orchestration layer.  
This is where “seeing tasks” becomes “acting on tasks”.

![Manager Preview](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

It supports:

- multiline search
- status filtering
- batch run / delete
- pinned tasks as their own section
- detail drawer inspection
- direct navigation into Monitor logs

### Monitor

Monitor is the observation surface.  
Its job is not just to print stdout, but to make logs workable.

It supports:

- real-time log streaming
- historical log switching
- terminal copy behavior
- selected-task CSV export
- tight linkage with Manager and task detail views

## Configuration entrypoint

Workspace settings live in:

```text
<project>/_pyruns_/_pyruns_settings.yaml
```

Important keys include:

- `header_refresh_interval`
- `generator_form_columns`
- `manager_columns`
- `manager_execution_mode`
- `monitor_sidebar_width_pct`
- `shell_mode`
- `shell_executable`

The important shell mental model is:

- keep `shell_mode: follow` by default
- switch to `custom` only when you explicitly want to lock execution to a fixed shell

## Documentation

- [Getting Started](docs/getting-started.md)
- [Showcase](docs/showcase.md)
- [UI Guide](docs/ui-guide.md)
- [Configuration](docs/configuration.md)
- [Architecture](docs/architecture.md)
- [Batch Syntax](docs/batch-syntax.md)
- [CLI Guide](docs/cli-guide.md)

## License

MIT
