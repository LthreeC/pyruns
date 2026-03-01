# 架构设计

## 总体设计

Pyruns 采用三层分离架构，核心逻辑与 UI 框架完全解耦：

```
┌─────────────────────────────────────────────────────┐
│  CLI (cli.py)                                       │
│  入口层：解析命令行参数、检测脚本类型、启动 UI       │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  UI Layer (ui/)                                     │
│  展示层：NiceGUI 页面渲染、用户交互、轮询刷新        │
│  ┌───────────┐ ┌────────────┐ ┌──────────────┐     │
│  │  pages/   │ │ components/│ │  widgets.py  │     │
│  │ 3 个标签页 │ │  可复用组件  │ │  原子级小部件 │     │
│  └───────────┘ └────────────┘ └──────────────┘     │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  Core Layer (core/)                                 │
│  业务层：任务管理、执行调度、系统指标、报告导出        │
│  无任何 UI 框架依赖，可独立测试                      │
└───────────────────────┬─────────────────────────────┘
                        │
┌───────────────────────▼─────────────────────────────┐
│  Utils Layer (utils/)                               │
│  工具层：纯函数，无状态，无 UI 依赖                  │
│  YAML 解析、ANSI 转换、进程管理、文件 I/O            │
└─────────────────────────────────────────────────────┘
```

## 目录结构

```
pyruns/
├── __init__.py              # 公开 API: read(), load(), add_monitor()
├── _config.py               # 全局常量与默认值
├── cli.py                   # CLI 入口: `pyr <script.py>`
│
├── core/                    # ── 核心业务逻辑 ──
│   ├── config_manager.py    # ConfigNode + ConfigManager
│   ├── executor.py          # run_task_worker: 子进程执行
│   ├── report.py            # CSV/JSON 导出构建
│   ├── system_metrics.py    # SystemMonitor: CPU/RAM/GPU 采样
│   ├── task_generator.py    # TaskGenerator: 创建任务目录
│   └── task_manager.py      # TaskManager: 扫描/调度/CRUD
│
├── ui/                      # ── NiceGUI Web 界面 ──
│   ├── app.py               # 应用引导 + 路由
│   ├── layout.py            # 主布局: 页头 + 侧栏 + 内容区
│   ├── state.py             # AppState 数据类
│   ├── theme.py             # 统一色彩/样式常量
│   ├── widgets.py           # 原子级 UI 组件
│   ├── components/          # 可复用的较大 UI 组件
│   │   ├── batch_dialog.py
│   │   ├── env_editor.py
│   │   ├── export_dialog.py
│   │   ├── header.py
│   │   ├── param_editor.py
│   │   ├── sidebar.py
│   │   ├── task_card.py
│   │   └── task_dialog.py
│   └── pages/               # 页面级渲染器
│       ├── generator.py
│       ├── manager.py
│       └── monitor.py
│
└── utils/                   # ── 纯工具函数 ──
    ├── batch_utils.py
    ├── config_utils.py
    ├── events.py
    ├── info_io.py
    ├── log_io.py
    ├── log_utils.py
    ├── parse_utils.py
    ├── process_utils.py
    ├── settings.py
    ├── sort_utils.py
    └── time_utils.py
```

## 核心模块详解

### `_config.py` — 全局配置常量

所有硬编码的常量集中在此文件，包括：

| 常量                        | 值 | 用途 |
|---------------------------|-----|------|
| `ENV_KEY_ROOT`            | `"__PYRUNS_ROOT__"` | 环境变量名：任务根目录 |
| `ENV_KEY_CONFIG`          | `"__PYRUNS_CONFIG__"` | 环境变量名：当前任务配置路径 |
| `ENV_KEY_SCRIPT`          | `"__PYRUNS_SCRIPT__"` | 环境变量名：用户脚本路径 |
| `DEFAULT_ROOT_NAME`       | `"_pyruns_"` | 默认任务存储目录名 |
| `ROOT_DIR`                | 动态计算 | 运行时的任务根目录路径 |
| `TASK_INFO_FILENAME`      | `"task_info.json"` | 任务元数据文件名 |
| `CONFIG_FILENAME`         | `"config.yaml"` | 任务配置快照文件名 |
| `CONFIG_DEFAULT_FILENAME` | `"config_default.yaml"` | 参数模板文件名 |
| `RUN_LOG_DIR`             | `"run_logs"` | 统一日志目录：run1.log, run2.log, … |
| `TRASH_DIR`               | `".trash"` | 软删除目录名 |
| `MONITOR_KEY`             | `"monitors"` | task_info.json 中监控数据的字段名 |

