# 🚀 安装与快速开始

欢迎使用 Pyruns！本文档将用最短的时间（约 5 分钟）带你体验 **“零配置、代码无侵入”** 的实验之旅。

---

## 💻 系统要求

| 项目 | 要求 | 备注 |
|------|------|------|
| **Python** | ≥ 3.8 | 推荐使用虚拟环境 (`conda` 或 `venv`) |
| **操作系统** | Windows / Linux / macOS | 跨平台支持 |
| **GPU 监控** | NVIDIA GPU + `nvidia-smi` | 可选。如果存在，右上角将显示实时的 GPU 利用率和显存 |

## 📦 安装

### 方式 1：通过 Pip 安装（推荐）

```bash
pip install pyruns
```

### 方式 2：从源码安装（开发者）

```bash
git clone https://github.com/LthreeC/pyruns.git
cd pyruns
pip install -e .
```

*附注：核心轻量级依赖项仅包含 `nicegui` (基于 FastAPI + Vue 的前端驱动), `pyyaml` (解析配置), 以及 `psutil` (采集系统资源)。*

---

## ⚡ 核心理念：Zero-Config (零配置)

市面上的实验管理工具通常需要重构代码、学习复杂的 API，并在脚本中添加样板代码。

**Pyruns 采用无侵入式的架构设计。**

其核心理念在于：**只要脚本使用了标准的 `argparse`，即可直接生成对应的 Web GUI，无需修改任何业务逻辑代码。**

---

## 🏃 快速开始模式

### 模式一：自动解析 Argparse 脚本（推荐）

这是最推荐的接入模式。对于标准的、包含 `parser.add_argument(...)` 的 Python 训练脚本。

```bash
pyr train.py
```

`pyr` 命令行会自动执行以下操作：
1. **静态扫描**：通过 AST 语法树解析读取 `argparse` 定义。
2. **生成默认配置**：在当前目录下创建 `_pyruns_/train/config_default.yaml`，提取所有参数的默认值和帮助文档。
3. **启动 Web 界面**：根据上述信息，为您启动一个可交互的 Web 参数配置表单。

### 模式二：基于 YAML 的配置模式

如果您的项目使用 YAML 作为配置文件，可以直接通过命令行传入：

```bash
pyr train.py my_base_config.yaml
```

这会将 `my_base_config.yaml` 复制为当前脚本的默认参数模板，方便在界面中进行修改和复制。

---

## 🎯 动手做：你的第一个实验

让我们在不改动业务逻辑的前提下，体验 Pyruns 流程。

### 1. 准备你的旧代码

新建一个 `train.py`，模拟一个普通的机器学习训练过程：

```python
# train.py
import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--lr", type=float, default=0.001, help="初始学习率")
parser.add_argument("--epochs", type=int, default=10, help="训练轮数")
parser.add_argument("--batch_size", type=int, default=32, help="数据批大小")
args = parser.parse_args()

print(f"🚀 [INIT] 开始训练: LR={args.lr}, EPOCHS={args.epochs}, BATCH={args.batch_size}")
for epoch in range(args.epochs):
    time.sleep(0.5)  # 模拟训练耗时
    print(f"🔄 Epoch {epoch+1}/{args.epochs} 完成")
```

### 2. 赋予其 GUI

就在这个文件所在的目录下，直接打开终端：

```bash
pyr train.py
```
*(如果是开发环境调试，推荐使用 `pyr dev train.py` 开启热重载模式)*

### 3. 在 Generator 页面调参 (排队)

终端将输出本地服务地址，并在默认浏览器中自动打开 `http://localhost:8099`。您将看到基于 `train.py` 自动渲染的 Web 配置表单。

![Generator UI 示例图](assets/tab_generator.png)

1. 在 UI 里修改参数，比如把 `lr` 改成 `0.01`。
2. （可选）为本次实验指定名称，比如 `fast-tuning`。
3. 点击底部的 **GENERATE** 按钮，实验配置即自动创建。

### 4. 在 Manager 页面运行

切换到左侧第二个 Tab（Manager 页面）。

1. 刚刚创建的任务正处于灰色的 `pending`（待运行）状态。
2. 选中其复选框。
3. 点击右侧的 **RUN SELECTED** 按钮。
4. 任务状态将更新为橙色的 `running`。

![Manager UI 示例图](assets/tab_manager.png)

### 5. 在 Monitor 页面查看日志

切换到左侧第三个 Tab（Monitor 页面）。

选择左侧列表里正在运行的任务。
浏览器中会展示支持 ANSI 颜色的流式日志输出，日志会根据控制台的打印实时自动滚动。

![Monitor UI 示例图](assets/tab_monitor.png)

---

## 📁 目录结构与工作区隔离

运行 `pyr train.py` 后，项目目录下的文件结构如下：

```text
your_project/
├── train.py                          # 原始脚本
└── _pyruns_/                         # Pyruns 专属工作目录
    ├── _pyruns_settings.yaml         # 全局 UI 首选项 (端口, 执行器模式等)
    └── train/                        # <--- 当前脚本专属工作区
        ├── config_default.yaml       # 自动提取的默认参数模板
        └── tasks/                    # 存储该脚本所有实验记录
            ├── fast-tuning/          # 单次实验数据目录
            │   ├── task_info.json    # 实验元数据 (PID, 状态, 时间)
            │   ├── config.yaml       # 本次实验的参数快照
            │   └── run.log           # 本次实验的标准输出日志文件
            └── .trash/               # 被软删除的实验记录
```

**目录隔离策略规则解释**
不同入口脚本在 `_pyruns_` 下拥有独立的文件夹。例如运行 `pyr test.py` 时，将在 `_pyruns_/test/` 目录下生成数据。此设计有效防止了多个脚本间的配置混淆和运行日志相互覆盖。

## ⏭️ 下一步

了解更多 Pyruns 的进阶特性：

- [🖱️ Web 界面操作手册](ui-guide.md) — 了解参数模板、批量调度与监控面板的详细操作
- [🧪 批量生成语法指南](batch-syntax.md) — 掌握使用 Product (`|`) 与 Zip (`(|)`) 生成多维参数网格
- [🛠️ 开发者 API 参考](api-reference.md) — 学习如何使用 API 记录评估指标并导出报告

