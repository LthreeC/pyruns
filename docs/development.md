# 开发者指南

## 开发环境搭建

### 1. 克隆仓库

```bash
git clone <repo-url>
cd pyruns
```

### 2. 安装依赖

```bash
# 推荐使用 conda
conda create -n pyruns python=3.10
conda activate pyruns

# 开发模式安装
pip install -e .
```

### 3. 启动开发服务器

```bash
# 方式一：通过 CLI 命令
pyr your_script.py

# 方式二：直接启动 UI
python -m pyruns.ui.app
```

NiceGUI 默认运行在 `http://localhost:8099`，`reload=False`（生产模式）。

---

## 代码规范

### 文件组织原则

| 原则 | 说明 |
|------|------|
| **单一职责** | 每个文件只负责一个功能领域 |
| **单文件不超过 ~300 行** | 超过时考虑拆分子模块 |
| **分层解耦** | `core/` 不导入 `ui/`，`utils/` 不导入 `core/` 或 `ui/` |
| **UI 逻辑分离** | `pages/` 只做页面组合，复杂逻辑提取到 `components/` 或 `core/` |

### 文件规模参考

| 文件 | 推荐行数 | 说明 |
|------|----------|------|
| `pages/*.py` | 200~400 | 页面级组合，包含事件绑定 |
| `components/*.py` | 100~300 | 可复用组件，有自己的状态 |
| `core/*.py` | 150~450 | 核心业务逻辑 |
| `utils/*.py` | 30~150 | 纯工具函数 |

### 命名约定

| 类型 | 约定 | 示例 |
|------|------|------|
| 文件名 | `snake_case.py` | `task_manager.py` |
| 类名 | `PascalCase` | `TaskManager`, `ConfigNode` |
| 函数名 | `snake_case` | `scan_disk()`, `load_yaml()` |
| 私有函数 | `_leading_underscore` | `_ensure_css()`, `_poll()` |
| 常量 | `UPPER_SNAKE_CASE` | `ROOT_DIR`, `INFO_FILENAME` |
| CSS 类集合 | `UPPER_SNAKE_CASE` | `PANEL_CARD`, `HEADER_GRADIENT` |
| 模块级 docstring | 必须 | 描述模块职责 |
| 函数 docstring | 推荐 | 非显而易见的函数必须有 |

### 导入顺序

```python
# 1. 标准库
import os
import json
from typing import Dict, Any

# 2. 第三方库
from nicegui import ui

# 3. 项目内部（按层级）
from pyruns._config import ROOT_DIR, INFO_FILENAME
from pyruns.core.task_manager import TaskManager
from pyruns.utils.config_utils import load_yaml
from pyruns.ui.theme import STATUS_ICONS
from pyruns.ui.widgets import status_badge
```

---

## NiceGUI 开发要点

### ⚠️ 不要在 `@ui.refreshable` 中执行阻塞 I/O

```python
# ❌ 错误
@ui.refreshable
def my_panel():
    data = load_from_disk()    # 阻塞！会导致 UI 状态不一致
    for item in data:
        ui.label(item)

# ✅ 正确
@ui.refreshable
def my_panel():
    for item in cached_data:   # 使用内存中的缓存数据
        ui.label(item)

def _initial_load():           # 在渲染完成后执行
    cached_data = load_from_disk()
    my_panel.refresh()

ui.timer(0.1, _initial_load, once=True)
```

### CSS 注入要 per-client

```python
# ❌ 错误 — 浏览器刷新后 CSS 丢失
_CSS_INJECTED = False

def _ensure_css():
    global _CSS_INJECTED
    if not _CSS_INJECTED:
        _CSS_INJECTED = True
        ui.add_css(MY_CSS)

# ✅ 正确 — 每个客户端独立追踪
_CSS_CLIENTS: set = set()

def _ensure_css():
    try:
        cid = ui.context.client.id
    except Exception:
        cid = "__default__"
    if cid not in _CSS_CLIENTS:
        _CSS_CLIENTS.add(cid)
        ui.add_css(MY_CSS)
```

### 嵌套 `@ui.refreshable` 的注意事项

```python
@ui.refreshable
def outer():
    @ui.refreshable
    def inner():
        ...

    inner()

    # ❌ 不要在 outer 的渲染周期中调用 inner.refresh()
    # ✅ 使用 timer 延迟调用
    ui.timer(0.05, inner.refresh, once=True)
```

### 轮询优化：快照对比

```python
# ❌ 每秒无条件刷新
def _poll():
    task_manager.refresh_from_disk()
    task_list.refresh()                    # 即使无变化也重建 DOM

# ✅ 快照对比
def _poll():
    task_manager.refresh_from_disk()
    new_snap = {t["id"]: t["status"] for t in task_manager.tasks}
    if new_snap != _last_snap:             # 仅变化时才刷新
        _last_snap = new_snap
        task_list.refresh()
```

### Quasar CSS 穿透