### `core/task_manager.py` — 任务管理器

`TaskManager` 是整个系统的核心，负责：

```
┌─────────────────────────────────────────────────────┐
│                     TaskManager                      │
│                                                      │
│  scan_disk()          全量扫描 root_dir，重建内存    │
│  refresh_from_disk()  仅刷新 running/queued 任务状态  │
│  add_task(s)()        添加新任务到内存列表            │
│  start_batch_tasks()  批量将任务标记为 queued         │
│  rerun_task()         重跑已完成/失败的任务           │
│  cancel_task()        取消运行中的任务（杀进程）      │
│  delete_task()        软删除（移至 .trash）           │
│                                                      │
│  _scheduler_loop()    后台线程，持续拉取 queued 任务  │
│  _ensure_executor()   按需创建 Thread/Process Pool   │
│  _on_task_done()      任务完成后同步状态到内存        │
└─────────────────────────────────────────────────────┘
```

**线程安全设计**：

- `self._lock`：保护 `self.tasks` 列表的并发访问
- `self._executor_lock`：保护执行器池的创建和销毁
- `self._running_ids`：追踪当前正在执行器中运行的任务 ID

**任务状态机**：

```
pending ──→ queued ──→ running ──→ completed
    │          │          │
    │          │          └──→ failed
    │          └──→ failed (cancel)
    │
    └──→ (delete → .trash)

completed/failed ──→ queued (rerun)
```

### `core/executor.py` — 任务执行器

`run_task_worker()` 在独立的线程/进程中运行：

1. 从 `task_info.json` 读取任务元数据（脚本路径、工作目录）
2. 构建命令行：`python script.py --arg1 val1 --arg2 val2`
3. 设置环境变量（`PYRUNS_CONFIG`、`PYTHONIOENCODING` 等）
4. 通过 `subprocess.Popen` 启动子进程
5. 将 stdout/stderr 重定向到 `run_logs/runN.log`
6. 等待进程结束，更新 `task_info.json` 中的状态

**重跑机制**：重跑日志存储在 `run_logs/runN.log`（N 从 1 递增），支持多次重跑。

### `core/system_metrics.py` — 系统监控

`SystemMonitor.sample()` 返回：

```python
{
    "cpu_percent": 45.2,    # psutil.cpu_percent()
    "mem_percent": 68.1,    # psutil.virtual_memory().percent
    "gpus": [               # nvidia-smi --query-gpu=...
        {"index": 0, "util": 85.0, "mem_used": 8192.0, "mem_total": 24576.0},
        {"index": 1, "util": 12.0, "mem_used": 1024.0, "mem_total": 24576.0},
    ]
}
```

GPU 数据有 0.5 秒超时保护，失败时返回缓存值。

## UI 层详解

### 应用启动流程

```
pyr train.py
    │
    ▼
cli.py: pyr()
    ├── detect_config_source_fast()  → 正则检测 argparse/pyruns.read
    ├── extract_argparse_params()    → AST 解析提取参数（仅 argparse 模式）
    ├── generate_config_file()       → 生成 config_default.yaml
    ├── os.environ[PYRUNS_ROOT] = ...
    └── from pyruns.ui.app import main; main()
            │
            ▼
        app.py: main()
            ├── TaskGenerator(root_dir)
            ├── TaskManager(root_dir)   → scan_disk() + _scheduler_loop()
            ├── SystemMonitor()
            └── ui.run(port=8099)
                    │
                    ▼
                main_page()  ← 每个浏览器连接调用一次
                    ├── AppState()  → 独立的 per-session 状态
                    └── render_main_layout()
                            ├── render_header()     → 品牌 + 系统指标
                            ├── render_sidebar()    → 导航按钮
                            └── content()           → @ui.refreshable
                                    └── page_router() → 根据 active_tab 渲染
```

### 页面切换机制

```
用户点击 Sidebar 按钮
    │
    ▼
state["active_tab"] = "monitor"
    │
    ▼
refresh_content()  →  content.refresh()
    │
    ▼
NiceGUI 销毁旧页面所有 DOM 元素
    │
    ▼
page_router() 重新调用 render_monitor_page()
    │
    ▼
ui.timer(0.1, _initial_load, once=True)
    │
    ▼
_initial_load():
    task_manager.scan_disk() / refresh_from_disk()
    task_list_panel.refresh()
```

**关键设计决策**：使用延迟加载（`ui.timer`）而非同步加载，避免在 `@ui.refreshable` 渲染周期中执行阻塞 I/O。

