# UI 指南

当前的 Pyruns UI 已经不再是“附带页面”，而是一套真正承担主流程的 React 工作台。  
它的目标不是做得花，而是做得顺、做得稳、做得像你每天真的会打开的工具。

## 页面整体气质

这一版 UI 的收口方向很明确：

- 更紧凑
- 更有层级
- 更少噪声
- 更像实验工作台，而不是资讯面板

![Generator 全页界面](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

## 侧边栏与工作区

左下区域会显示当前：

- workspace 路径
- 当前脚本名，或 `_shell_`
- shell runtime 来源

长文本支持悬停查看完整内容，这一点在深层路径和 shell workspace 下尤其重要。

### 切换脚本工作区

选择一个 `.py` 文件后，Pyruns 会定位它旁边的：

```text
<project>/_pyruns_/<script_name>/
```

### 切换 Shell Workspace

从脚本工作区切换到 shell workspace 后，当前工作区会变成：

```text
<project>/_pyruns_/_shell_/
```

## Generator

Generator 是整个 UI 最像“工作台”的页面。

### 在脚本工作区

支持：

- `Form`
- `YAML`

#### Form

适合：

- 调整多个参数
- 使用 batch 语法
- pin 常用字段

当前特点：

- 类型 chip 会按模板声明类型稳定显示
- pin 顶部参数不再强行重新排位，而是更强调状态变化
- batch 触发项会被单独标记
- 右侧固定生成区始终在视线里

#### YAML

适合：

- 直接编辑完整配置
- 一次只生成一个任务
- 想保留完整 YAML 语义的人

当前编辑器已经补上更强的高亮和更宽松的编辑空间。

### 在 Shell Workspace

固定为：

- `Shell`

特点：

- 编辑的是脚本正文
- 每次只生成一个任务
- 落盘文件为 `config.sh`
- 默认跟随启动 `pyr` 的终端语义执行

![Shell Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_generator.png)

## Manager

Manager 是任务的调度台。  
你不只是看见任务，而是要快速决定“接下来对它做什么”。

![Manager 全页界面](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

建议这里放：

- 一张带状态摘要、筛选器、任务卡片网格的整页截图
- 最好能看见 pinned 区和卡片底部按钮

### 你可以做什么

- 搜索任务
- 按状态筛选
- 选择多个任务批量运行 / 删除
- pin 任务
- 查看详情
- 直接跳转 Monitor 日志

### 当前交互约定

- `Pinned` 区域独立展示，不只是排序提前
- 主按钮、危险按钮、次按钮已经尽量统一语义
- 卡片底部动作保持紧凑，减少鼠标移动距离
- 搜索框支持多行输入，换行按 AND 语义过滤

### 任务详情面板

常见标签包括：

- `Task Info`
- `Config` 或 `Script`
- `Notes`
- `Env Vars`

其中：

- `python` 任务显示 `config.yaml`
- `shell` 任务显示 `config.sh`

![任务详情面板](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/task_info.png)

## Monitor

Monitor 的目标不是“把日志显示出来”，而是把日志变成一个可工作的界面。

![Monitor 全页界面](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_monitor.png)

建议这里放：

- 一张带左侧任务栏、中央 xterm、顶部动作栏的截图
- 最好包含一段真实运行日志

### 默认行为

- 直接从侧边栏或 tab 进入时默认不选中任何任务
- 只有从任务卡片或 Dashboard 显式跳转时，才会带着当前任务进入
- 当前选中的任务消失时会清空选中状态，不会自动跳到别的任务

### 日志查看

当前实现重点：

- xterm 实例只初始化一次
- 切任务时避免重复 reset
- 请求带竞态保护，避免旧响应覆盖新内容
- websocket chunk 会校验任务身份，避免串日志

### 导出

Monitor 支持选择多个任务并导出，适合快速聚合一批日志做对照或归档。

![Shell Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_monitor.png)

## Dashboard

Dashboard 是第一页，也是最适合做“项目展示图”的页面。

现在它主要承担：

- 全局任务摘要
- CPU / RAM / GPU 状态
- 多 GPU 卡片布局
- GPU 显存占用与进程明细入口

这一页目前还缺一张真正能代表首页状态的截图。  
如果你后面继续补素材，最值得新增的依然是 Dashboard 总览图和 GPU 明细图。

## 三页统一设计语言

当前 UI 的统一目标包括：

- section 结构统一
- pin 语义统一为紫色
- 选中态统一
- 信息密度更高但不显得挤
- 一致的搜索框、对话框、状态 badge 和 action button

相似组件尽量共享同一套语言，这样用户不用每页重新学习一次。
