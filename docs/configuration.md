# 配置说明

这一版 Pyruns 需要从三个层面理解配置：

1. workspace 级配置
2. task 级配置
3. shell runtime 配置

## 1. 核心概念

### `workspace_kind`

表示当前打开的工作区是什么：

- `script`
- `shell`

### `task_kind`

表示任务实际保存和执行的内容是什么：

- `config`
- `shell`

这两个概念不要混淆：

- `workspace_kind` 影响页面能力和默认编辑模式
- `task_kind` 影响任务文件结构和执行方式

## 2. 工作区目录结构

### Script Workspace

```text
_pyruns_/
├─ _pyruns_settings.yaml
└─ main/
   ├─ script_info.json
   ├─ config_default.yaml
   └─ tasks/
```

### Shell Workspace

```text
_pyruns_/
├─ _pyruns_settings.yaml
└─ _shell_/
   ├─ script_info.json
   └─ tasks/
```

## 3. 任务目录结构

### config 任务

```text
tasks/<task_name>/
├─ task_info.json
├─ config.yaml
└─ run_logs/
   ├─ run1.log
   ├─ run2.log
   └─ error.log
```

### shell 任务

```text
tasks/<task_name>/
├─ task_info.json
├─ config.sh
└─ run_logs/
   ├─ run1.log
   ├─ run2.log
   └─ error.log
```

注意：

- Windows 下也仍然落盘为 `config.sh`
- 变化的是执行策略，不是任务模型

## 4. `script_info.json`

### Script Workspace 示例

```json
{
  "workspace_kind": "script",
  "script_name": "train",
  "script_path": "D:/project/train.py"
}
```

### Shell Workspace 示例

```json
{
  "workspace_kind": "shell",
  "script_name": "_shell_",
  "script_path": ""
}
```

## 5. `task_info.json`

无论是 config 任务还是 shell 任务，都会有统一的生命周期元信息。

典型字段：

```json
{
  "name": "task_001",
  "status": "pending",
  "progress": 0.0,
  "created_at": "2026-03-19_12-00-00",
  "task_kind": "config",
  "config_file": "config.yaml",
  "start_times": [],
  "finish_times": [],
  "pids": [],
  "records": [],
  "tracks": []
}
```

对于 shell 任务：

```json
{
  "task_kind": "shell",
  "config_file": "config.sh"
}
```

## 6. `_pyruns_settings.yaml`

位置：

```text
<project>/_pyruns_/_pyruns_settings.yaml
```

当前默认模板大致包含这些键：

```yaml
ui_port: 8099
header_refresh_interval: 3

generator_form_columns: 4
generator_auto_timestamp: true
generator_mode: form

manager_columns: 5
manager_max_workers: 1
manager_execution_mode: thread
ui_page_size: 50

monitor_chunk_size: 50000
monitor_scrollback: 100000
monitor_sidebar_width_pct: 24

log_enabled: false
log_level: INFO

shell_mode: follow
shell_executable: ""
```

## 7. 重点配置项解释

### `header_refresh_interval`

- 控制 Dashboard/Header 的轮询刷新频率
- 单位秒
- 最小值实际会被钳制到 `1`

### `monitor_sidebar_width_pct`

- 控制 Monitor 左侧任务栏宽度百分比
- 当前前端会把它限制在 `18 ~ 36`

### `manager_execution_mode`

- `thread`
- `process`

主要影响批量运行时的调度方式。

### `shell_mode`

可选值：

- `follow`
- `custom`

语义：

- `follow`：默认，跟随启动 `pyr` 的当前终端
- `custom`：显式指定 `shell_executable`

### `shell_executable`

只有在 `shell_mode: custom` 时才会生效。

## 8. shell 任务的执行约束

当前 shell 任务的执行规则：

- 任务正文直接来自 `config.sh`
- 不读取 `config.yaml`
- 不注入 `__PYRUNS_CONFIG__`
- 继续继承当前 Python 进程环境

这意味着 shell 任务更接近：

> “在启动 `pyr` 的那个终端里，再执行一次同样的命令文本”

### Windows

- 默认跟随 PowerShell 或 cmd
- 会用原生 wrapper 把 `config.sh` 内容交给对应终端执行
- 不做 bash 语法模拟

### Linux / macOS

- 默认跟随当前 shell
- 直接按当前 shell 语义执行

## 9. config 任务的执行约束

config 任务仍然是 Python 任务主链路：

- 每个任务有自己的 `config.yaml`
- executor 会注入 `__PYRUNS_CONFIG__`
- `pyruns.load()` / `pyruns.read()` 读取的是该任务自己的配置文件

## 10. 搜索与过滤

Manager 和 Monitor 的搜索框支持多行输入。

语义是：

- 每一行是一个 term
- 多行之间按 AND 匹配
- 会在任务名、preview、config_text、search_text、notes 等字段中做包含判断

这个行为和后端 `filter_tasks` 一致。
