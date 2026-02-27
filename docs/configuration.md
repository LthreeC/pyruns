# 配置系统

## 概述

Pyruns 的配置系统围绕 YAML 文件构建，支持嵌套结构、自动类型推断和批量参数展开。整个配置体系分为三个层次：

| 层次 | 文件 | 说明 |
|------|------|------|
| **参数模板** | `config_default.yaml` | 脚本的默认参数基准，由 `pyr` 自动生成或用户导入 |
| **任务快照** | `config.yaml` | 每次生成任务时的参数副本，保存在各任务目录下 |
| **任务元数据** | `task_info.json` | 任务的完整生命周期数据（状态、时间、PID、笔记等） |

---

## 配置文件

### `config_default.yaml` — 参数模板

存放在 `_pyruns_/{script}/` 目录下，是 Generator 页面的参数来源。

**生成方式（三选一）**：

| 方式 | 触发条件 | 行为 |
|------|----------|------|
| **Argparse 自动解析** | `pyr train.py`（脚本含 `argparse`） | AST 静态分析提取所有参数默认值并生成 |
| **YAML 导入** | `pyr train.py my_config.yaml` | 将 `my_config.yaml` 复制为 `config_default.yaml` |
| **pyruns.load() 模式** | 脚本使用 `pyruns.load()` | 要求 `config_default.yaml` 已存在，否则报错并引导用户导入 |

> 后续运行 `pyr train.py`（不带 YAML 参数）时，始终自动加载已有的 `config_default.yaml`。再次传入新 YAML 会覆盖旧模板。

```yaml
# Auto-generated for train.py

lr: 0.001  # 学习率
epochs: 10  # 训练轮数
batch_size: 32  # 批大小
model: resnet50
```

**在 Generator 中的行为**：
- 以只读模板形式加载（左上角 🔒 标记）
- 用户编辑的是内存副本，原始文件不受影响
- 生成任务时，参数写入各任务目录的独立 `config.yaml`

### `config.yaml` — 任务级快照

每个任务目录包含一个独立的 `config.yaml`，是生成任务时的参数快照：

```yaml
lr: 0.01
epochs: 20
batch_size: 64
model: resnet50
```

- 批量生成时，每个任务的 `config.yaml` 各自包含该任务的参数组合
- 内部元数据字段（以 `_meta` 开头的键）会被自动过滤

### `task_info.json` — 任务元数据

记录任务的完整生命周期数据：

```json
{
  "name": "task_2026-02-27_15-55-43_[1-of-5]",
  "status": "completed",
  "progress": 1.0,
  "created_at": "2026-02-27_15-55-44",
  "pinned": false,
  "script": "D:/path/to/script/main.py",
  "start_times": [
    "2026-02-27_15-55-48"
  ],
  "finish_times": [
    "2026-02-27_15-55-53"
  ],
  "pids": [
    22996
  ],
  "monitors": [
    {
      "last_loss": 1.0
    }
  ]
}
```

**字段说明**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `string` | 任务显示名称（与目录名一致） |
| `status` | `string` | 当前状态，如 `pending`, `queued`, `running`, `completed`, `failed` |
| `progress` | `float` | 进度 0.0 ~ 1.0 |
| `created_at` | `string` | 任务创建时间 |
| `pinned` | `bool` | 是否在 Manager 页面置顶 |
| `script` | `string` | 绑定的 Python 脚本绝对路径 |
| `start_times` | `list[string]` | 包含历次运行的启动时间记录 |
| `finish_times` | `list[string]` | 包含历次运行的结束时间记录 |
| `pids` | `list[int]` | 包含历次运行的进程 ID（运行结束后保留历史 PID 记录） |
| `notes` | `string` | （可选）用户填写的笔记内容 |
| `monitors` | `list[dict]` | （可选）记录的实时监控数据 |

# 核心配置系统详解

Pyruns 的设计原则之一是与原生脚本逻辑保持解耦。其配置系统覆盖了从**模板提取**到**环境注入**的完整流程。

---

## 一级机制：配置的生命周期

### 1. 默认原型（`config_default.yaml`）

该文件作为当前脚本的参数基准。

