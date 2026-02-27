# Pyruns

**[English](README-en.md) | 简体中文**

<p align="center">
  <img src="https://img.shields.io/pypi/v/pyruns.svg?style=for-the-badge&color=blue" alt="PyPI version">
  <img src="https://img.shields.io/pypi/pyversions/pyruns.svg?style=for-the-badge" alt="Python Versions">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=for-the-badge" alt="License">
</p>

<p align="center">
  <b>Python 实验管理 Web UI 工具：提供参数可视化、批量调度与资源监控功能</b>
</p>

Pyruns 为 Python 脚本提供基于本地浏览器的图形界面。其主要功能包括：通过解析 `argparse` 生成可视化设置表单、支持特定语法实现参数网格的批量任务生成、管理并行调度队列，以及在浏览器端提供 ANSI 彩色日志的实时流式输出和跨任务的指标聚合导出。

**当前工具无需修改原生业务代码，且所有流程均在本地环境执行。**

```bash
pip install pyruns
pyr train.py          # 启动 Web UI 并接管当前脚本
```

---

## 💡 解决的工程痛点

在日常的模型调参过程中，修改和记录不同参数组合的实验结果往往是一件极为繁琐且容易出错的事情：
- 编写多层嵌套的 Bash 循环脚本进行超参搜索不仅冗长且难以维护。
- 手动记录每一组参数以及它对应的实验结果费时费力，且随时间推移极易混淆或丢失。
- 在多参数并发执行时，控制台标准输出日志互相交错，错误定位困难。

**Pyruns 提供的核心应对方案，是为了让繁琐的“参数修改与实验追踪”变得极其简单：**
- ✅ **极简的任务生成**：对现有 `argparse` 进行非侵入式解析，并提供**简洁、清晰可见的 Web UI 参数编辑器**。支持声明式语法（如 `lr: 0.001 | 0.01`）一键展开参数网格，生成互相隔离的任务。
- ✅ **便捷的历史任务管理**：你可以非常方便地对之前运行过的实验任务进行记录追溯、条件搜索、复用以及状态管理。
- ✅ **流式隔离的实时查看**：无须担心因为 SSH 终端断开而中断任务。任务运行时，在浏览器即可实时查看每个独立任务的输出与错误堆栈。
- ✅ **变量监视与一键导出报告**：提供简易 API 监视任务运行过程中的关键指标，并在前端支持跨任务的比对，甚至一键合并且导出综合报表。

---

## ✨ 核心特性

| 特性 | 说明 |
|------|------|
| 🔌 **静态参数解析** | 从源码中提取 `argparse` 定义来渲染 Web UI 表单；亦支持直接基于 `yaml` 文件的配置。 |
| 🧮 **参数网格展开** | 支持使用 `\|` 进行笛卡尔积排列或 `(\|)` 进行配对组合，用于生成批量参数配置集。 |
| ⚡ **独立任务队列** | 内部实现任务队列缓冲，可选 Thread/Process pool 型执行器控制并发规模。 |
| 📋 **状态看板管理** | 对任务进行 Pending/Running/Failed/Completed 状态流转管理，支持对历史配置的搜索与复用。 |
| 🖥️ **流式彩色终端** | 增量返回终端输出并完整支持 ANSI 转义字符渲染（如 `tqdm`）。 |
| 📊 **指标监控聚合** | 提供 `pyruns.add_monitor()` 回调，帮助在多组参数实验后导出 CSV/JSON 等合并报告。 |
| 📁 **环境参数快照** | 在 `_pyruns/` 下建立与脚本结构绑定的运行时目录，快照当前环境与参数，防干扰且支持软删除安全机制。 |

---

## 🚀 启动方式

### 模式 1：基于 Argparse 的脚本接入

无需修改源码。Pyruns 通过 AST 分析器提取源码中 `add_argument` 的参数。

```bash
pyr train.py
```
这会在脚本所在目录下生成 `_pyruns_/train/config_default.yaml`，随后启动本地的 Web 服务（默认监听端口 `8099`）。

### 模式 2：基于 YAML 加载逻辑的接入

若您的源码直接通过读取外部 YAML 配置驱动并在代码内调用 `pyruns.load()`：

```bash
# 首次运行：传入默认参数模板
pyr train.py my_config.yaml  
```
此后，Pyruns 会接管参数调度及其相关环境变量：

