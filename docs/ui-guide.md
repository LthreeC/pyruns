# UI 指南

当前 UI 主链路是 React + FastAPI。页面主要分为 `Generator`、`Manager`、`Monitor` 三页。

## 侧边栏与工作区

左下区域会显示当前：

- workspace 路径
- 当前脚本名，或 `_shell_`

高价值截断文本支持悬停查看完整内容。

### 切换脚本工作区

选择一个 `.py` 文件后，Pyruns 会定位它旁边的：

```text
<project>/_pyruns_/<script_name>/
```

### 切换 Shell Workspace

从脚本工作区切换到 shell workspace 后，当前工作区变成：

```text
<project>/_pyruns_/_shell_/
```

## Generator

Generator 会根据工作区类型显示不同编辑模式。

### 在脚本工作区

支持：

- `Form`
- `YAML`

#### Form

适合：

- 调整多个参数
- 使用批量语法
- pin 常用字段

特点：

- 顶部模板选择
- 参数区支持高密度布局
- 右侧固定生成按钮

#### YAML

适合：

- 直接编辑完整配置
- 一次只生成一个任务

### 在 Shell Workspace

固定为：

- `Shell`

特点：

- 编辑的是脚本正文
- 每次只生成一个任务
- 落盘文件为 `config.sh`
- 运行时使用当前宿主平台的原生 shell 语义

## Manager

Manager 负责任务管理和批量执行。

### 你可以做什么

- 搜索任务
- 按状态筛选
- 选择多个任务批量运行 / 删除
- pin 任务
- 查看详情
- 直接跳转 Monitor 日志

### 当前交互约定

- `Pinned` 区域独立展示，不只是排序提前
- 主按钮使用统一的紫色语义
- `Run` 使用统一 success 语义
- 批量选择使用统一勾选式 indicator

### 任务详情面板

常见标签：

- `Task Info`
- `Config` 或 `Script`
- `Notes`
- `Env Vars`

其中：

- `config` 任务显示 `config.yaml`
- `shell` 任务显示 `config.sh`

如果当前平台是 Windows，Shell 页面里的命令应默认按 Windows 原生命令风格书写，除非你显式配置了其他 shell executable。

## Monitor

Monitor 用来查看日志和导出。

### 默认行为

- 直接从侧边栏或 tab 进入时默认不选中任何任务
- 只有从任务卡片或 Dashboard 显式跳转时，才会带着当前任务进入
- 当前选中的任务消失时会清空选中状态，不会自动跳到别的任务

### 日志查看

当前实现重点：

- xterm 实例只初始化一次
- 切任务时避免重复 reset
- 请求带竞态保护，避免旧响应覆盖新内容
- websocket 断开后停止发送

### 导出

Monitor 支持选择多个任务并导出。

当前按钮文案统一为：

- `Export`

## 三页统一设计语言

当前 UI 收口目标：

- 紧凑间距
- 一致的 section 样式
- 一致的 pin 紫色语义
- 一致的选中态
- 一致的主按钮 / 次按钮层级

相似组件尽量共享同一套视觉语言：

- 状态 pill
- icon button
- search input
- dialog
- select state