NiceGUI 使用 Quasar (Vue) 渲染组件，Quasar 会在模板中包装额外的 `<div>`，导致 CSS 无法直接作用于目标元素。

解决方案：

```css
/* 使用 !important 和子选择器 */
.my-container .nicegui-label,
.my-container label,
.my-container span {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}
```

---

## 添加新功能

### 添加新的 UI 页面

1. 创建 `pyruns/ui/pages/new_page.py`：

```python
"""New Page — description of the page."""
from nicegui import ui
from typing import Dict, Any

def render_new_page(state: Dict[str, Any], task_manager) -> None:
    """Entry point for the New tab."""
    # ... 渲染逻辑
```

2. 在 `pyruns/ui/app.py` 的 `page_router()` 中添加路由：

```python
elif tab == "new_page":
    render_new_page(state, task_manager)
```

3. 在 `pyruns/ui/components/sidebar.py` 的 `_TABS` 中添加导航项：

```python
_TABS = [
    ("Generator", "add_circle", "generator"),
    ("Manager", "dns", "manager"),
    ("Monitor", "monitor_heart", "monitor"),
    ("NewPage", "star", "new_page"),        # 新增
]
```

### 添加新的 UI 组件

1. 创建 `pyruns/ui/components/my_component.py`：

```python
"""My Component — reusable widget for X."""
from nicegui import ui

def my_component(data, on_change=None):
    """Render my component."""
    ...
```

2. 在需要的页面中导入使用：

```python
from pyruns.ui.components.my_component import my_component
```

### 添加新的核心功能

1. 创建 `pyruns/core/my_feature.py`（无 UI 依赖）
2. 在 `app.py` 的 `main()` 中初始化（如需全局单例）
3. 通过函数参数传递到 UI 层（不使用全局导入）

### 添加新的工具函数

1. 添加到现有 `pyruns/utils/*.py` 中
2. 或创建新的 `pyruns/utils/my_utils.py`
3. 确保是纯函数（无状态、无 UI 依赖、无 core 依赖）

---

## 调试技巧

### 查看所有模块导入是否正常

```bash
python -c "
from pyruns.ui.pages.monitor import render_monitor_page
from pyruns.ui.pages.manager import render_manager_page
from pyruns.ui.pages.generator import render_generator_page
from pyruns.ui.components.header import render_header
from pyruns.core.task_manager import TaskManager
from pyruns.ui.app import main_page, main
print('All imports OK')
"
```

### 日志系统

项目使用自定义的日志配置（`utils/log_utils.py`）：

```python
from pyruns.utils import get_logger
logger = get_logger(__name__)

logger.warning("Something went wrong: %s", error)
logger.error("Critical failure: %s", exc, exc_info=True)
```

**注意**：`logger.info()` 调用已被清理，仅保留 `warning` 和 `error` 级别。

### PowerShell 注意事项

Windows PowerShell 5.x 不支持 `&&`，使用 `;` 替代：

```powershell
# ❌
cd path && python script.py

# ✅
cd path ; python script.py
```

---

## 测试

### 手动测试清单

| 测试项 | 操作 |
|--------|------|
| Generator 基本功能 | 加载模板 → 编辑参数 → 生成 1 个任务 |
| Generator 批量生成 | 使用 `\|` 语法 → 确认对话框 → 生成 N 个任务 |
| Generator 视图切换 | Form ↔ YAML 视图切换，数据保持一致 |
| Manager 任务显示 | 卡片网格正确显示，状态颜色正确 |
| Manager 批量运行 | 勾选 → RUN → 状态变为 queued → running |
| Manager 单任务操作 | 卡片内 RUN / STOP / RERUN 按钮 |
| Manager 详情对话框 | 5 个标签页内容正确，Notes 可保存 |
| Monitor 初始加载 | **直接进入 Monitor 页面**（不先访问 Manager） |
| Monitor 日志查看 | 选择任务 → 日志实时更新 |
| Monitor 导出 | 勾选任务 → Export → CSV/JSON 下载 |
| Header GPU 显示 | 所有 GPU 卡独立显示 |
| 浏览器刷新 | 刷新后 CSS 不丢失，页面正常 |

---

## 项目结构变更日志

### 已完成的重构

| 变更 | 从 | 到 | 原因 |
|------|-----|-----|------|
| 日志 I/O | `core/log_io.py` | `utils/log_io.py` | 纯 I/O 工具不属于业务层 |
| 数据加载 | `core/report.py` / `utils/config_utils.py` | `utils/task_io.py` | 集中 task 数据 I/O |
| 批量确认框 | `pages/generator.py` 内联 | `components/batch_dialog.py` | 减少页面文件体积 |
| 环境变量编辑器 | `widgets.py` 内联 | `components/env_editor.py` | 分离可复用组件 |
| 进程工具 | `core/task_manager.py` 内联 | `utils/process_utils.py` | 分离工具函数 |
| `logger.info()` | 全部文件 | 已删除 | 开发期调试日志无需保留 |

