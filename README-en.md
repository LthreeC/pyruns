# Pyruns — Python Experiment UI

**English | [简体中文](README.md)**

<p align="center">
  <img src="https://img.shields.io/pypi/v/pyruns.svg" alt="PyPI version">
  <img src="https://img.shields.io/pypi/pyversions/pyruns.svg" alt="Python Versions">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License">
</p>

<p align="center">
  <b>🧪 A minimalist, lightweight, and powerful Web UI for managing, running, and monitoring your Python experiments.</b><br>
  <i>Zero configuration. Plug and play. Focus on your code and make hyperparameter tuning elegant and efficient for ML and scientific computing.</i>
</p>

---

## 🌟 Why Pyruns?

In Machine Learning and scientific computing, **Hyperparameter Tuning** and **Experiment Management** often introduce friction into the workflow:
- Managing batch experiments via complex Shell scripts is prone to errors.
- Console outputs scattered across multiple Terminal windows are difficult to track.
- Experiment records lack structure, making it hard to reproduce optimal parameter configurations.

**Pyruns provides a lightweight solution.**
It is designed as a local-first **Task Runner and Web GUI**, rather than a heavy MLOps platform. Built on a **Zero-Config** philosophy, Pyruns automatically orchestrates a Web UI around your existing `argparse` scripts with no modifications required to your underlying code.

📌 **Key Features**
- **🔌 Zero-Code Integration:** Parses your `argparse` definitions and renders them into an interactive web form.
- **⚡ Batch Generation:** Define massive parameter grids effortlessly using internal syntax like `|` (Cartesian Product) and `(|)` (Paired mapping).
- **🚀 Parallel Execution:** Built-in task queue and worker pools (threads/processes) for efficient concurrent execution.
- **📊 Real-time Monitoring:** ANSI-colored stdout streams directly to the browser for live log tracking.
- **📈 Metrics Logging:** Use the `pyruns.add_monitor()` API to log key metrics (e.g., loss, accuracy) and export aggregated CSV/JSON reports.

---

## 📦 Installation

```bash
pip install pyruns
```

**Requirements:** Python ≥ 3.8.  
**Dependencies:** *NiceGUI, PyYAML, psutil*.

---

## 🚀 Quick Start

### Mode 1: Zero Config (Recommended)
No need to rewrite your code! Pyruns works out of the box, instantly generating a UI for your `argparse`-based scripts.

```bash
pyr train.py
```

### Mode 2: Custom YAML Config
If you prefer configuration files, you can directly import your YAML as the base parameters for the script:

```bash
pyr train.py my_config.yaml
```

**CLI Helpers**
```bash
pyr help     # View all supported CLI commands
pyr version  # Check current version
```

---

## ✨ Features & UI Showcases

We provide easy-to-follow tutorials in the `examples/` directory. Pyruns covers your entire experiment lifecycle through three core scenarios:

### 1. Generator: Configure & Batch Tasks

Load your `argparse` or YAML templates to configure hyperparameters through a structured form. Leverage **batch generation syntax** to queue parameter sweeps efficiently.

![Generator UI - showcasing forms and batch syntax](docs/assets/multi_gen.png)

> 💡 **Tip**: When parsing Argparse arguments, if both short and long forms exist (e.g., `-b, --batch_size`), Pyruns prioritizes the long argument name for clarity.

### 2. Manager: Task Console & Parallel Dispatch

Browse scheduled experiments in a clean card grid. Features include status filtering, fuzzy search, and bulk operations. Selected tasks are delegated to the worker pool for parallel execution.

![Manager UI - task grid and filter](docs/assets/tab_manager.png)

Selecting a task card displays detailed experiment metadata, including environment variables, configuration snapshots, execution logs, and user notes:

| | |
|:---:|:---:|
| **Task Info Overview**<br>![Task Details Info](docs/assets/taskinfo.png) | **Config Snapshot**<br>![Task Details Config](docs/assets/config.png) |
| **Custom Notes**<br>![Task Details Notes](docs/assets/notes.png) | **Environment Sandbox**<br>![Task Details Env](docs/assets/env.png) |

### 3. Monitor: Live Colored Logs & System Metrics

Clicking on a `running` task provides a real-time **ANSI-colored terminal log** view. The global navigation bar continuously updates hardware metrics (CPU, RAM, and Multi-GPU usage).

![Monitor UI - colored terminal log and export](docs/assets/tab_monitor.png)

**Logging Final Metrics**
To track final evaluation metrics across tasks, append the following API call at the end of your script (see `examples/3_metrics_logging/train.py`):

```python
last_loss = 0

for epoch in range(1, args.epochs + 1):
    time.sleep(0.5)  # Simulate compute
    loss = 1.0 / (epoch * args.lr * 100)
    last_loss = loss
    print(f"Epoch {epoch}/{args.epochs} - Loss: {loss:.4f}")

pyruns.add_monitor(last_loss=last_loss)
```

![Monitor UI - export](docs/assets/export_report.png)

---

## 📋 Batch Generation Syntax 

Pyruns supports specific syntax formulations within the UI to facilitate Grid Search creation.

![]()

**Product (Cartesian) `|`**  
Computes a Cartesian product. The configuration below produces $3 \times 2 = 6$ parameter pairs.
```yaml
learning_rate: 0.001 | 0.01 | 0.1
batch_size: 32 | 64
```

**Zip (Paired) `(|)`**  
Matched by position. The arrays wrapped in parentheses must have the same length. This accurately generates exactly 3 combinations.
```yaml
seed: (1 | 2 | 3)
experiment_name: (exp_a | exp_b | exp_c)
```

---

## ⚙️ Workspace Isolation `_pyruns_`

Pyruns handles filesystem outputs systematically. Running `pyr train.py` generates a `_pyruns_` workspace directory adjacent to the script.

Global settings are stored in `_pyruns_/_pyruns_settings.yaml`. Crucially, experiment outputs and histories are **isolated by the entry script's name** (e.g., outputs under `_pyruns_/train/` are kept separate from `_pyruns_/test/`), mitigating file overwrite conflicts.

![]()

You can edit the global settings (`_pyruns_/_pyruns_settings.yaml`) to deeply customize your UI experience:
```yaml
ui_port: 8099                      # Web UI port
generator_form_columns: 2          # Default expanded columns in generator form
manager_max_workers: 4             # Max parallel threads/processes in Manager
manager_execution_mode: thread     # Execution scheduling mode: thread or process
log_enabled: false                 # Enable extra debug logging output
```

---

## 📚 Detailed Documentation

Want to unlock Pyruns' full potential? Check out our official documentation:

- 🚀 [Getting Started](docs/getting-started.md) — 5-minute setup workflow
- ⚙️ [Configuration Guide](docs/configuration.md) — Understand `_pyruns_` philosophy and isolation
- 🧪 [Batch Syntax](docs/batch-syntax.md) — Deep dive into Product, Zip, and Range syntax
- 🖥️ [UI User Guide](docs/ui-guide.md) — Master advanced interactions in Generator, Manager, and Monitor
- 🛠️ [API Reference](docs/api-reference.md) — For power users: Deep integration inside your code
- 📐 [Architecture](docs/architecture.md) — Internal scheduling logic (for developers)

---

## 📄 License
MIT License.
