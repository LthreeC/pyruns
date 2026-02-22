# 配置系统文档

## 概述

Pyruns 的配置系统围绕 YAML 文件构建，支持嵌套结构、自动类型推断和批量参数展开。

## 配置文件

### `config_default.yaml` — 模板配置

由 `pyr` 自动生成，存放在 `_pyruns_/` 根目录。包含脚本中所有 `argparse` 参数的默认值。

```yaml
# Auto-generated for train.py

lr: 0.001  # 学习率
epochs: 10  # 训练轮数
batch_size: 32  # 批大小
model: resnet50
```

**特点**：
- 在 Generator 页面中以只读模板形式显示（左上角有 🔒 标记）
- 编辑不会修改原始文件
- 用户编辑的是内存中的副本，生成任务时写入独立的 `config.yaml`

### `config.yaml` — 任务级配置

每个任务目录包含一个独立的 `config.yaml`，是从模板生成时的参数快照。

```yaml
lr: 0.01
epochs: 20
batch_size: 64
model: resnet50
```

**特点**：
- 不包含内部元数据字段（以 `_meta` 开头的键会被过滤）
- 批量生成时，每个任务的 `config.yaml` 包含该任务对应的参数组合

### `task_info.json` — 任务元数据

每个任务目录包含一个 `task_info.json`，记录任务的完整生命周期数据。

```json
{
    "id": "2026-02-13_10-30-45_1707817845123",
    "name": "baseline-[1-of-6]",
    "status": "completed",
    "created_at": "2026-02-13 10:30:45",
    "progress": 1.0,
    "pinned": false,
    "env": {
        "CUDA_VISIBLE_DEVICES": "0"
    },
    "script": "/path/to/train.py",
    "run_at": "2026-02-13 10:31:00",
    "run_pid": null,
    "rerun_at": ["2026-02-13 11:00:00"],
    "rerun_pid": [null],
    "notes": "首次实验，baseline 配置",
    "monitor": [
        {"epoch": 1, "loss": 0.892, "acc": 45.2, "_ts": "2026-02-13 10:31:05"},
        {"epoch": 2, "loss": 0.534, "acc": 72.1, "_ts": "2026-02-13 10:31:10"}
    ]
}
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `id` | `string` | 唯一标识符（时间戳 + 毫秒） |
| `name` | `string` | 显示名称（= 目录名） |
| `status` | `string` | `pending` / `queued` / `running` / `completed` / `failed` |
| `created_at` | `string` | 创建时间 |
| `progress` | `float` | 进度 0.0 ~ 1.0 |
| `pinned` | `bool` | 是否置顶 |
| `env` | `dict` | 环境变量（如 `CUDA_VISIBLE_DEVICES`） |
| `script` | `string` | 用户脚本的绝对路径 |
| `run_at` | `string?` | 首次运行时间 |
| `run_pid` | `int?` | 首次运行的进程 PID（结束后为 null） |
| `rerun_at` | `list[string]` | 每次重跑的时间 |
| `rerun_pid` | `list[int?]` | 每次重跑的 PID |
| `notes` | `string` | 用户笔记 |
| `monitor` | `list[dict]` | 监控数据条目列表 |

## ConfigNode — 点号访问配置

`pyruns.load()` 返回一个 `ConfigNode` 对象，支持属性风格的点号访问：

```python
config = pyruns.load()

# 基础类型
config.lr          # 0.001
config.epochs      # 10

# 嵌套结构
config.model.name  # "resnet50"
config.model.layers  # [64, 128, 256]

# 转回字典
config.to_dict()   # {"lr": 0.001, "epochs": 10, "model": {"name": "resnet50", ...}}
```

**支持的配置文件格式**：
- `.yaml` / `.yml`
- `.json`

## 类型推断

配置值从字符串输入自动推断类型（由 `parse_value()` 处理）：

| 输入 | 推断类型 | 结果 |
|------|----------|------|
| `42` | `int` | `42` |
| `3.14` | `float` | `3.14` |
| `true` / `True` | `bool` | `True` |
| `false` / `False` | `bool` | `False` |
| `[1, 2, 3]` | `list` | `[1, 2, 3]` |
| `hello` | `str` | `"hello"` |
| `None` | `NoneType` | `None` |

## 嵌套配置

YAML 的嵌套结构在 Pyruns 中完全支持：

```yaml
# config_default.yaml
model:
  name: resnet50
  hidden_size: 256
  dropout: 0.1

training:
  lr: 0.001
  epochs: 100
  scheduler:
    type: cosine
    warmup: 5
```

在 Generator 页面中，嵌套字典显示为可折叠的分组。

在脚本中使用：

```python
config = pyruns.load()
config.model.name          # "resnet50"
config.training.scheduler.type  # "cosine"
```

## 环境变量配置（Per-Task）

每个任务可以在 Task Dialog 的 **Env Vars** 标签页中设置独立的环境变量：

```json
{
    "CUDA_VISIBLE_DEVICES": "0,1",
    "MASTER_PORT": "29500",
    "OMP_NUM_THREADS": "4"
}
```

这些环境变量在任务执行时通过 `executor.py` 的 `_prepare_env()` 注入到子进程环境中。

## 配置优先级

`pyruns.read()` 的配置文件查找顺序：

```
1. 环境变量 PYRUNS_CONFIG（pyr 运行器自动设置）
   └── 指向任务目录下的 config.yaml

2. 显式传入的 file_path
   └── pyruns.read("my_config.yaml")

3. 默认路径
   └── _pyruns_/config_default.yaml
```

## UI 与环境全局配置 (`_pyruns_settings.yaml`)

为保证高度定制化并整洁分离项目逻辑，Pyruns 将配置层分为三部分：

1. **`_pyruns_settings.yaml` (Workspace UI 设置)**: 自动生成在工作区内的文件。用于用户自定义如 `ui_port`（端口）、`manager_max_workers`（并发进程）、页面列数等外部设置。
2. **`pyruns/_config.py` (底层常量)**: 硬编码了所有不会也不应该被用户修改的系统级变量（例如 `.trash` 回收站命名、内部的环境变量名称 `__PYRUNS_CONFIG__`）。
3. **`pyruns/ui/theme.py` (视觉系统)**: 所有 UI 的 Tailwind 样式、颜色映射（例如 `STATUS_ICONS`）。通过这种归类彻底杜绝散落的硬代码，实现界面的极度统一。

