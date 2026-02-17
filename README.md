# Pyruns — Python Experiment Runner & Monitor

<p align="center">
  <b>🧪 A lightweight web UI for managing, running, and monitoring Python experiments.</b>
</p>

---

## ✨ Features

| Feature | Description |
|---------|------------|
| **Generator** | Load YAML configs, edit parameters in a structured form or raw YAML editor, batch-generate tasks with product (`\|`) and zip (`(\|)`) syntax |
| **Manager** | Card-grid overview of all tasks with status filters, search, batch run/delete, adjustable columns |
| **Monitor** | Real-time ANSI-colored log viewer, task list with live status, export reports to CSV/JSON |
| **System Metrics** | Live CPU, RAM, and GPU summary (count × avg utilization) in the header |
| **Auto Config Detection** | `pyr script.py` detects `argparse` parameters or `pyruns.read()` calls automatically |
| **Workspace Settings** | Customise UI defaults (refresh intervals, grid columns, workers) via `_pyruns_.yaml` |

## 📦 Installation

```bash
pip install pyruns
```

### Dependencies

- Python ≥ 3.8
- [NiceGUI](https://nicegui.io/) — web UI framework
- [PyYAML](https://pyyaml.org/) — YAML parsing
- [psutil](https://github.com/giampaolo/psutil) — system metrics
- `nvidia-smi` (optional) — GPU metrics

## 🚀 Quick Start

### CLI Mode (recommended)

```bash
pyr your_script.py       # Launch UI for your script
pyr dev your_script.py   # Launch with hot-reload (for development)
pyr help                 # Show usage instructions
```

`pyr` will:
1. Detect parameters from your script (argparse or `pyruns.read()`)
2. Generate `_pyruns_/config_default.yaml` (for argparse scripts)
3. Create `_pyruns_/_pyruns_.yaml` with editable UI defaults
4. Open the web UI at `http://localhost:8080`

### In Your Script

```python
import pyruns

# Under pyr — load() auto-reads the task config, no read() needed
config = pyruns.load()
print(config.lr, config.epochs)

# Record metrics for the Monitor page
for epoch in range(100):
    loss = train(config)
    pyruns.add_monitor(epoch=epoch, loss=loss)
```

When running standalone (`python train.py`), specify a config explicitly:

```python
pyruns.read("path/to/config.yaml")   # explicit path
config = pyruns.load()               # then load as usual
```

## ⚙️ Workspace Settings

On first launch, `pyr` creates `_pyruns_/_pyruns_.yaml`:

```yaml
ui_port: 8080                      # web UI port
header_refresh_interval: 3         # metrics refresh (seconds)
generator_form_columns: 2          # parameter editor columns
generator_auto_timestamp: true     # auto-name tasks with timestamp
manager_columns: 5                 # task card grid columns
manager_max_workers: 1             # parallel worker count
manager_execution_mode: thread     # thread | process
manager_poll_interval: 2           # Manager polling (seconds)
monitor_poll_interval: 1           # Monitor polling (seconds)
```

Edit this file to customise the UI for your workflow.

## 📋 Batch Syntax

```yaml
# Product (cartesian): 3 × 2 = 6 combinations
lr: 0.001 | 0.01 | 0.1
batch_size: 32 | 64

# Zip (paired): lengths must match
seed: (1 | 2 | 3)
name: (exp_a | exp_b | exp_c)
```

## 📄 License

MIT

---

# Pyruns — Python 实验管理与监控工具

<p align="center">
  <b>🧪 一个轻量级 Web UI，用于管理、运行和监控 Python 实验。</b>
</p>

---

## ✨ 功能特性

| 功能 | 说明 |
|------|------|
| **Generator** | 加载 YAML 配置，结构化表单 / 原始 YAML 编辑，支持 `\|` 笛卡尔积和 `(\|)` 配对批量生成 |
| **Manager** | 卡片网格管理任务，状态过滤、搜索、批量运行/删除 |
| **Monitor** | 实时 ANSI 彩色日志查看，任务状态监控，导出 CSV/JSON |
| **系统指标** | 顶栏实时显示 CPU、RAM、GPU 概览（数量 × 平均利用率） |
| **自动检测** | `pyr script.py` 自动提取 argparse 参数或检测 `pyruns.read()` |
| **工作区配置** | 通过 `_pyruns_.yaml` 自定义刷新间隔、网格列数、并行数等 |

## 📦 安装

```bash
pip install pyruns
```

## 🚀 快速开始

```bash
pyr your_script.py       # 启动 UI
pyr dev your_script.py   # 热加载模式（开发调试用）
pyr help                 # 查看使用说明
```

### 在脚本中使用

```python
import pyruns

# pyr 模式下，load() 自动读取任务配置，无需手动 read()
config = pyruns.load()

# 记录训练指标（Monitor 页面可查看）
pyruns.add_monitor(epoch=1, loss=0.5, acc=92.3)
```

手动运行时（`python train.py`）：

```python
pyruns.read("path/to/config.yaml")   # 指定配置路径
config = pyruns.load()
```

## ⚙️ 工作区配置

首次启动时自动生成 `_pyruns_/_pyruns_.yaml`，可编辑以自定义 UI 默认值：

```yaml
ui_port: 8080                      # Web UI 端口
header_refresh_interval: 3         # 顶栏刷新间隔（秒）
generator_form_columns: 2          # 参数编辑器列数
generator_auto_timestamp: true     # 自动时间戳命名
manager_columns: 5                 # 任务卡片网格列数
manager_max_workers: 1             # 默认并行数
manager_execution_mode: thread     # thread | process
manager_poll_interval: 2           # Manager 轮询间隔（秒）
monitor_poll_interval: 1           # Monitor 轮询间隔（秒）
```

## 📋 批量生成语法

```yaml
# 笛卡尔积：3 × 2 = 6 种组合
lr: 0.001 | 0.01 | 0.1
batch_size: 32 | 64

# 配对组合：长度必须一致
seed: (1 | 2 | 3)
name: (exp_a | exp_b | exp_c)
```

## 📄 开源协议

MIT
