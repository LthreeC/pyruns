# pyruns

![logo](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/pyruns_logo2.png)

[English](README-en.md) | **简体中文**

[![PyPI version](https://img.shields.io/pypi/v/pyruns.svg)](https://pypi.org/project/pyruns/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyruns.svg)](https://pypi.org/project/pyruns/)
[![License](https://img.shields.io/pypi/l/pyruns.svg)](https://github.com/LthreeC/pyruns/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2563eb.svg)](https://lthreec.github.io/pyruns/)

> Python 实验管理 Web UI 工具：参数可视化编辑、批量任务生成、任务调度管理、实时日志流式查看与 CSV 指标导出。  
> 全流程本地运行，围绕磁盘工作区组织状态，让脚本实验这件事终于变得清楚、直接、可追踪。

![Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

Pyruns 为 Python 脚本提供基于本地浏览器的图形界面。它的核心思路不是“接管你的工程”，而是尽量贴着你已经在用的工作方式走：

- 继续使用原脚本
- 继续使用原终端 / shell
- 继续使用原 conda 环境与环境变量
- 把任务、配置、日志、备注、运行历史稳稳落在 `_pyruns_` 工作区里

**无需注册账号 · 无需联网 · 无需数据库 · 所有流程均在本地执行**

```bash
pip install pyruns
pyr train.py
pyr train.py my_config.yaml
pyr
```

推荐优先从这两条主路径开始：

- `pyr train.py`
- `pyr train.py my_config.yaml`

这两条是 Pyruns 最自然、也最应该被用户第一眼看到的接入方式。  
`pyr` 仍然可用，但更适合作为 shell / 命令任务的补充入口。

除了 Generator / Manager / Monitor，现在首页也会先把系统状态、任务概览和 GPU 资源收拢成一个更适合“刚打开就快速判断下一步”的入口。

![Home](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_home.png)

## 为什么它顺手

很多实验工具做得很大，但真正落到日常工作里，最麻烦的问题往往还是这些：

### 痛点 1：超参搜索还在靠手写循环

没有 Pyruns 时，你常常要写这种让人头皮发麻的嵌套循环：

```bash
for lr in 0.001 0.01 0.1; do
  for bs in 32 64 128; do
    for opt in adam sgd; do
      python train.py --lr $lr --batch_size $bs --optimizer $opt \
        > logs/lr${lr}_bs${bs}_${opt}.log 2>&1 &
    done
  done
done
wait
```

用了 Pyruns，在 `Form` 模式里写成这样就够了：

```yaml
lr: 0.001 | 0.01 | 0.1
batch_size: 32 | 64 | 128
optimizer: adam | sgd
```

点击生成后，就会自动展开成多组独立任务，每个任务都拥有自己的配置快照、运行目录和日志文件。

### 痛点 2：实验记录全靠人脑回忆

“上周那组 `lr=0.01` 的实验，用的到底是哪个 `batch_size`？”  
“那次 shell 任务跑在什么环境里？”  
“日志在哪个目录？”

Pyruns 会为每个任务保存：

- `config.yaml` 或 `config.sh`
- `task_info.json`
- `run_logs/runN.log`
- 运行时间线、PID、备注、环境信息

这意味着任务不是跑完就散掉，而是会留下完整、可检索、可复用的历史。

### 痛点 3：多任务并发时日志全混在一起

多个任务一起跑的时候，最难受的就是终端输出互相穿插。Pyruns 把每个任务的输出都隔离到独立日志文件，并在 `Monitor` 页面里提供更像真实终端的实时查看体验。

![Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_monitor.png)

对于 shell 工作流，这点尤其重要：

![Shell Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_monitor.png)

## 核心特性

| 特性 | 说明 |
| --- | --- |
| `React Generator` | 支持 `Form` / `YAML` / `Shell` 三种生成入口。脚本工作区可视化调参，shell 工作区直接编辑命令正文。 |
| `Form 批量生成` | 在 `Form` 模式中支持 `|`、`(|)`、`start:stop:step` 等批量语法，用于笛卡尔积、配对组合与区间展开。 |
| `YAML 单任务模式` | `YAML` 模式专注于一次生成一个完整 `config.yaml` 任务，不承担 batch 展开。 |
| `Shell Workspace` | 每个 shell 任务保存为 `config.sh`，默认跟随启动 `pyr` 的那个终端语义执行。 |
| `Manager 控制台` | 支持搜索、状态筛选、批量运行、批量删除、pin、详情查看与日志跳转。 |
| `Monitor 终端` | 使用 xterm.js 实时查看日志流、切换历史日志、复制终端内容，并导出 CSV 聚合结果。 |
| `指标导出` | 通过 `pyruns.record()` 记录实验指标后，可在 Monitor 中按任务勾选导出 CSV。 |
| `磁盘工作区` | 真实状态以磁盘为准。页面刷新、CLI / Web 共用、手工检查与备份都更简单。 |

## 两种工作区

先用一句话区分：

- `script` 模式：围绕一个 Python 脚本建立工作区
- `shell` 模式：围绕一个目录里的命令任务建立工作区

| 模式 | 入口 | 选择对象 | 任务文件 | 更适合 |
| --- | --- | --- | --- | --- |
| `script` | `pyr train.py` / `pyr train.py config.yaml` | Python 脚本 | `config.yaml` | `argparse`、`pyruns.load()`、配置驱动实验 |
| `shell` | `pyr` | 当前目录 | `config.sh` | PowerShell / cmd / bash 命令任务、终端工作流 |

最重要的区别不是页面像不像，而是 Pyruns 正在管理什么：

- `script` 模式管理的是“脚本 + 配置任务”
- `shell` 模式管理的是“目录 + 命令任务”

### 1. Script Workspace

当你打开一个普通 Python 脚本时，Pyruns 会围绕它创建一个独立工作区：

```text
project/
├─ train.py
└─ _pyruns_/
   ├─ _pyruns_settings.yaml
   └─ train/
      ├─ script_info.json
      ├─ config_default.yaml
      └─ tasks/
```

这一模式适合：

- `argparse` 脚本
- `pyruns.load()` / `pyruns.read()` 风格脚本
- 希望每个任务都带独立 `config.yaml` 的 Python 任务
- 把参数调优、模板配置、批量生成放在首位的工作流

### 2. Shell Workspace

当你切到 shell 模式时，Pyruns 管理的对象不再是某个 `.py` 文件，而是一个目录。  
也就是说，shell workspace 的起点是“文件夹”，不是“Python 脚本”。

当你切到 shell 模式，工作区会变成：

```text
project/
└─ _pyruns_/
   └─ _shell_/
      ├─ script_info.json
      └─ tasks/
```

每个 shell 任务落盘为：

```text
_pyruns_/_shell_/tasks/<task_name>/config.sh
```

最重要的语义是：

- 默认 `shell_mode: follow`
- 默认跟随启动 `pyr` 的当前终端
- 当前 Python 进程环境会继续继承给子进程
- 不自动做跨 shell 语法翻译

所以 shell 模式更像是：

- 把原本散落在终端历史里的命令整理成任务
- 把同一目录下的一组命令工作流纳入 Manager / Monitor
- 保留“像在原终端里执行”这件事本身

也就是说，shell task 的目标语义就是：

> 尽量等价于“在你启动 `pyr` 的那个终端里，再手动执行一次同样的命令”

![Shell Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_generator.png)

## 接入方式

### 模式 1：零侵入接入 `argparse`

如果你的脚本本来就是用 `argparse`：

```bash
pyr train.py
```

Pyruns 会尝试提取参数定义，生成可编辑表单，并把你在界面里修改后的值再拼回命令行参数。

### 模式 2：基于 YAML 模板接入

如果你的脚本通过 `pyruns.load()` 读取配置：

```bash
pyr train.py my_config.yaml
```

首次运行时，Pyruns 会把这份模板作为 `config_default.yaml` 保存下来。之后再次运行：

```bash
pyr train.py
```

系统会继续围绕这个工作区进行调参、生成和运行。

如果从 UI Launcher 里选择脚本，规则也一样：`argparse` 脚本可以直接打开；`pyruns.load()` 脚本第一次没有 `config_default.yaml` 时，需要先选择一份 YAML 作为默认模板。之后只要工作区里已有 `config_default.yaml`，就会直接复用；如果是 `argparse` 脚本，默认模板会按当前脚本参数重新刷新。

## 脚本内 API

Pyruns 暴露给训练脚本的 API 很少，基本就这几个：

| API | 用途 |
| --- | --- |
| `pyruns.load()` | 读取当前任务的 YAML / JSON 配置，返回可用点号访问的配置对象。 |
| `pyruns.read(path=None)` | 显式读取配置文件；通常直接用 `pyruns.load()` 就够了。 |
| `pyruns.record(**kwargs)` | 写入本次运行的最终指标，例如 `final_loss`、`acc`、`seed`。同一次运行会合并到一个 records 槽位。 |
| `pyruns.track(**kwargs)` | 写入时间序列指标，例如每个 epoch 的 `loss`、`acc`，会追加到 tracks 里。 |
| `pyruns.get_task_dir()` | 返回当前任务目录；不在 Pyruns 任务中运行时返回 `None`。 |
| `pyruns.get_run_index()` | 返回当前 run 槽位；适合一个任务多次运行时区分记录。 |

最常见的脚本写法：

```python
import pyruns

cfg = pyruns.load()

for epoch in range(cfg.training.epochs):
    loss = train_one_epoch(cfg)
    pyruns.track(loss=loss)

pyruns.record(final_loss=loss, seed=cfg.training.seed)
```

### 模式 3：CLI 交互模式

如果你在无头服务器上，或者更偏爱命令行操作，可以直接进入 CLI 交互模式：

```bash
pyr cli train.py
```

CLI 和 Web UI 使用同一套磁盘工作区与任务数据，操作结果天然互通。

### 模式 4：直接打开 shell workspace

如果你现在没有固定的 Python 入口脚本，只是想在当前目录直接管理一组命令：

```bash
pyr
```

这会直接创建并打开：

```text
<current_dir>/_pyruns_/_shell_
```

这条链路尤其适合：

- 命令型实验
- PowerShell / cmd / bash 任务
- 先从 shell 起步，再慢慢沉淀成稳定脚本工作流

## 实际上手示例

仓库中的 `examples/` 已经提供了可以直接运行的示例。

### 示例 1：Argparse 原生支持

目录：

```text
examples/1_argparse_script/
```

适合展示的页面通常是：

- `Generator` 表单页
- `Manager` 中展开后的任务卡片

### 示例 2：使用 `pyruns.load()` 加载 YAML

目录：

```text
examples/2_pyruns_config/
```

这一类脚本更适合展示：

- 独立 `config.yaml` 任务快照
- `Task Info` / `Env` 等任务详情

![Task Detail](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/task_info.png)

## 页面模块

### Home

Home 是进入工作区后的第一眼总览。  
它负责先把“现在这台机器和这份工作区处于什么状态”讲清楚，而不是让你一上来就扎进任务列表。

![Home Preview](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_home.png)

这一页适合先看：

- 当前任务总览
- 活跃 / 已完成 / 失败的状态分布
- GPU 与系统资源占用
- 接下来该去 Generator、Manager 还是 Monitor

### Generator

Generator 是参数编辑与任务生成的入口。  
这一页要做的不是“展示所有字段”，而是让你在高密度信息里依然能快速找到重点参数、快速生成任务。

![Generator Preview](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

你可以在这里做这些事：

- 选择模板
- 用 `Form` 模式快速调整参数
- 用 `YAML` 模式编辑完整配置
- 用 `Shell` 模式直接写命令正文
- 使用 pin 把关键参数固定在视线里
- 预览 batch 展开的结果

### Manager

Manager 是任务调度台。  
这里不只是“看任务”，更是“处理任务”。

![Manager Preview](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

它支持：

- 多行搜索
- 状态筛选
- 批量运行 / 删除
- pinned tasks 独立展示
- 任务详情抽屉
- 一键跳转到 Monitor 看日志

### Monitor

Monitor 是运行中任务的观测面。  
它承担的是“让日志真正变得可工作”，而不是只把 stdout 放在页面上。

它支持：

- 实时日志流
- 历史日志切换
- 终端复制
- 按任务勾选导出 CSV
- 与 Manager / Task Detail 形成来回联动

## 配置入口

工作区配置文件位于：

```text
<project>/_pyruns_/_pyruns_settings.yaml
```

比较重要的键包括：

- `header_refresh_interval`
- `generator_form_columns`
- `manager_columns`
- `manager_execution_mode`
- `monitor_sidebar_width_pct`
- `shell_mode`
- `shell_executable`

其中 shell 相关最重要的理解方式是：

- 默认保持 `shell_mode: follow`
- 只有明确需要固定某个 shell 时，才切到 `custom`

## 文档导航

- [快速开始](docs/getting-started.md)
- [页面展示](docs/showcase.md)
- [界面指南](docs/ui-guide.md)
- [配置说明](docs/configuration.md)
- [架构说明](docs/architecture.md)
- [批量语法](docs/batch-syntax.md)
- [CLI 指南](docs/cli-guide.md)

## License

MIT
