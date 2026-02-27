# API 参考

## 公开 API

用户脚本中可直接调用以下函数：

```python
import pyruns
```

---

### `pyruns.read(file_path=None)`

读取配置文件并初始化全局 ConfigManager。

**参数**：

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `file_path` | `str?` | `None` | 配置文件路径（YAML 或 JSON） |

**查找优先级**：

| 优先级 | 来源 | 说明 |
|--------|------|------|
| 1 | 环境变量 `__PYRUNS_CONFIG__` | 由 `pyr` executor 自动设置，绝对指向任务的 `config.yaml` |
| 2 | 显式传入的 `file_path` | 直接读取传给函数的绝对或相对路径 |
| 3 | 默认相对路径 | 尝试读取工作区下 `_pyruns_/{script}/config_default.yaml` 作为默认值 |

**示例**：

```python
# 1. 在 Pyruns 环境下，将完全自动查找 __PYRUNS_CONFIG__
pyruns.read()

# 2. 手动指定路径（当脱离 Pyruns 界面，手动调参测试时）
pyruns.read("configs/experiment.yaml") 
```

**异常**：配置文件不存在时抛出 `FileNotFoundError`；解析失败时抛出 `RuntimeError`。

---

### `pyruns.load()`

返回已加载的配置对象。在以 `pyr` 启动的环境下会自动执行加载逻辑并绑定当前任务唯一确定的配置，无需手动调用 `read()`。

> 使用 `pyruns.load()` 的脚本需要确保 `_pyruns_/{script}/config_default.yaml` 存在作为基底模板。可通过 `pyr script.py your_config.yaml` 一次性导入生成模板，后续脱离命令行直接 `pyr script.py` 即可。

**返回值**：`ConfigNode` | `list[ConfigNode]` — 支持点号属性访问的配置树。

**示例**：

```python
# 无需 read()
config = pyruns.load()

# 点号访问
print(config.lr)               # 0.001
print(config.model.name)       # "resnet50"

# 列表切片访问
print(config.layers)           # [64, 128, 256]

# 转回字典
d = config.to_dict()
```

**异常**：未调用 `read()` 且无法自动定位到 `__PYRUNS_CONFIG__` 或是 `config_default.yaml` 时抛出 `RuntimeError`。

---

### `pyruns.add_monitor(data=None, **kwargs)`

向当前任务的 `task_info.json` 运行时数据中追加监控日志。常用于在训练或评估阶段记录关键指标，后续可在界面 Monitor 选项卡跨任务聚合交叉对比并导出报表。

**参数**：

| 参数 | 类型 | 说明 |
|------|------|------|
| `data` | `dict?` | 字典形式的监控指标集合 |
| `**kwargs` | | 关键字形式传入的指标 |

**行为**：

- 同一次运行进程内的多次调用将被记录为含有时间戳的独立条目。
- 数据安全地追加到对应任务 `task_info.json` 的 `"monitors"` 列表属性中。
- 不在 `pyr` 接管的任务中运行时（即环境变量未设置 `__PYRUNS_CONFIG__` 时），函数调用被**静默忽略**，确保业务源码脱离 Pyruns 环境运行不报错。
- 提供并发写保护（默认配置遇互斥锁最多透明退避重试 5 次）。

**示例**：

```python
import pyruns

# 1. 记录最终指标
pyruns.add_monitor(loss=0.234, acc=95.2)

# 2. 混合字典与关键字使用
pyruns.add_monitor({"base_loss": 0.5}, final_acc=98.1)
```

**写入的后端结构**：

```json
{
    "monitors": [
        {
            "loss": 0.234,
            "acc": 95.2
        }
    ]
}
```

**异常**：当 `data` 非 `dict` 类型且不为 `None` 时抛出 `TypeError`。

---

## ConfigNode 

`pyruns.load()` 的返回值类型，提供字典封装的安全配置树对象。

### 属性访问

```python
node = ConfigNode({"a": 1, "b": {"c": 2}})
node.a      # 1
node.b.c    # 2
```

### `to_dict() → dict`

将内部包装的所有嵌套节点全部还原为原生 Python 字典实例。

```python
config.to_dict()  # {"a": 1, "b": {"c": 2}}
```

### `__repr__()`

重写格式化输出，提供基于多行的易读控制台回显信息格式。

---

## 工具函数库

所有位于后台运行的核心实现被封装于下层 `pyruns.utils` 库。

### `pyruns.utils.config_utils`

#### `generate_batch_configs(base_config) → list[dict]`

解析传入配置中包含的 Product (`|`) / Zip (`(|)`) 以及跨步数字等特殊语法格式，并根据规则展开生成所有合法的参数组合字典。

#### `count_batch_configs(base_config) → int`

计算基于语法所将展开的组合排练总数，而不执行具体的对象生成（当包含语法错误或数目不匹配的组合时安全返回 0）。

#### `flatten_dict(d, sep='.') → dict`

将多层嵌套字典展开为平级的一层字典，键以指定分隔符连接。

### `pyruns.utils.info_io`

负责与工作区结构进行持久化状态交互的高级 API：

#### `load_task_info(task_dir) → dict`

基于指定任务目录，安全读取 `task_info.json` 的完整状态机记录。

#### `save_task_info(task_dir, info)`

执行底层的线程/进程安全回写锁校验，将最新状态数据覆盖落地到本地盘。

---

### `pyruns.utils.parse_utils`

#### `extract_argparse_params(filepath) → dict[str, dict]`

Pyruns 实现非侵入式解析的核心。通过调用标准的 Python `ast` 抽象语法树遍历器，静态逆向提取目标源码中包含的 `add_argument()` 定义及其附属信息。

```python
# AST逆向结果
{
    "lr": {"name": "--lr", "type": float, "default": 0.001, "help": "学习率"},
    "epochs": {"name": "--epochs", "type": int, "default": 10},
    "batch_size": {"name": "--batch_size", "type": int, "default": 32, "help": "批大小"},
}
```

---

### `pyruns.utils.process_utils`

#### `is_pid_running(pid) → bool`

提供跨操作系统的进程可达性探测方法（Windows 环境调用 `kernel32.OpenProcess`，Unix 环境基于 `os.kill(pid, 0)` 测试联通探测）。

#### `kill_process(pid)`

向指定的进程发停止信号关闭释放资源（Windows 系统默认基于 `taskkill /F /T /PID` 断开进程树，Unix 依赖抛发 `os.kill(SIGTERM)` 等手段退出）。

