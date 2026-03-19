# 架构说明

当前 Pyruns 的主链路可以概括成一句话：

> React 前端驱动 FastAPI 运行时，运行时调用 core 层管理磁盘工作区，所有任务状态最终以磁盘文件为准。

## 1. 总体结构

```text
CLI / Launcher
    ↓
FastAPI Runtime            (pyruns/web)
    ↓
Core Services              (pyruns/core)
    ↓
Utils / File IO / Settings (pyruns/utils)
    ↓
Workspace on disk          (_pyruns_/...)
```

前端构建链路：

```text
frontend/  --build-->  pyruns/web/static/
```

## 2. 分层职责

## `frontend/`

负责：

- 页面和交互
- Zustand 状态管理
- 轮询与 websocket
- xterm.js / CodeMirror 等前端展示层

关键页面：

- `dashboard`
- `generator`
- `manager`
- `monitor`

## `pyruns/web/`

负责：

- FastAPI app 组装
- API 路由
- 当前 workspace runtime
- 把前端请求映射到 core 层

关键文件：

- `pyruns/web/app.py`
- `pyruns/web/runtime.py`

### `runtime.py`

这是 Web 层最重要的状态中枢之一，负责：

- 当前 `root_dir`
- 当前 `tasks_dir`
- 当前 settings
- `TaskManager` / `TaskGenerator` / `SystemMonitor` 的懒加载
- dashboard、task list、task logs、workspace info 等 API 数据聚合

## `pyruns/core/`

负责真正的业务逻辑。

### `task_generator.py`

负责把“用户要创建什么任务”落到磁盘：

- config task 写 `config.yaml`
- shell task 写 `config.sh`
- 同时生成 `task_info.json`

### `task_manager.py`

负责任务管理：

- 扫描磁盘任务
- 刷新任务状态
- 排队、批量运行、删除、重命名、pin
- 把任务生命周期保持和磁盘文件同步

### `executor.py`

负责执行任务：

- 根据 `task_kind` 选择命令构造方式
- config task 注入 `__PYRUNS_CONFIG__`
- shell task 解析当前 shell runtime
- 采集 stdout/stderr 到 `run_logs/runN.log`
- 通过事件总线把实时日志推给 Monitor websocket

### `system_metrics.py`

负责系统指标：

- CPU
- RAM
- NVIDIA GPU 利用率、显存占用
- GPU 进程明细

## `pyruns/utils/`

负责偏底层但可复用的能力。

### `settings.py`

- 读写 `_pyruns_settings.yaml`
- 合并默认值
- 暴露 save/load/get

### `shell_runtime.py`

- 解析 `shell_mode`
- 检测 follow/custom
- 检测当前启动终端
- 给 executor / frontend / runtime 提供统一 shell runtime 信息

### `task_files.py`

- 统一 task payload 文件的读写入口
- 归一化 `workspace_kind` / `task_kind`

### `info_io.py`

- `task_info.json`
- `script_info.json`
- log 文件路径解析

### `log_io.py`

- 日志文件读取
- 编码回退
- 终端换行标准化

### `sort_utils.py`

- 搜索和排序逻辑
- Manager / Monitor 多行搜索的 AND 语义

## 3. 磁盘是真实状态源

这是 Pyruns 很重要的设计原则。

前端和内存态都只是运行时视图，真正的任务状态最终落在磁盘：

- `task_info.json`
- `config.yaml` / `config.sh`
- `run_logs/`

这么做的好处：

- 页面刷新后仍然可恢复
- 不依赖数据库
- CLI / Web UI 共用同一套任务状态

## 4. 两条工作流

## Script Workspace 工作流

```text
pyr train.py
  → bootstrap_workspace(...)
  → _pyruns_/train/
  → Generator(form / yaml)
  → TaskGenerator.create_tasks(..., task_kind="config")
  → tasks/<task_name>/config.yaml
  → executor injects __PYRUNS_CONFIG__
```

## Shell Workspace 工作流

```text
Open Shell Mode
  → _pyruns_/_shell_/
  → Generator(shell)
  → TaskGenerator.create_shell_task(...)
  → tasks/<task_name>/config.sh
  → executor resolves shell runtime
  → shell task runs with follow/custom semantics
```

## 5. shell 执行策略

当前 shell 执行策略的目标不是“模拟 bash”，而是“尽量跟随你启动 `pyr` 的原终端”。

默认：

- `shell_mode = follow`

只有显式改成：

- `shell_mode = custom`

才会启用 `shell_executable`。

### Windows

- 跟随 PowerShell：按 PowerShell 语义执行
- 跟随 cmd：按 cmd 语义执行
- wrapper 的职责只是把任务文本交给原生终端
- 不做跨 shell 翻译

### Linux / macOS

- 默认跟随当前 shell
- 强调 follow，而不是“bash 优先”

## 6. 前端日志流

Monitor 页面依赖两种数据源：

### 初始历史加载

- `GET /api/tasks/{name}/logs`
- 读取历史日志内容

### 运行中增量流

- `WS /api/tasks/{name}/logs/stream`
- executor 通过 `log_emitter` 推送增量 chunk

当前实现重点：

- 切任务时会重置 store 的日志选择状态
- websocket chunk 会校验任务身份，避免串到错误任务
- 历史日志读取会标准化终端换行，避免 xterm 出现“逐行右移”的假缩进

## 7. UI 交付路径

当前 UI 交付链路是：

```text
frontend/
  └─ build
      └─ pyruns/web/static/
```

FastAPI 会直接服务 `pyruns/web/static/` 里的构建产物，因此前端改动最终都应该落回这条链路。
