# Pyruns 问题排查与解决记录

本文档记录了开发过程中遇到的疑难 Bug 及其根因分析和修复方案。

---

## 目录

- [目录](#目录)
- [1. Monitor 页面必须先点 Manager 才能加载任务列表](#1-monitor-页面必须先点-manager-才能加载任务列表)
  - [现象](#现象)
  - [根因分析](#根因分析)
  - [修复方案](#修复方案)
  - [相关文件](#相关文件)
- [2. Monitor 页面任务列表直接进入加载的日志是错误的](#2-monitor-页面任务列表直接进入加载的日志是错误的)
  - [现象](#现象-1)
  - [根因分析](#根因分析-1)
  - [修复方案](#修复方案-1)
  - [相关文件](#相关文件-1)
- [3. 任务名称在侧栏换行导致 UI 难看](#3-任务名称在侧栏换行导致-ui-难看)
  - [现象](#现象-2)
  - [根因分析](#根因分析-2)
  - [修复方案](#修复方案-2)
  - [相关文件](#相关文件-2)
- [4. 标签页切换卡顿](#4-标签页切换卡顿)
  - [现象](#现象-3)
  - [根因分析](#根因分析-3)
  - [修复方案](#修复方案-3)
  - [相关文件](#相关文件-3)
- [5. Header 组件 GPU 显示异常 (IndentationError)](#5-header-组件-gpu-显示异常-indentationerror)
  - [现象](#现象-4)
  - [根因分析](#根因分析-4)
  - [修复方案](#修复方案-4)
  - [相关文件](#相关文件-4)
- [6. CSS 在浏览器刷新后丢失](#6-css-在浏览器刷新后丢失)
  - [现象](#现象-5)
  - [根因分析](#根因分析-5)
  - [修复方案](#修复方案-5)
  - [相关文件](#相关文件-5)
- [7. PowerShell 中 \&\& 语法报错](#7-powershell-中--语法报错)
  - [现象](#现象-6)
  - [根因分析](#根因分析-6)
  - [修复方案](#修复方案-6)
- [架构设计要点](#架构设计要点)
  - [代码分层](#代码分层)
  - [NiceGUI 开发注意事项](#nicegui-开发注意事项)

---

## 1. Monitor 页面必须先点 Manager 才能加载任务列表

### 现象

用户打开应用后直接点击 Monitor 标签，左侧任务列表为空，无法选择任何任务查看日志。但如果先访问 Manager 页面再切换到 Monitor，任务列表就能正常加载。

### 根因分析

**核心问题：在 NiceGUI 的 `@ui.refreshable` 渲染周期中执行了同步的阻塞 I/O 操作。**

具体流程：

1. `TaskManager.__init__()` 在应用启动时调用 `scan_disk()`，首次加载任务列表到内存中
2. 用户直接点击 Monitor 时，`render_monitor_page()` 被调用
3. 在旧代码中，Monitor 页面的渲染函数内部 **同步** 调用了 `task_manager.scan_disk()`
4. 这个调用发生在 `@ui.refreshable` 的嵌套上下文中
5. NiceGUI 的 `@ui.refreshable` 在渲染过程中维护一个「元素树构建栈」
6. 在嵌套的 `@ui.refreshable` 中执行同步 I/O 操作可能导致内部状态不一致

**为什么 Manager 先访问就正常：**
- Manager 页面的 `@ui.refreshable task_list` 定义在顶层，不嵌套在其他 refreshable 中
- Manager 的 `scan_disk()` 调用不在 refreshable 渲染周期内
- 访问 Manager 后，`task_manager.tasks` 已被正确填充，Monitor 后续访问时直接使用内存中的数据即可

### 修复方案

**延迟加载模式（Deferred Loading）：**

```python
# ❌ 旧代码 — 在渲染函数中同步加载
def render_monitor_page(state, task_manager):
    task_manager.scan_disk()  # 阻塞！在 refreshable 中执行
    # ... 渲染 UI ...

# ✅ 新代码 — 延迟到渲染完成后
def render_monitor_page(state, task_manager):
    # ... 渲染 UI 骨架 ...

    def _initial_load():
        if not task_manager.tasks:
            task_manager.scan_disk()
        else:
            task_manager.refresh_from_disk()
        task_list_panel.refresh()  # 安全地在渲染周期外刷新

    ui.timer(0.1, _initial_load, once=True)  # NiceGUI 元素树提交后执行
```

**关键点：**
- `ui.timer(0.1, callback, once=True)` 确保回调在 NiceGUI 完成元素树构建后才执行
- 在 timer 回调中刷新 `@ui.refreshable` 组件是安全的，因为此时 UI 上下文已完全就绪
- 同样的模式后来也被应用到 Manager 页面，使两个页面行为一致

### 相关文件

- `pyruns/ui/pages/monitor.py` — `_initial_load()` + `ui.timer()`
- `pyruns/ui/pages/manager.py` — 同样采用延迟加载模式

---

## 2. Monitor 页面任务列表直接进入加载的日志是错误的

### 现象

即使任务列表加载出来了，选择某个任务后显示的日志内容可能是旧的或不完整的。定时轮询也不能正确刷新。

### 根因分析

原始的轮询机制 `_poll()` 仅在有选中任务时才刷新左侧任务列表。当没有选中任务时，新加入的任务或状态变化不会被反映。

此外，轮询每次都调用 `task_manager.refresh_from_disk()` 并无条件地刷新 UI，即使数据没有变化，这导致了不必要的 UI 重建和性能浪费。

### 修复方案

**快照对比轮询（Snapshot-based Polling）：**

```python
def _task_snap(task_manager):
    """O(n) 内存快照，零磁盘 I/O"""
    return {
        t["id"]: (t["status"], t["progress"], t.get("monitor_count", 0))
        for t in task_manager.tasks
    }

def _poll():
    task_manager.refresh_from_disk()
    new_snap = _task_snap(task_manager)
    if new_snap != _last_snap:  # 只有真正变化时才刷新
        _last_snap = new_snap
        task_list_panel.refresh()
```

**改进点：**
- 使用快照对比避免无变化时的冗余 UI 重建
- 每 30 秒执行一次 `scan_disk()`（全量扫描）检测新增/删除的任务
- 中间的轮询只执行 `refresh_from_disk()`（仅刷新活跃任务状态）

### 相关文件

- `pyruns/ui/pages/monitor.py` — `_task_snap()` + `_poll()`

---

## 3. 任务名称在侧栏换行导致 UI 难看

### 现象

Monitor 页面左侧任务列表中，较长的任务名称（如 `task_2026-02-12_18-30-45-[1-of-12]`）会在状态图标后换行，导致列表视觉混乱。

### 根因分析

NiceGUI 使用 Quasar（Vue 框架）渲染组件，Quasar 的 `QRow` 组件默认允许 `flex-wrap: wrap`，即子元素会在空间不足时自动换行。

CSS 的 `white-space: nowrap` 仅对 **文本节点** 生效，但 NiceGUI 的 `ui.label()` 会被包装在 Quasar 组件的额外 `<div>` 中，导致 CSS 属性无法穿透到实际文本元素。

### 修复方案

三重保障策略：

1. **容器级别**：`flex-wrap: nowrap` 禁止行级换行
2. **元素级别**：使用 `ui.element("div")` 配合内联样式直接控制文本容器
3. **全局 CSS**：在 `widgets.py` 中注入针对 `.monitor-task-item` 子元素的强制规则

```css
/* 全局 CSS 强制规则 */
.monitor-task-item {
    flex-wrap: nowrap !important;
    overflow: hidden;
}
.monitor-task-item .nicegui-label,
.monitor-task-item label,
.monitor-task-item span {
    white-space: nowrap !important;
    overflow: hidden !important;
    text-overflow: ellipsis !important;
}
```

```python
# 单个任务项的渲染
with ui.element("div").style(
    "flex: 1 1 0; min-width: 0; overflow: hidden;"
):
    ui.label(task_name).style(
        "white-space: nowrap; overflow: hidden; text-overflow: ellipsis; "
        "display: block; width: 100%;"
    ).tooltip(task_name)  # 鼠标悬停显示完整名称
```

### 相关文件

- `pyruns/ui/pages/monitor.py` — `_task_list_item()`
- `pyruns/ui/widgets.py` — `_GLOBAL_CSS`

---

## 4. 标签页切换卡顿

### 现象

在 Generator / Manager / Monitor 三个标签页之间切换时，有明显的卡顿感，尤其是在任务数量较多时。

### 根因分析

NiceGUI 的 SPA（单页应用）模式使用 `@ui.refreshable` 实现标签切换。每次切换标签时：

1. `sidebar.py` 调用 `refresh_content()` → `content.refresh()`
2. NiceGUI **销毁** 旧页面的所有 DOM 元素
3. **重新创建** 新页面的所有 DOM 元素
4. 新页面的渲染函数中可能包含同步的磁盘 I/O

具体瓶颈：
- **Manager 页面**：旧代码在渲染函数中同步调用 `scan_disk()` 或 `refresh_from_disk()`，读取每个任务的 `task_info.json`
- **Generator 页面**：`list_template_files()` 遍历目录、`load_yaml()` 读取配置文件
- **大量任务卡片**：数百张任务卡片的 DOM 创建本身也需要时间

### 修复方案

1. **延迟加载**：Manager 和 Monitor 页面都采用 `ui.timer(0.05, _initial_load, once=True)` 模式，先渲染空骨架，再异步加载数据
2. **快照对比**：轮询时只在数据真正变化时才刷新 UI，避免无意义的 DOM 重建
3. **`monitor_count` 缓存**：在 `TaskManager` 层面缓存每个任务的监控数据条数，避免 Monitor 页面的每个列表项都读取 JSON

```python
# Manager 页面的延迟加载
task_list()  # 先渲染空骨架
ui.timer(0.05, _initial_load, once=True)  # 然后异步加载数据
```

### 相关文件

- `pyruns/ui/pages/manager.py` — `_initial_load()`
- `pyruns/ui/pages/monitor.py` — `_initial_load()`
- `pyruns/core/task_manager.py` — `monitor_count` 字段

---

## 5. Header 组件 GPU 显示异常 (IndentationError)

### 现象

在包含 GPU 显示的 `header.py` 文件中存在 Python 缩进错误，导致整个 header 组件无法加载。该错误在特定条件下才触发，因为 `header.py` 是被间接导入的。

### 根因分析

`_gpu_chip()` 函数中 `with ui.row()` 语句多缩进了一级，变成了条件语句体外的缩进，产生 `IndentationError: unexpected indent`。

```python
# ❌ 错误代码
def _gpu_chip(gpu):
    util = gpu["util"]
    mem_used = gpu["mem_used"]
    mem_total = gpu["mem_total"]

        with ui.row().classes(...):  # 多了一级缩进！
```

### 修复方案

修正缩进，同时重构函数为更清晰的结构，将 `stat()` 内联函数提取为 `_stat_chip()` 顶层函数。

### 相关文件

- `pyruns/ui/components/header.py`

---

## 6. CSS 在浏览器刷新后丢失

### 现象

通过 `ui.add_css()` 注入的全局样式（如 CodeMirror 只读样式、Monitor 日志样式等），在用户刷新浏览器后丢失，导致 UI 异常。

### 根因分析

原始代码使用模块级全局布尔变量 `_CSS_INJECTED = False` 来防止重复注入。但 NiceGUI 服务端的模块状态是持久的，当用户刷新浏览器时：

1. 服务端创建了一个 **新的 NiceGUI Client**
2. 但模块级 `_CSS_INJECTED` 仍然是 `True`（上一个客户端设置的）
3. CSS 不会为新客户端重新注入

### 修复方案

使用 per-client 的 CSS 注入追踪：

```python
_CSS_CLIENTS: set = set()  # 追踪哪些客户端已注入 CSS

def _ensure_css():
    try:
        cid = ui.context.client.id  # 每个浏览器标签页有唯一 ID
    except Exception:
        cid = "__default__"
    if cid not in _CSS_CLIENTS:
        _CSS_CLIENTS.add(cid)
        ui.add_css(_GLOBAL_CSS)
```

### 相关文件

- `pyruns/ui/widgets.py` — `_ensure_css()`
- `pyruns/ui/components/param_editor.py` — `_ensure_editor_css()`

---

## 7. PowerShell 中 && 语法报错

### 现象

在 Windows PowerShell 中运行 `cd "path" && python -c "..."` 报错：
```
标记"&&"不是此版本中的有效语句分隔符。
```

### 根因分析

`&&` 是 Bash/CMD 的链式命令语法，PowerShell（v5.x）不支持。PowerShell 7+ 支持 `&&`，但 Windows 默认安装的是 PowerShell 5.x。

### 修复方案

在 PowerShell 中使用 `;` 替代 `&&`：

```powershell
# ❌ PowerShell 5.x 不支持
cd "path" && python script.py

# ✅ 使用分号
cd "path" ; python script.py
```

注意：`;` 不具备「前一个命令成功才执行下一个」的语义，如需此行为应使用：

```powershell
cd "path"; if ($?) { python script.py }
```

---

## 架构设计要点

### 代码分层

```
core/    → 业务逻辑（不依赖 UI 框架）
ui/      → NiceGUI 界面
  pages/      → 页面级渲染（完整标签页）
  components/ → 可复用组件（对话框、编辑器等）
  widgets.py  → 原子级小组件（按钮、徽章等）
utils/   → 纯工具函数（无状态、无 UI 依赖）
```

### NiceGUI 开发注意事项

1. **不要在 `@ui.refreshable` 渲染周期中执行阻塞 I/O**
   — 使用 `ui.timer(delay, callback, once=True)` 延迟到渲染完成后

2. **CSS 注入要 per-client**
   — 使用 `ui.context.client.id` 追踪，而非全局布尔变量

3. **嵌套 `@ui.refreshable` 需格外小心**
   — 在外层 refreshable 的渲染函数中定义内层 refreshable 时，确保内层的 `refresh()` 不在外层的渲染周期中被调用

4. **轮询优化：快照对比**
   — 用 `dict` 快照对比新旧状态，仅在变化时调用 `.refresh()`，避免每秒无意义的 DOM 重建

5. **Quasar CSS 穿透**
   — Quasar 组件会在 Vue 模板中包装额外的 `<div>`，CSS 需要用 `!important` 和深层选择器才能穿透到实际元素