### 轮询与快照对比

Manager 和 Monitor 页面都使用快照对比来避免无意义的 UI 重建：

```python
def _task_snap(task_manager):
    """O(n) 内存快照，零磁盘 I/O"""
    return {
        t["id"]: (t["status"], t["progress"], t.get("monitor_count", 0))
        for t in task_manager.tasks
    }

def _poll():
    task_manager.refresh_from_disk()          # 轻量级：仅读活跃任务
    new_snap = _task_snap(task_manager)
    if new_snap != last_snap:                 # 只有变化时才重建 UI
        last_snap = new_snap
        task_list_panel.refresh()
```

**轮询频率**：
- Manager：每 2 秒轮询一次
- Monitor：每 1 秒轮询一次 + 增量日志推送

### CSS 注入策略

NiceGUI 服务端是持久的（不会重启），但用户可能刷新浏览器。使用 per-client 集合追踪 CSS 注入状态：

```python
_CSS_CLIENTS: set = set()

def _ensure_css():
    cid = ui.context.client.id          # 每个浏览器标签页唯一
    if cid not in _CSS_CLIENTS:
        _CSS_CLIENTS.add(cid)
        ui.add_css(_GLOBAL_CSS)
```

### 组件职责划分

| 级别 | 位置 | 职责 | 示例 |
|------|------|------|------|
| **页面** | `pages/*.py` | 组合组件、管理页面状态、绑定事件 | `generator.py`、`manager.py` |
| **组件** | `components/*.py` | 可复用的 UI 块，有自己的内部状态 | `param_editor.py`、`task_dialog.py` |
| **小部件** | `widgets.py` | 无状态的原子级 UI 元素 | `status_badge()`、`dir_picker()` |
| **主题** | `theme.py` | CSS 类名、颜色映射、图标名 | `STATUS_CARD_STYLES`、`PANEL_CARD` |

## 数据流

### 任务生命周期

```
Generator Page                    Core                         Disk
──────────────                    ────                         ────
用户编辑参数
    │
    ▼
generate_batch_configs()    →   configs: List[Dict]
    │
    ▼
TaskGenerator.create_tasks()  →  task_info.json + config.yaml + run.log
    │
    ▼
TaskManager.add_tasks()       →  内存 tasks 列表更新
    │
    ▼
Manager Page: task_list.refresh()  ←  UI 显示新卡片
    │
用户点击 RUN SELECTED
    │
    ▼
TaskManager.start_batch_tasks()   →  task.status = "queued"
    │                                  task_info.json 写入 status
    ▼
_scheduler_loop() 拉取 queued
    │
    ▼
executor.submit(run_task_worker)  →  子进程执行脚本
    │                                  stdout → run.log
    │                                  task_info.json → running → completed/failed
    ▼
_on_task_done()                   →  内存 tasks 状态同步
    │
    ▼
轮询: refresh_from_disk() + 快照对比 → UI 刷新
```

### 监控数据流

```
用户脚本                          Disk                    Monitor Page
────────                          ────                    ────────────
pyruns.add_monitor(epoch=1,...)
    │
    ▼
task_info.json["monitor"].append({
    "epoch": 1,
    "loss": 0.234,
    "_ts": "2026-02-13 10:30:45"
})
                                    │
                                    ▼
                            Monitor 轮询读取
                            load_monitor_data()
                                    │
                                    ▼
                            Export Dialog:
                            build_export_csv() / build_export_json()
```

## 设计原则

### 1. 磁盘即真相源（Disk as Source of Truth）

所有任务状态最终以 `task_info.json` 为准。内存中的 `tasks` 列表是缓存，通过 `scan_disk()` / `refresh_from_disk()` 同步。

### 2. 软删除

`delete_task()` 将目录移至 `.trash/`，而非直接删除，允许手动恢复。

### 3. 无数据库

不使用 SQLite 或其他数据库。每个任务是一个目录，包含 JSON + YAML + log 文件。文件系统即数据库。

### 4. 环境变量传递

`pyr` → `executor` → 用户脚本的配置传递全部通过环境变量 `PYRUNS_CONFIG`，用户脚本调用 `pyruns.read()` 自动从环境变量获取路径。

### 5. 延迟加载（Deferred Loading）

NiceGUI 的 `@ui.refreshable` 在渲染过程中维护元素树构建栈。在渲染周期中执行阻塞 I/O 会导致不可预测的行为。所有页面使用 `ui.timer(delay, callback, once=True)` 模式，确保数据加载在渲染完成后执行。

