# Pyruns — Python 实验管理与监控 UI

**[English](README-en.md) | 简体中文**

<p align="center">
  <b>🧪 一个轻量级 Web UI，用于管理、运行和监控 Python 实验。</b>
</p>

---

## 📦 安装

```bash
pip install pyruns
```

**运行要求:** Python ≥ 3.8  
**依赖项:** *NiceGUI, PyYAML, psutil*

---

## 📚 说明文档 (Documentation)
- 🚀 [安装与快速开始](docs/getting-started.md) — 5 分钟上手指南
- ⚙️ [配置详解](docs/configuration.md) — 理解 `_pyruns_` 结构与工作区设置
- 🧪 [批量生成语法](docs/batch-syntax.md) — Product / Zip 语法详解
- 🖥️ [UI 操作指南](docs/ui-guide.md) — 掌握 Generator/Manager/Monitor 页面的所有细节
- 🛠️ [API 参考](docs/api-reference.md) — 在脚本内深度集成 `pyruns`
- 📐 [架构设计](docs/architecture.md) — 了解底层原理（适用于开发者）

---

## 🚀 快速开始

无需修改现有代码！Pyruns 开箱即用，自动兼容您现有的 `argparse` 命令行参数。只需在您的 Python 脚本前加上 `pyr` 即可启动它。

```bash
pyr your_script.py       # 启动 Pyruns Web 界面
pyr help                 # 查看帮助信息
```

## ✨ 核心界面与示例教程

我们在 `examples/` 目录下提供了简单易懂的教程。Pyruns 提供三个核心页面来管理您的 Python 实验生命周期。

### 1. Generator: 参数配置与批量生成
解析 YAML 配置或 `argparse`，并在结构化表单中编辑超参数。使用强大的批量生成语法瞬间创建数百个实验配置！

**基础用法：自动解析 Argparse（无需修改代码）**
参考 `examples/1_argparse_script/main.py`。Pyruns 会自动读取你的 `argparse` 定义，并为你构建 Generator 表单界面。
> 💡 **提示**: 当你同时定义了短参数和长参数（如 `-b, --batch_size`）时，Pyruns 会智能地优先使用**长参数名**（即 `batch_size`）作为变量！
```bash
pyr examples/1_argparse_script/main.py
```

**基础用法：读取 Pyruns Config**
如果你不想用命令行参数，你可以直接让你的脚本读取 pyruns 生成的配置文件：
参考 `examples/2_pyruns_config/main.py`。

```python
import pyruns

# 加载配置
config = pyruns.load()
print(f"Loading config items: {config.learning_rate}")
```

### 2. Manager: 任务网格与批量操作
以清晰的卡片网格形式展现所有生成的任务。按状态（Queued、Running、Failed）过滤，按名称搜索，利用多个后台 Worker 进程并行运行实验！

### 3. Monitor: 实时日志与指标记录
点击任意运行中的任务即可查看实时 ANSI 彩色终端日志。在代码中调用 `pyruns.add_monitor()`，即可轻松记录训练指标，并在实验结束后一键导出为 CSV 任务报告。

**进阶用法：记录训练指标**
参考 `examples/3_metrics_logging/train.py`。只需在代码中加一行，即可在 UI 中追踪模型损失 (loss)、准确率 (accuracy) 和 epochs！

```python
import pyruns

for epoch in range(100):
    loss, accuracy = train_one_epoch()
    
    # 记录每个 epoch 的指标，便于实验后批量导出 CSV 报告
    pyruns.add_monitor(epoch=epoch, loss=loss, accuracy=accuracy)
```

---

## 📋 批量生成语法

你可以直接在 Generator 表单中快速排队成百上千的交叉实验。

**笛卡尔积 (Product) |**
生成 $3 \\times 2 = 6$ 种组合。
```yaml
learning_rate: 0.001 | 0.01 | 0.1
batch_size: 32 | 64
```

**配对组合 (Zip) (|)**
必须长度一致，这会精确生成 3 种组合。
```yaml
seed: (1 | 2 | 3)
experiment_name: (exp_a | exp_b | exp_c)
```

---

## ⚙️ 工作区配置

启动时，`pyr` 会与您的脚本同级目录下自动创建一个 `_pyruns_` 工作区文件夹。
在里面可以找到 `tasks/` (存放任务)、`config_default.yaml` 以及 `_pyruns_settings.yaml`。

您可以编辑此文件来修改 UI 的布局列数、默认端口、并行 Worker 数量和刷新频率等！
```yaml
ui_port: 8099                      # Web UI 服务端口
generator_form_columns: 2          # Generator 表单的列数
manager_max_workers: 4             # 并行运行的脚本进程数量
manager_execution_mode: thread     # 运行模式: 线程 (thread) 或 进程 (process)
monitor_poll_interval: 1.0         # Monitor 日志轮询间隔（秒）
log_enabled: false                 # 是否启用文件日志
```

---

## 📄 开源协议
MIT License.
