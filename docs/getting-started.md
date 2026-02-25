# 安装与快速开始

5 分钟上手 Pyruns 的完整工作流。

---

## 系统要求

| 项目 | 要求 | 备注 |
|------|------|------|
| Python | ≥ 3.8 | 推荐 `conda` 或 `venv` 虚拟环境 |
| 操作系统 | Windows / Linux / macOS | 跨平台 |
| GPU 监控 | NVIDIA GPU + `nvidia-smi` | 可选，有则自动显示 GPU 利用率 |

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

1. 通过 AST 静态分析提取所有 `argparse` 参数定义
2. 在 `_pyruns_/train/` 下生成 `config_default.yaml`（含默认值与 help 文本）
3. 启动 Web UI 并在浏览器中打开参数配置表单

### 模式二：自定义 YAML 配置

当脚本使用 `pyruns.load()` 读取配置（而非 `argparse`）时，首次运行时传入配置文件：

```bash
pyr train.py my_config.yaml
# → my_config.yaml 复制到 _pyruns_/train/config_default.yaml
```

后续运行无需再指定 YAML，Pyruns 自动加载已保存的模板：

```bash
pyr train.py
# → 自动加载 _pyruns_/train/config_default.yaml
```

如需更换模板，再次传入即可覆盖。

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

> 开发调试推荐 `pyr dev train.py`（热重载模式）。

### 3. Generator — 配置参数

浏览器自动打开 `http://localhost:8099`，显示从 `train.py` 解析生成的参数表单。

![Generator](assets/tab_generator.png)

1. 修改参数，例如将 `lr` 改为 `0.01`
2. （可选）填写任务名称前缀，如 `fast-tuning`
3. 点击 **GENERATE** 生成任务

### 4. Manager — 运行任务

切换到 Manager 页面：

1. 新任务显示为灰色 `Pending` 状态
2. 勾选后点击 **RUN SELECTED**
3. 状态变为橙色 `Running`

![Manager](assets/tab_manager.png)

### 5. Monitor — 查看日志

切换到 Monitor 页面，选中正在运行的任务即可查看 ANSI 彩色实时日志流。

![Monitor](assets/tab_monitor.png)

---

## 工作区结构

运行后的目录：

```text
your_project/
├── train.py
└── _pyruns_/
    ├── _pyruns_settings.yaml      # 全局 UI 设置
    └── train/                     # 按脚本名隔离
        ├── script_info.json       # 脚本元信息
        ├── config_default.yaml    # 参数模板
        └── tasks/
            ├── fast-tuning/
            │   ├── task_info.json # 任务元数据
            │   ├── config.yaml    # 参数快照
            │   └── run_logs/
            │       └── run1.log   # 控制台输出
            └── .trash/            # 软删除（可恢复）
```

不同脚本在 `_pyruns_/` 下完全隔离（如 `train/` 和 `test/`），互不干扰。

---

## 下一步

- [界面操作手册](ui-guide.md) — Generator / Manager / Monitor 进阶操作
- [批量生成语法](batch-syntax.md) — Product (`|`) 与 Zip (`(|)`) 参数网格
- [API 参考](api-reference.md) — `read()` / `load()` / `add_monitor()`
