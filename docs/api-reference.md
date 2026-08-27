# API 参考

本文分两部分：

- 用户脚本可直接调用的 Python API
- Web UI 使用的 FastAPI 接口概览

## Python API

```python
import pyruns
```

### `pyruns.read(file_path=None)`

读取配置文件并初始化全局配置对象。

查找顺序：

1. 环境变量 `__PYRUNS_CONFIG__`
2. 显式传入的 `file_path`
3. `_pyruns_/<script>/config_default.yaml`

适用场景：

- 配置任务
- 直接在脚本里手动加载 YAML / JSON

注意：

- shell 任务不会设置 `__PYRUNS_CONFIG__`

示例：

```python
import pyruns

pyruns.read()
cfg = pyruns.load()
```

### `pyruns.load()`

返回 OmegaConf 的 `DictConfig` 或 `ListConfig`。

示例：

```python
cfg = pyruns.load()
print(cfg.lr)
print(cfg.model.name)
```

需要转换为普通 Python 字典或列表时，使用 OmegaConf 官方 API：

```python
from omegaconf import OmegaConf

data = OmegaConf.to_container(cfg, resolve=True)
```

### `pyruns.record(data=None, **kwargs)`

把当前运行的一次记录写入 `task_info.json["records"]`。

示例：

```python
pyruns.record(loss=0.31, acc=91.2)
```

特点：

- 同一次运行会合并到当前 run slot
- 在非 Pyruns 环境下会静默返回
- 持久化失败会在任务日志中输出一次去重 warning，但不会终止用户实验

### `pyruns.track(key=None, value=None, **kwargs)`

把序列数据写入 `task_info.json["tracks"]`。

示例：

```python
pyruns.track(loss=0.8)
pyruns.track(loss=0.6)
pyruns.track("acc", 0.91)
```

### `pyruns.get_task_dir()`

返回当前任务目录；如果不在 Pyruns 环境里返回 `None`。

### `pyruns.get_run_index()`

返回当前运行槽位；如果不在 Pyruns 环境里返回 `None`。

### `pyruns.artifact_dir()`

返回当前 run 的文件输出目录，并自动创建目录。

目录固定为：

```text
<task_dir>/artifacts/runN
```

如果不在 Pyruns 任务环境里，则使用当前工作目录下的 `artifacts/run1`。

示例：

```python
import os

artifact_dir = pyruns.artifact_dir()
metrics_path = os.path.join(artifact_dir, "metrics.json")
with open(metrics_path, "w", encoding="utf-8") as f:
    f.write("{}")
```

## Web API 概览

这些接口是本机 Web UI 的内部控制面，不是公开远程 API。服务只监听 loopback；每次
`pyr ui` 启动会生成随机令牌，首个 tokenized URL 请求将其交换为 `HttpOnly`、
`SameSite=Strict` 的持久会话 cookie。该会话会在使用时续期，并在同一主机、端口和
工作区的 UI 更新或重启后继续有效。所有 `/api` HTTP 与 WebSocket 请求都必须携带该
会话。请求体、分页、批量任务数、环境变量数和日志读取量都有硬上限。

主入口：

- `GET /api/workspace`
- `POST /api/workspace/run-root`
- `POST /api/workspace/shell`

### Workspace

#### `GET /api/workspace`

返回当前工作区：

```json
{
  "run_root": ".../_pyruns_/main",
  "script_name": "main",
  "script_path": "D:/project/main.py",
  "workspace_kind": "script",
  "workspace_ready": true
}
```

#### `POST /api/workspace/shell`

切换并初始化 shell workspace。

返回：

```json
{
  "run_root": ".../_pyruns_/_shell_",
  "script_name": "_shell_",
  "script_path": "",
  "workspace_kind": "shell"
}
```

### Generator

#### `POST /api/generator/preview`

脚本工作区：

- `mode = "form"`：做 batch 预览
- `mode = "yaml"`：预览单任务

shell 工作区：

- `mode = "shell"`：预览单个 shell 任务

#### `POST /api/generator/create`

请求体字段：

- `name_prefix`
- `mode`
- `yaml_text`
- `shell_text`
- `template_value`
- `append_timestamp`

返回中包含：

- `count`
- `items`
- `task_kind`

### Tasks

#### `GET /api/tasks`

分页获取任务列表。

#### `GET /api/tasks/{task_name}`

获取单个任务详情。

#### `POST /api/tasks/{task_name}/run`

运行单个任务。

#### `POST /api/tasks/{task_name}/cancel`

停止单个任务。

#### `POST /api/tasks/{task_name}/pin`

pin / unpin 任务。

#### `PATCH /api/tasks/{task_name}/notes`

更新 notes。请求必须同时提交编辑时读到的 `expected_notes`：

```json
{
  "notes": "needs review",
  "expected_notes": ""
}
```

如果 notes 已被其他客户端修改，接口返回 `409 Conflict` 且不覆盖新内容。

#### `PATCH /api/tasks/{task_name}/env`

更新任务环境变量。请求必须同时提交编辑时读到的完整 `expected_env`：

```json
{
  "env": {"CUDA_VISIBLE_DEVICES": "0"},
  "expected_env": {}
}
```

如果环境变量已被其他客户端修改，接口返回 `409 Conflict` 且保留磁盘上的新内容。

#### `POST /api/tasks/{task_name}/rename`

重命名任务。

### Logs

#### `GET /api/tasks/{task_name}/logs`

读取某个日志文件的历史内容。

#### `WS /api/tasks/{task_name}/logs/stream`

订阅日志增量。

消息示例：

```json
{
  "type": "chunk",
  "task_name": "task_001",
  "content": "epoch 1 done\n"
}
```

### Launcher

- `GET /api/launcher/scripts`
- `GET /api/launcher/configs`
- `GET /api/launcher/workspaces`
- `POST /api/launcher/open`
- `POST /api/launcher/pick-script`

用于启动器中的脚本发现、配置发现和工作区打开。