```bash
pyr train.py
```

---

## 🎯 界面模块

### 🔧 Generator — 简洁清晰的参数编辑器
在左侧提供清晰可见的结构化表单控制超参数修改，并支持声明化批量语法；右侧可实时预览将会并行生成的批量实验任务。通过（Pin）功能可以方便地标记核心参数。
![Generator UI](docs/assets/multi_gen.png)

### 📦 Manager — 便捷的历史任务记录与管理
核心的任务管理面板。能够极其方便地监控、搜索并管理所有生成的任务队列。支持勾选进行并发执行限制；点击进入任务卡片，可查阅其精准的参数快照 (`config.yaml`) 历史记录。
![Manager UI](docs/assets/tab_manager.png)

<details>
<summary><b>🔥 点击查看卡片内部详情弹窗特性</b></summary>

| 特性 | 视图预览 |
| :---: | :---: |
| **生命周期总览**<br>重跑历史与 PIDs 记录 | ![Task Details Info](docs/assets/taskinfo.png) |
| **绝对隔离快照**<br>独享的 `config.yaml` 映射 | ![Task Details Config](docs/assets/config.png) |
| **实验笔记记录**<br>支持随时修改的实验副文本 | ![Task Details Notes](docs/assets/notes.png) |
| **环境变量溯源**<br>完整还原任务启动时的全部系统变量 | ![Task Details Env](docs/assets/env.png) |

</details>

### 📈 Monitor — 实时查看与一键导出报告
将处于活跃状态的任务的标准输流出实时定向到浏览器终端。同时，监视到的任务运行指标（如 Loss、Accuracy 等），在此支持跨任务勾选，一键导出聚合比对报表。
![Monitor UI](docs/assets/tab_monitor.png)

---

## 🧪 批量生成语法

在 Generator 的代码域内支持特定的管道解析语法，用于描述性构建执行计划：

**笛卡尔积组合 `|`** — 全排列组装（如 3 × 2 = 6 个独立任务）：
```yaml
learning_rate: 0.001 | 0.01 | 0.1
batch_size: 32 | 64
```

**一一配对映射 `(|)`** — 等长对应（共 3 个独立任务）：
```yaml
seed: (1 | 2 | 3)
experiment_name: (exp_a | exp_b | exp_c)
```
支持数字序列区间（如：`lr: 1:10:2`）。详细结构规则见 [批量构建语法说明](docs/batch-syntax.md)。

---

## 📂 运行区结构原理

Pyruns 采用隔离持久化策略。针对单一入口脚本所生成的执行缓存符合以下文件树形规约：

```text
your_project/
├── train.py
└── _pyruns_/
    ├── _pyruns_settings.yaml         # 全局端口、并发数相关配制 
    └── train/                        # 对应该脚本的独立命名空间
        ├── script_info.json          # 记录脚本路径与环境依赖
        ├── config_default.yaml       # UI 表单所需的参数初始骨架 
        └── tasks/
            ├── fast_tuning_[1-of-6]/
            │   ├── task_info.json    # 元数据状态机（存放执行PID、启停时间、监控数据等）
            │   ├── config.yaml       # 该任务启动时的确切参数配置切片
            │   └── run_logs/
            │       ├── run1.log      # 主流终端输出
            │       └── error.log     # 非零状态码退出时的标准错误分离堆栈
            └── .trash/               # Manager界面执行的非实质性软删除回收站
```

底层调用逻辑在执行队列发出 Run 信号时生效，执行器将 `config.yaml` 对应路径经由 `__PYRUNS_CONFIG__` 环境变量置入被唤醒的子进程。

---

## 📖 补充文档

| 文档部分 | 说明 |
|------|------|
| [📗 安装部署与初次接入](docs/getting-started.md) | 环境说明与第一个示例运作 |
| [📘 批量语法细则](docs/batch-syntax.md) | 复杂网格规则与类型推断行为 |
| [📕 界面高级操作与控制](docs/ui-guide.md) | Manager执行限制细节及报表数据的导出等 |
| [📙 配置流转与沙盒说明](docs/configuration.md) | Node树层级、优先读取顺位判定 |
| [📓 API 接口文档](docs/api-reference.md) | 深入代码端的 `read()` / `load()` 及 `add_monitor()` 功能介绍 |

---

## License

MIT
