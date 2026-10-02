# UI 指南

当前的 Pyruns UI 已经不再是“附带页面”，而是一套真正承担主流程的 React 工作台。  
它的目标不是做得花，而是做得顺、做得稳、做得像你每天真的会打开的工具。

Web UI 必须显式启动；裸 `pyr` 或 `pyruns` 只显示 CLI 帮助：

```bash
pyr ui
pyr ui train.py
pyr ui train.py --config configs/default.yaml
pyr ui shell
pyr ui shell --no-browser
```

服务只监听本机回环地址。启动时会生成新的随机访问令牌；浏览器首次打开完整 URL 后，
服务器写入持久的 `HttpOnly`、`SameSite=Strict` 会话 cookie，并重定向到不含令牌的
干净地址。会话在使用时自动续期，并可跨同一主机、端口和工作区的正常更新或重启保留；
只有清除浏览器数据或本地 `~/.pyruns/sessions` 状态后才需要重新打开最新 URL。
`--no-browser` 会在终端打印完整访问 URL，请勿共享。Pyruns UI 不是面向公网或多用户的服务。

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

- `Grid`
- `Tree`
- `YAML`

#### Grid / Tree

适合：

- 调整多个参数
- 使用 batch 语法
- pin 常用字段

当前特点：

- `Grid` 适合快速扫描和编辑扁平参数，`Tree` 配合 Outline 按配置路径浏览
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
- 落盘文件为当前 runtime 对应的 `config.ps1` / `config.cmd` / `config.sh`
- 默认跟随启动 Web UI 时的终端语义执行

![Shell Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_generator.png)

## Manager

Manager 是任务的调度台。  
你不只是看见任务，而是要快速决定“接下来对它做什么”。

![Manager 全页界面](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

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
- 搜索框默认检索全部字段，可选择任务名、备注、Python 配置、Shell 脚本、任务 Env 或完整日志，并组合区分大小写、全词和正则匹配

搜索按原样保留空格与换行，默认不区分大小写。按 `Enter` 立即搜索，`Shift+Enter` 插入换行；多行文本必须在同一个字段或日志文件内连续出现。

Manager 和 Monitor 均支持 `Ctrl/Cmd+Shift+F` 聚焦并选中查询。在首行按 `↑` 回看历史，在末行按 `↓` 返回较新的查询，最后回到未提交的草稿；多行输入内部仍按普通光标键移动。最近 100 条查询在当前浏览器保存、去重并由两页共用，可在搜索设置中清空。自动搜索的历史延迟 2 秒记录，避免保存连续输入的每一步。

搜索框旁的筛选按钮提供搜索设置：默认边输入边搜索、等待 300 ms、最多 20,000 个匹配。结果分批出现，达到上限后会提示，可选择不限制数量重新搜索。设置在 Manager 和 Monitor 间共用并保存在当前浏览器。搜索结果不会定时重扫，日志或任务变化后使用刷新按钮更新；搜索中点击停止按钮或按 `Escape` 可以取消。

完整日志搜索使用随依赖安装的 ripgrep，按原始 UTF-8 文件内容匹配；颜色转义仅在结果预览中隐藏。扫描期间就会显示已找到的匹配，不必等当前日志文件读完；后续进度合并推送，减少界面刷新开销。点击结果仍会打开对应历史日志的匹配位置。日志追加、重写或替换后，下次搜索会重新检查文件。

### 任务详情面板

常见标签包括：

- `Info`
- `Config`
- `Notes`
- `Env`

其中：

- `Info` 显示任务模式、目录和完整 Run History
- `python` 任务显示 `config.yaml`
- `shell` 任务显示当前 runtime 对应的 `config.ps1` / `config.cmd` / `config.sh`

![任务详情面板](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/task_info.png)

## Monitor

Monitor 的目标不是“把日志显示出来”，而是把日志变成一个可工作的界面。

![Monitor 全页界面](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_monitor.png)

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

浏览器标签页进入后台时，Monitor 暂停任务事件和实时日志连接；返回时更新任务状态，并从已接收的位置继续读取日志。Manager、Monitor 和 Dashboard 的定时刷新也会在后台暂停，返回时立即补刷。搜索结果仍需使用刷新按钮更新。

Monitor 收到任务变更事件后读取服务端已有快照，合并短时间内的连续事件。连接正常时每 60 秒校验一次磁盘状态，断连时改为每 5 秒重试；手动刷新会立即同步磁盘。

### 导出

Monitor 支持选择多个任务并导出，适合快速聚合一批日志做对照或归档。

![Shell Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_monitor.png)

## Dashboard

Dashboard 是第一页，也是最适合做“工作区展示图”的页面。

现在它主要承担：

- 当前 workspace 的任务摘要和最近任务
- CPU / RAM / GPU 状态
- 多 GPU 卡片布局
- GPU 显存占用与进程明细入口

## 四页统一设计语言

当前 UI 的统一目标包括：

- section 结构统一
- pin 语义统一为紫色
- 选中态统一
- 信息密度更高但不显得挤
- 一致的搜索框、对话框、状态 badge 和 action button

相似组件尽量共享同一套语言，这样用户不用每页重新学习一次。
