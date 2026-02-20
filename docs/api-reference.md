# API 参考

## 公开 API（`pyruns` 包）

以下函数可以在用户脚本中直接调用：

```python
import pyruns
```

---

### `pyruns.read(file_path=None)`

读取配置文件，初始化全局 ConfigManager。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file_path` | `str?` | `None` | 配置文件路径（YAML 或 JSON） |

**配置查找优先级**：

1. 环境变量 `PYRUNS_CONFIG`（由 `pyr` 运行器自动设置）
2. 显式传入的 `file_path`
3. `_pyruns_/config_default.yaml`

**示例**：

```python
pyruns.read()                          # 自动查找
pyruns.read("configs/experiment.yaml") # 指定路径
```

**异常**：
- `FileNotFoundError`：配置文件不存在
- `RuntimeError`：配置文件解析失败

---

### `pyruns.load()`

返回已加载的配置对象。必须先调用 `pyruns.read()`。

**返回值**：`ConfigNode` | `list[ConfigNode]` — 支持点号属性访问的配置对象。

**示例**：

```python
pyruns.read()
config = pyruns.load()

# 点号访问
print(config.lr)               # 0.001
print(config.model.name)       # "resnet50"

# 列表类型
print(config.layers)           # [64, 128, 256]

# 转回字典
d = config.to_dict()
```

**异常**：
- `RuntimeError`：未调用 `read()` 就调用 `load()`

---

### `pyruns.add_monitor(data=None, **kwargs)`

向当前任务的 `task_info.json` 追加一条监控数据。

**参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `data` | `dict?` | 字典形式的监控数据 |
| `**kwargs` | | 关键字参数形式的监控数据 |

**行为**：

- 数据按 **Run** 聚合。同一次 Run 内的多次调用会合并到同一个字典中
- 数据追加到 `task_info.json` 的 `"monitors"` 列表中
- 如果不在 `pyr` 管理的任务中运行（无 `PYRUNS_CONFIG` 环境变量），调用被静默忽略
- 写入失败时自动重试最多 5 次

**示例**：

```python
# 关键字参数（推荐）
pyruns.add_monitor(epoch=10, loss=0.234, acc=95.2)

# 字典参数
pyruns.add_monitor({"metric_a": 1.0, "metric_b": 2.0})

# 混合使用
pyruns.add_monitor({"base_loss": 0.5}, epoch=10, lr=0.001)
```

**写入的 JSON 结构**：

```json
{
    "monitors": [
        {
            "epoch": 10,
            "loss": 0.234,
            "acc": 95.2
        }
    ]
}
```

**异常**：
- `TypeError`：`data` 参数不是 `dict` 类型

---

## ConfigNode

`pyruns.load()` 返回的配置对象类型。

### 属性访问

```python
node = ConfigNode({"a": 1, "b": {"c": 2}})
node.a      # 1
node.b.c    # 2
```

### `to_dict() → dict`

递归转换回普通 Python 字典。

```python
config.to_dict()  # {"a": 1, "b": {"c": 2}}
```

### `__repr__()`

仿 argparse 风格的字符串表示：

```python
repr(config)  # "ConfigNode(a=1, b=ConfigNode(c=2))"
```

---

## 工具函数

### `pyruns.utils.config_utils`

#### `generate_batch_configs(base_config) → list[dict]`

根据 Product / Zip 语法生成多个配置组合。详见 [批量生成语法](batch-syntax.md)。

#### `count_batch_configs(base_config) → int`

预览将生成的配置数量（不实际生成）。

#### `load_yaml(path) → dict`

安全加载 YAML 文件，异常时返回空字典。

#### `save_yaml(path, data)`

将字典保存为 YAML 文件。

#### `parse_value(val_str) → Any`

将字符串输入解析为 Python 原生类型。

#### `flatten_dict(d, sep='.') → dict`

将嵌套字典展平为点号分隔的单层字典。

```python
flatten_dict({"a": {"b": 1, "c": 2}})
# {"a.b": 1, "a.c": 2}
```

#### `unflatten_dict(d, sep='.') → dict`

将展平的字典还原为嵌套字典。

---

### `pyruns.utils.task_io`

#### `load_task_info(task_dir) → dict`

从任务目录加载 `task_info.json`。

#### `save_task_info(task_dir, info)`

将字典保存到任务目录的 `task_info.json`。

#### `load_monitor_data(task_dir) → list[dict]`

从 `task_info.json` 中提取 `"monitor"` 字段。

#### `get_log_options(task_dir) → dict[str, str]`

返回 `{显示名: 文件路径}` 映射，包含 `run_logs/` 目录下所有 `runN.log` 文件。

#### `resolve_log_path(task_dir, log_file_name=None) → str?`

解析日志文件的完整路径。未指定名称时返回第一个可用的日志。

---

### `pyruns.utils.parse_utils`

#### `detect_config_source_fast(filepath) → tuple[str, str?]`

用正则快速检测脚本的配置来源：

```python
("argparse", None)           # 使用 argparse
("pyruns_read", "config.yaml")  # 使用 pyruns.read("config.yaml")
("unknown", None)            # 无法检测
```

#### `extract_argparse_params(filepath) → dict[str, dict]`

通过 AST 解析提取 `add_argument()` 调用的所有参数信息。当同时提供短参数和长参数名时（如 `-b, --batch_size`），会优先使用长参数名作为字典的键。

```python
{
    "lr": {"name": "--lr", "type": float, "default": 0.001, "help": "学习率"},
    "epochs": {"name": "--epochs", "type": int, "default": 10},
    "batch_size": {"name": "--batch_size", "type": int, "default": 32, "help": "批大小"},
}
```

---

### `pyruns.utils.ansi_utils`

#### `ansi_to_html(text) → str`

将 ANSI 转义码转换为 HTML `<span>` 标签。支持标准 8/16 色、粗体、斜体、下划线。

#### `tail_lines(text, n=1000) → str`

返回文本的最后 n 行。

---

### `pyruns.utils.process_utils`

#### `is_pid_running(pid) → bool`

跨平台检测进程是否仍在运行。Windows 使用 `kernel32.OpenProcess`，Unix 使用 `os.kill(pid, 0)`。

#### `kill_process(pid)`

跨平台终止进程树。Windows 使用 `taskkill /F /T /PID`，Unix 使用 `os.kill(SIGTERM)`。

