# 架构说明

Pyruns 当前的核心设计其实可以浓缩成一句话：

> React 前端负责把体验做顺，FastAPI 运行时负责把状态串起来，core 层负责把任务真正落到磁盘并执行，磁盘才是最终真实来源。

这不是一个“先有页面、再去猜状态”的系统。  
它是一个“先把任务结构稳稳落下，再让 UI 把它看清楚”的系统。

## 1. 整体结构

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

前端交付链路则是：

```text
frontend/  --build-->  pyruns/web/static/
```

## 2. 这套分层为什么顺

### `frontend/`

负责：

- 页面和交互
- Zustand 状态管理
- 轮询与 websocket
- xterm.js / CodeMirror 等展示层

关键页面：

- `dashboard`
- `generator`
- `manager`
- `monitor`

这层的重点是体验，不负责伪造真实状态。

### `pyruns/web/`

负责：

- FastAPI app 组装
- API 路由
- 当前 workspace runtime
- 聚合 dashboard、task list、task logs、workspace info

关键文件：

- `pyruns/web/app.py`
- `pyruns/web/runtime.py`

其中 `runtime.py` 是 Web 层最重要的状态枢纽之一，负责把“当前打开的是哪个工作区、当前使用什么 shell runtime、当前 tasks 目录在哪里”这些运行时信息统一组织起来。

### `pyruns/core/`

这是 Pyruns 真正干活的地方。

#### `task_generator.py`

负责把“用户想生成什么任务”真正写到磁盘：

- config task 写 `config.yaml`
- shell task 写 `config.sh`
- 同时生成 `task_info.json`

#### `task_manager.py`

负责任务管理：

- 扫描磁盘任务
- 刷新任务状态
- 批量运行、删除、pin
- 让内存视图始终跟磁盘文件对齐

#### `executor.py`

负责实际执行任务：

- 根据 `task_kind` 选择命令构造方式
- config task 注入 `__PYRUNS_CONFIG__`
- shell task 解析 shell runtime
- 采集 stdout / stderr 到 `run_logs`
- 把实时日志推给 Monitor websocket

#### `system_metrics.py`

负责系统指标：

- CPU
- RAM
- NVIDIA GPU 利用率
- 显存占用 / 总量
- GPU 进程明细

### `pyruns/utils/`

这一层不高调，但很关键。它让整个系统不用数据库也能稳定工作。

#### `settings.py`

- 读写 `_pyruns_settings.yaml`
- 合并默认值
- 提供 save / load / get

#### `shell_runtime.py`

- 解析 `shell_mode`
- 区分 `follow` / `custom`
- 检测当前启动终端
- 给 executor、runtime、frontend 提供统一 shell runtime 信息

#### `task_files.py`

- 统一 task payload 文件读写
- 归一化 `workspace_kind` / `task_kind`

#### `info_io.py`

- 读写 `task_info.json`
- 读写 `script_info.json`
- 解析日志路径

#### `log_io.py`

- 读取日志文件
- 处理编码回退
- 标准化终端换行

#### `sort_utils.py`

- 搜索和排序逻辑
- Manager / Monitor 多行搜索的 AND 语义

## 3. 磁盘是最终状态源

这是 Pyruns 很重要的设计原则。

前端状态和运行时内存都只是视图。  
真正能被信任、能被恢复、能被共享的是磁盘上的这些文件：

- `task_info.json`
- `config.yaml` / `config.sh`
- `run_logs/`

这带来的好处很直接：

- 页面刷新后状态可恢复
- 不依赖数据库
- CLI / Web UI 可以共用同一套任务状态
- 任务本身很容易检查、备份、迁移

## 4. 两条工作流

### Script Workspace 工作流

```text
pyr train.py
  → bootstrap_workspace(...)
  → _pyruns_/train/
  → Generator(form / yaml)
  → TaskGenerator.create_tasks(..., task_kind="config")
  → tasks/<task_name>/config.yaml
  → executor injects __PYRUNS_CONFIG__
```

### Shell Workspace 工作流

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

Pyruns 对 shell 模式的理解不是“模拟 bash”，而是：

> 尽量跟随你启动 `pyr` 的原终端

默认值：

- `shell_mode = follow`

只有显式切到：

- `shell_mode = custom`

才会启用 `shell_executable`。

### Windows

- 跟随 PowerShell：按 PowerShell 语义执行
- 跟随 cmd：按 cmd 语义执行
- wrapper 只负责把任务文本交给原生终端
- 不做跨 shell 翻译

### Linux / macOS

- 默认跟随当前 shell
- 强调 follow，而不是“bash 优先”

## 6. 前端日志流为什么稳定

Monitor 页面依赖两种数据源：

### 初始历史加载

- `GET /api/tasks/{name}/logs`

### 运行中增量流

- `WS /api/tasks/{name}/logs/stream`

当前实现重点包括：

- 切任务时会重置日志选择状态
- websocket chunk 会校验任务身份，避免串到错误任务
- 历史日志会做终端换行标准化，避免 xterm 出现“逐行右移”的假缩进

## 7. UI 为什么适合做 GitHub Pages 展示

现在的 React UI 已经足够完整，完全可以做一个静态展示版：

- Dashboard 展示假系统指标
- Generator 展示表单 / YAML / Shell 三种态
- Manager 展示任务卡片和详情抽屉
- Monitor 展示模拟日志流和终端界面

也就是说，GitHub Pages 很适合承载：

- 文档站
- 截图展示
- mock 数据驱动的静态 UI demo

不适合承载的部分则是：

- 真正执行任务
- 实时 FastAPI runtime
- 真实任务调度

## 8. 推荐补图位置

如果你想让这一页更有展示感，最适合补的图是：

- 一张整体架构流程图
- 一张前端到 `pyruns/web/static/` 的交付链路图
- 一张 shell follow/custom 语义的流程示意图

如果后面要补图，建议统一放在：

- `/docs/assets/architecture_overview.png`
- `/docs/assets/build_pipeline.png`
- `/docs/assets/shell_runtime_flow.png`
