# Pyruns — Python 实验管理与监控 Web UI

**[English](README-en.md) | 简体中文**

<p align="center">
  <img src="https://img.shields.io/pypi/v/pyruns.svg" alt="PyPI version">
  <img src="https://img.shields.io/pypi/pyversions/pyruns.svg" alt="Python Versions">
  <img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License">
</p>

<p align="center">
  <b>🧪 一个极简、轻量级且强大的 Web UI，用于管理、批量运行和监控你的 Python 实验。</b><br>
  <i>无需繁琐的配置，即插即用，让机器学习和科学计算的调参过程优雅而高效。</i>
</p>


---

## 📦 安装

```bash
pip install pyruns
```

**运行要求:** Python ≥ 3.8  
**依赖项:** *NiceGUI, PyYAML, psutil*

---

## 🚀 快速开始

无需修改现有代码！Pyruns 开箱即用，自动兼容你的 `argparse` 脚本；

或可自行写一个config.yaml对应pyruns.load来实现加载参数、

### CLI 命令用法

```bash
# 模式 1：零配置启动 (自动解析您的 Argparse 脚本构建 UI)
pyr train.py

# 模式 2：导入自定义 YAML 配置启动 (将 YAML 作为本脚本的配置基础)
pyr train.py my_config.yaml

# 辅助命令
pyr help
pyr version
```

---

## ✨ 核心功能与界面演示

我们在 `examples/` 目录下提供了简单易懂的教程。Pyruns 通过三个核心页面为您管理实验生命周期。

### 1. Generator: 参数配置与批量生成

解析 YAML 配置或 `argparse`，并在结构化表单中优雅地编辑超参数。使用强大的**批量生成语法**瞬间创建数百个实验配置！

![Generator UI - 展示参数表单和批量语法](docs/assets/批量生成任务参数.png)

**基础用法：自动解析 Argparse（无需修改代码）**
参考 `examples/1_argparse_script/main.py`。Pyruns 会自动读取你的 `argparse` 定义，并为你构建 Generator 表单界面。
> 💡 **提示**: 当你同时定义了短参数和长参数（如 `-b, --batch_size`）时，Pyruns 会优先使用长参数名作为变量！

### 2. Manager: 任务网格与并行控制

以清晰的卡片网格形式展现所有生成的任务。按状态过滤，按名称搜索，一键选中目标任务，并利用后台工作进程池实现**多实验并行运行**！

![Manager UI - 展示任务网格与卡片](docs/assets/任务卡片页面3.png)

可查看任务详情配置：

![Manager UI - 展示任务详情配置](docs/assets/任务详情task.png)

![Manager UI - 展示任务详情配置](docs/assets/任务详情config.png)

![Manager UI - 展示任务详情配置](docs/assets/任务详情notes.png)

![Manager UI - 展示任务详情配置](docs/assets/任务详情env.png)

### 3. Monitor: 实时日志与指标记录

点击任意运行中的任务，即可在浏览器直接查看像 VSCode 终端一样的**实时 ANSI 彩色日志**！
此外，在代码中调用 `pyruns.add_monitor()` 即可记录训练指标，实验结束后支持一键导出 CSV 报告。

![Monitor UI - 展示彩色终端和指标导出](docs/assets/日志监测2.png)

**进阶用法：记录任务的最终指标**
参考 `examples/3_metrics_logging/train.py`。只需在训练结束后添加一行代码：

```python
import pyruns

# 训练过程...
loss, accuracy = 0.2, 0.95

# 记录当前运行(Run)的最终指标，便于在 UI 批量导出 CSV/JSON 报告
pyruns.add_monitor(loss=loss, accuracy=accuracy)
```

---

## 📋 进阶：批量生成语法

你可以直接在 Generator 表单中快速排队成百上千的交叉实验。

![]()

**笛卡尔积 (Product) `|`**  
生成 $3 \times 2 = 6$ 种组合。
```yaml
learning_rate: 0.001 | 0.01 | 0.1
batch_size: 32 | 64
```

**配对组合 (Zip) `(|)`**  
必须长度一致，这会精确生成 3 种组合。
```yaml
seed: (1 | 2 | 3)
experiment_name: (exp_a | exp_b | exp_c)
```

---

## ⚙️ 工作区配置 `_pyruns_`

启动时，`pyr train.py` 会自动与您的脚本同级目录下创建一个 `_pyruns_` 统一工作区文件夹。
所有 UI 核心设置共享于 `_pyruns_/_pyruns_settings.yaml`，而每个脚本都会拥有自己独立的子命名空间（如 `_pyruns_/train/config_default.yaml` 与 `_pyruns_/train/tasks/`），彻底杜绝多脚本配置冲突！

![]()

您还可以通过 CLI 显式导入自己的 YAML 作为初始配置：
```bash
# 自动复制 my_config.yaml 至 _pyruns_/train/config_default.yaml
pyr train.py my_config.yaml
```

您可以编辑设置（`_pyruns_/_pyruns_settings.yaml`）来深度定制您的 UI 交互体验：
```yaml
ui_port: 8099                      # Web UI 服务端口号
generator_form_columns: 2          # Generator 参数表单默认列数
manager_max_workers: 4             # Manager 页面并行执行允许的最大 Worker 数
manager_execution_mode: thread     # 运行模式: 线程 (thread) 或 进程 (process)
log_enabled: false                 # 是否启用文件日志
```

---

## 📚 详细文档 (Documentation)

想要发掘 Pyruns 的全部潜力？请查阅我们的官方文档：

- 🚀 [安装与快速开始](docs/getting-started.md) — 5 分钟上手指南
- ⚙️ [配置详解](docs/configuration.md) — 理解 `_pyruns_` 结构与工作区设置
- 🧪 [批量生成语法](docs/batch-syntax.md) — Product / Zip 语法详解
- 🖥️ [UI 操作指南](docs/ui-guide.md) — 掌握三大核心页面的所有细节
- 🛠️ [API 参考](docs/api-reference.md) — 在脚本内深度集成 `pyruns`
- 📐 [架构设计](docs/architecture.md) — 了解底层原理（适用于开发者）

---

## 📄 License
MIT License.