- **动态提取**：通过 `pyr train.py` 运行时，系统后台利用 AST 静态分析自动提取 `add_argument` 中的参数类型和缺省值，并将结果保存至工作区目录下的 `_pyruns_/{script}/config_default.yaml`。
- **显式导入**：通过 `pyr train.py my_base.yaml` 命令，系统将指定的外部 YAML 复制为工作区原型。

该模板作为 Generator 界面的表单渲染基础。

### 2. 参数快照（`config.yaml`）

当通过界面生成多个任务时，Pyruns 会为每个任务创建相互独立的专属目录，并在其中存入确切的配置副本：

```text
_pyruns_/
└── train/
    ├── config_default.yaml       # 整体参数模板
    └── tasks/
        ├── task_1/
        │   └── config.yaml       # task_1 的专属运行配置副本 
        ├── task_2/
        │   └── config.yaml
```

后续界面的重新渲染或全局模板的更改均不会影响已有任务快照中的数值，从而保证实验的可复现性。

---

## 二级机制：任务调度的注入机制

在点击任务的执行操作时，底层 Executor 不会通过命令行参数（如 `--lr 0.1`）来传递由于嵌套或数据类型导致的复杂超参数。 

实际流程是将任务目录中 `config.yaml` 的绝对路径注入到系统环境变量 `__PYRUNS_CONFIG__` 中，随后启动原脚本所在进程：

```python
# executor 层调度逻辑简化：
env["__PYRUNS_CONFIG__"] = "..._pyruns_/train/tasks/task_1/config.yaml"
subprocess.run(["python", "train.py"], env=env)
```

脚本启动并调用（或经由 `argparse` 代理被隐式调用） `pyruns.read()` 时：

#### 优先级决断：
1. 优先检测系统环境变量中是否存在 `__PYRUNS_CONFIG__`。
2. 若存在，则直接绑定并读取该变量指向的快照文件，忽略其他入参。
3. 参数被反序列化为 `ConfigNode` 对象供后续业务逻辑使用。

---

## 额外机制

### 嵌套字典深度支持

不要觉得 Pyruns 只能处理平铺的 `lr: 0.1` 这种单维字典，无论是你上传的 YAML 还是它自动生成的对象，它完美支持无论多深的嵌套：

```yaml
# config.yaml
model:
  vit:
    patch_size: 16
    layers:
      - 64
      - 128
```

在代码中，你依然可以使用魔法点号：

```python
config = pyruns.load()
print(config.model.vit.layers[1])  # 打印出 128
```

### 类型安全（隐式推导）

如果是在 Web UI 下手写 YAML，不需要给字符串加引号，底层使用了 PyYAML 的 SafeLoader 进行类型推断：

```yaml
learning_rate: 0.001   # float
epochs: 100            # int
use_pretrained: true   # bool (Python 的 True)
name: baseline         # str
```

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

在脚本中使用（Mode 2）：

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

## 配置查找优先级

`pyruns.read()` 按如下顺序查找配置文件：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | 环境变量 `__PYRUNS_CONFIG__` | 由 `pyr` executor 自动设置，指向当前任务的 `config.yaml` |
| 2 | 显式传入的 `file_path` | `pyruns.read("my_config.yaml")` |
| 3 | 默认路径 | `_pyruns_/{script}/config_default.yaml` |

**工作流说明**：
- 通过 `pyr` 运行任务时，executor 会自动设置 `__PYRUNS_CONFIG__` 指向任务目录的 `config.yaml`，用户脚本中的 `pyruns.read()` / `pyruns.load()` 会自动读取
- 直接 `python train.py` 运行时，按优先级 2→3 查找

---

## 全局设置 `_pyruns_settings.yaml`

Pyruns 的配置层明确分离：

| 文件 | 作用 | 修改方式 |
|------|------|----------|
| `_pyruns_settings.yaml` | 用户可定制的 UI 设置（端口、并发数、列数等） | 手动编辑或 UI 内修改 |
| `pyruns/_config.py` | 系统级常量（目录名、环境变量名等） | 仅开发者修改 |
| `pyruns/ui/theme.py` | 视觉系统（CSS 类名、颜色映射、图标） | 仅开发者修改 |

