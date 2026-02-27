# 安装与快速开始

5 分钟上手 Pyruns 的完整工作流。

---

## 系统要求

| 项目 | 要求 | 备注 |
|------|------|------|
| Python | ≥ 3.8 | 推荐 `conda` 或 `venv` 虚拟环境 |
| 操作系统 | Windows / Linux / macOS | 跨平台 |
| GPU 监控 | NVIDIA GPU + `nvidia-smi` | 可选，有则自动在顶部显示 GPU 利用率 |

---

## 安装

**pip（推荐）**

```bash
pip install pyruns
```

**源码安装（开发者）**

```bash
git clone https://github.com/LthreeC/pyruns.git
cd pyruns
pip install -e .
```

核心依赖仅三项：`nicegui`（Web 框架）、`pyyaml`（配置解析）、`psutil`（系统指标采集）。

---

## 两种启动模式

### 模式一：自动解析 Argparse（推荐）

适用于任何使用 `parser.add_argument(...)` 的 Python 脚本，无需改动任何代码。

```bash
pyr train.py
```

执行后 Pyruns 会：

1. 通过 AST 静态分析隐式提取出文件中所有的 `argparse` 参数定义
2. 在 `_pyruns_/train/` 下生成 `config_default.yaml`（含默认值与 help 文本）
3. 启动 Web UI 并在浏览器中呈现参数配置表单

### 模式二：自定义 YAML 配置

当脚本不使用命令行参数，而是直接在代码深处调用 `pyruns.load()` 读取配置时，你只需要在首次运行时传入一个模板：

```bash
pyr train.py my_config.yaml
# → my_config.yaml 将会自动被拷贝为 _pyruns_/train/config_default.yaml 作为表单原型
```

由于已经保存了原型，后续运行无需再指定 YAML，仅仅需要这么跑：

```bash
pyr train.py
```

如需更换最初的参数模板，再次带上另一个文件名传入即可覆盖。

---

## 动手实践：第一个实验

### 1. 准备脚本

```python
# train.py
import argparse
import time

parser = argparse.ArgumentParser()
parser.add_argument("--lr", type=float, default=0.001, help="学习率")
parser.add_argument("--epochs", type=int, default=10, help="训练轮数")
parser.add_argument("--batch_size", type=int, default=32, help="批大小")
args = parser.parse_args()

print(f"[INIT] LR={args.lr}, EPOCHS={args.epochs}, BATCH={args.batch_size}")
for epoch in range(args.epochs):
    time.sleep(0.5)
    print(f"Epoch {epoch+1}/{args.epochs} done")
```

### 2. 启动 UI

```bash
pyr train.py
```

> 开发与调试时，推荐使用 `pyr dev train.py`（开启后端热重载）。

### 3. Generator — 配置参数

你的默认浏览器会自动打开 `http://localhost:8099`，并显示从 `train.py` 解析出的参数表单。

![Generator](assets/tab_generator.png)

1. 在左侧表单中修改基础参数，或在右下角 YAML 输入框中使用类似 `lr: 0.001 | 0.01` 的语法定义参数网格。
2. （可选）为这组任务设定一个统一的前缀，例如 `fast-tuning`。
3. 点击 **GENERATE**，系统将解析参数网格并在后台生成对应的多个隔离任务记录。

### 4. Manager — 并行调度任务

切换到 Manager 页面：

1. 刚刚生成的所有任务会出现在列表中，并处于灰色的 `Pending` 状态。
2. 勾选需要执行的任务卡片（或使用全选功能），然后在右上角控制面板中设定并发 `Workers` 数，接着点击 **RUN SELECTED**。
3. 被成功调度的任务状态将过渡到橙色的 `Running` 状态。

![Manager](assets/tab_manager.png)

### 5. Monitor — 追踪流式日志

在处理多个并发任务时，如需检视特定任务的输出表现或异常堆栈，可切换至 Monitor 页面：
在左侧任务队列中选中目标项目，右侧终端面板即可实时显示其标准输出。

![Monitor](assets/tab_monitor.png)

---

## 隔离的工作区机制

为确保项目源码空间清洁以及多次实验环境配置的独立性，任何运行历史都会隔离并映射在当前运行目录的子结构下：

```text
your_project/
├── train.py
└── _pyruns_/
    ├── _pyruns_settings.yaml      # 全局界面行为偏好规范
    └── train/                     # 按入口脚本同名的独立分块
        ├── script_info.json       # 绑定绝对路径与解析类型
        ├── config_default.yaml    # 提取的默认基础配置
        └── tasks/
            ├── fast_tuning-[1-of-6]/
            │   ├── task_info.json # 承载时间戳、历次运行 PID 及聚合监控结构等状态机数据
            │   ├── config.yaml    # 固定的任务运行专属环境变量快照
            │   └── run_logs/
            │       └── run1.log   # 终端输出捕捉切片
            └── .trash/            # 执行软删除操作产生的缓冲区域
```

---

## 下一步

- [UI 界面进阶操作](ui-guide.md) — 分析界面内各组件的互动功能（参数置顶、并发限制、批量导出等）
- [批量参数语法解析](batch-syntax.md) — 规则定义如 Product (`|`) 与 Zip (`(|)`) 联合网格化操作
- [内部 API 规范](api-reference.md) — 利用 `pyruns.read()` 及扩展 `add_monitor()` 自主调用功能详细指导
