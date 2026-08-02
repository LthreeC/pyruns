# Pyruns CLI 详细指南

Pyruns CLI 采用与 Git 相同的核心交互思想：一套稳定接口、两个等价入口、明确的子命令、一次调用完成一件事，结果可由退出码和标准输出判断。它没有需要持续操控的交互式 REPL，也不会在裸命令下悄悄启动 Web 服务。

```bash
pyr --help
pyruns --help
pyr COMMAND [OPTIONS]
pyruns COMMAND [OPTIONS]
pyr -w WORKSPACE COMMAND [OPTIONS]
```

`pyr` 与 `pyruns` 都是正式入口，命令、选项、输出和退出码完全一致。本文为简洁统一使用 `pyr`。任务必须使用精确名称，不支持序号、模糊匹配、旧命令或隐式别名。

## 1. 设计约定

### 一次调用，一次操作

每个命令执行完都会退出。人、Shell 脚本、CI 和 AI agent 使用的是同一套接口，不需要识别提示符或维护会话状态。

### 默认行为必须安全且可预测

- 裸 `pyr` 或 `pyruns` 都只打印包含常用命令和快速示例的精简帮助；`pyr help -a` 才展开完整命令索引。
- `run` 默认等待全部任务结束。
- 批量任务中任意一个失败，命令退出码就是 `1`。
- `--detach` 只改变等待方式，不改变任务语义。
- `rm` 立即执行且不确认，但只是可恢复的软删除。
- Web UI 只能由 `pyr ui` / `pyruns ui` 或对应的 `dev` 命令显式启动。
- Windows 后台 runner 和任务进程不会创建额外控制台窗口。

### 输出属于接口的一部分

- 正常数据写 stdout。
- 进度提示和错误写 stderr。
- 用法错误返回 `2`。
- `--json` 提供稳定机器输出；它必须放在子命令之前。
- `log` 默认原样输出日志，不混入表格或装饰文本。

## 2. 全局选项

```text
-C, --directory PATH              从 PATH 目录执行
-w, --workspace NAME|PATH|SCRIPT  精确选择工作区
--json                            输出稳定 JSON
--no-color                        禁用 ANSI 颜色
--debug                           内部异常时显示 traceback
--version                         输出版本并退出
```

全局选项放在子命令前：

```bash
pyr --json -w train ls
pyr -C D:/work/project -w shell status
```

每个子命令都有独立帮助：

```bash
pyr help -a
pyr help run
pyr run --help
```

默认帮助只列 `init exec add run ls show log stop` 八个日常命令，高级命令仍可直接执行；需要浏览全部命令时使用 `help -a`。

## 3. 工作区发现

Pyruns 从当前目录开始向父目录查找最近的 `_pyruns_`。找到后按以下规则选择工作区：

1. 显式传入 `-w` 时，只使用该精确选择。
2. 项目只有一个工作区时，自动选择它。
3. 项目存在多个工作区时，拒绝猜测并要求 `-w`。
4. 没找到项目时，提示先执行 `pyr init`。

`-w` 支持四种值：

```bash
pyr -w shell ls                       # shell workspace
pyr -w train ls                       # _pyruns_/train
pyr -w ./train.py ls                  # 由脚本定位 workspace
pyr -w ./_pyruns_/train ls            # workspace 绝对或相对路径
```

Shell workspace 保存任意终端命令；script workspace 保存某个 Python 入口脚本的参数快照与运行记录。

## 4. 初始化：`init`

初始化当前项目的 shell workspace：

```bash
pyr init
```

初始化 Python 脚本工作区：

```bash
pyr init train.py
pyr init train.py --config configs/default.yaml
```

`init` 只建立磁盘工作区，不运行任务，也不启动 UI。成功时输出实际工作区路径。

## 5. 运行终端命令：`exec`

### 精确 argv 模式

默认使用 `--` 后的参数向量。它最适合普通程序调用，也最适合自动化：

```bash
pyr exec --name smoke -- python -V
pyr exec --name train -- python train.py --epochs 10 --lr 0.001
```

Pyruns 会为每个参数做当前 shell 所需的转义，再把任务保存为 `config.ps1`、`config.cmd` 或 `config.sh`。参数里的空格和短横线不会被误认为 Pyruns 自己的选项。

### 无副作用预览

在真正创建 workspace、task 或启动 runner 前，可以查看完整执行计划：

```bash
pyr exec --dry-run --name smoke -- python -V
pyr --json exec --dry-run --name smoke -- python -V
```

计划会说明目标 workspace 是否需要创建、任务名是否可精确使用、工作目录、argv 或 shell 表达式、解释器、环境变量和 detach 状态。`--dry-run` 不创建 `_pyruns_`、设置文件或任务目录，也不会执行用户命令。显式任务名已存在时仍会像真实执行一样报错；省略 `--name` 且默认名称冲突时，计划会说明真实执行需要生成唯一后缀。

### 直接运行 Shell 脚本文件

当精确 argv 的第一个参数是已有的 Shell 脚本文件时，Pyruns 自动选择解释器：

| 文件 | 运行方式 |
| --- | --- |
| `.sh` | 当前可用的 Bash/sh；不要求文件具有 executable bit |
| `.ps1` | PowerShell 的非交互 `-File` 模式 |
| `.cmd` / `.bat` | Windows `cmd.exe` |

```bash
pyr exec --name setup -- ./scripts/setup.sh
pyr exec --name setup-ps -- .\scripts\setup.ps1
pyr exec --name setup-cmd -- .\scripts\setup.cmd
```

脚本路径可以包含空格。路径之后如果还有内容，它们是脚本自己的参数，不是 Pyruns 参数，并会按独立 argv 传递。文件不存在、平台不支持或解释器不可用时，`exec` 会在创建任务前给出明确错误。任务保存的是解释器调用与原脚本绝对路径，不复制脚本本身；每次运行会记录脚本内容哈希和所在 Git 状态，因此重跑既保留脚本原目录语义，也能识别源文件变化。

`--detach`、`run`、`wait`、`stop`、`show` 和 `log` 对这类任务没有特殊规则，和普通 Shell 任务完全一致：

```bash
pyr exec --name setup --detach -- ./scripts/setup.sh
pyr -w shell wait setup
pyr -w shell run setup
pyr -w shell log setup
```
### 显式 shell 模式

只有需要管道、重定向、变量展开或 `&&` 等 shell 语法时才使用：

```bash
pyr exec --name report --shell "python eval.py > metrics.txt"
```

### 环境变量

`--env KEY=VALUE` 可重复，值会保存到任务元数据并在运行时注入：

```bash
pyr exec --name gpu0 \
  --env CUDA_VISIBLE_DEVICES=0 \
  --env PYTHONUNBUFFERED=1 \
  -- python train.py
```

### 前台与 detach

默认情况下，`exec` 会跟随日志并等待结果。任务成功返回 `0`，任务失败返回 `1`。

```bash
pyr exec --name smoke -- python smoke.py
```

长任务使用 `--detach`。Pyruns 会在隐藏 runner 接受任务后返回：

```bash
pyr exec --name train --detach -- python train.py --epochs 100
pyr -w shell wait train
```

## 6. 添加配置任务：`add`

`add` 只用于 script workspace。它读取 YAML、展开 batch 语法，并把每个配置保存成独立任务快照：

```bash
pyr -w train add configs/quick.yaml
pyr -w train add configs/sweep.yaml --name ablation
```

它完全非交互，不打开编辑器。成功后逐行输出实际创建的任务名；`--json` 下输出任务对象数组。

## 7. 运行现有任务：`run`

按精确任务名运行：

```bash
pyr -w train run baseline
pyr -w train run seed1 seed2 seed3 --workers 3
```

执行模式可选 `thread` 或 `process`：

```bash
pyr -w train run seed1 seed2 --workers 2 --mode process
```

也可以创建并立即运行：

```bash
pyr -w train run --from configs/quick.yaml --name quick
```

先预览 YAML 展开后的任务数量、候选名称和并发参数：

```bash
pyr -w train run --from configs/quick.yaml --name quick --dry-run
pyr --json -w train run --from configs/quick.yaml --name quick --dry-run
```

这里的 `--dry-run` 只适用于 `run --from CONFIG`；它会读取并验证 YAML，但不会创建或运行任务。`run EXISTING --dry-run` 会作为用法错误拒绝，避免让“预览重跑”产生含糊语义。

约束如下：

- 不能同时传位置任务名和 `--from`。
- `--name` 只与 `--from` 一起使用。
- 默认等待所有任务进入最终状态。
- 任一任务失败，批量命令返回 `1`。
- `--detach` 在 runner 接受全部任务后返回。

## 8. 查询：`ls`、`status`、`show`

列出任务：

```bash
pyr -w train ls
pyr -w train ls loss
pyr -w train ls --status failed --limit 20
pyr -w train ls --status running --status queued
pyr -w train ls --sort name --reverse
pyr -w train ls --trash
```

查看工作区汇总：

```bash
pyr -w train status
pyr --json -w train status
```

查看一个精确任务：

```bash
pyr -w train show baseline
pyr --json -w train show baseline
pyr -w train show baseline@2
```

`show` 包含任务目录、payload、run index、PID、最新日志、配置、环境变量、备注和加载错误。`TASK@RUN` 额外选择一个历史运行，并显示该次运行的开始时间、结束时间、PID 和日志路径。

## 9. 日志：`log`

打印最新一次运行日志：

```bash
pyr -w train log baseline
```

持续跟随当前运行：

```bash
pyr -w train log baseline --follow
```

查看历史 run 或只获取路径：

```bash
pyr -w train log baseline@2
pyr -w train log baseline --run 2
pyr -w train log baseline --path
pyr --json -w train log baseline@2 --path
pyr --json -w train log baseline --path
```

`TASK@RUN` 是 `show` 和 `log` 的历史运行短语法，等价于 `log TASK --run RUN`。`RUN` 必须是已有的正整数运行编号；`TASK@RUN` 不能再和 `--run` 或 `--follow` 组合。`@` 因此是保留分隔符，不能用于新任务名。

`log` 没有全屏交互查看器。`log -f` 只是持续向 stdout 输出字节，并不是交互终端。原始日志模式不能与 `--json` 混用；需要机器可读数据时先取 `--path`。

## 10. 等待和停止：`wait`、`stop`

等待已有活动任务：

```bash
pyr -w train wait baseline
pyr -w train wait seed1 seed2 --timeout 600
```

`timeout=0` 表示无限等待。pending 任务尚未交给 runner，因此 `wait` 会拒绝它。

向真正拥有任务的 runner 请求取消：

```bash
pyr -w train stop baseline
pyr -w train stop seed1 seed2 --timeout 15
```

取消请求写入任务元数据，拥有任务的 runner 读取请求并终止对应任务，而不是让另一个 CLI 进程假装拥有它。成功停止后的终态是 `cancelled`，不会再与真正的执行失败 `failed` 混在一起；取消后的任务仍可用 `run TASK` 重跑。

## 11. 生命周期：`rm`、`restore`、`mv`、`pin`

```bash
pyr -w train mv baseline baseline-lr1e3
pyr -w train pin baseline-lr1e3
pyr -w train pin baseline-lr1e3 --off
pyr -w train rm baseline-lr1e3
pyr -w train ls --trash
pyr -w train restore baseline-lr1e3
```

`rm` 不显示确认提示，直接把任务移动到 workspace trash，并不永久删除。脚本、CI、AI agent 和人使用完全相同的行为。

## 12. 导出：`export`

默认把 CSV 写 stdout：

```bash
pyr -w train export
pyr -w train export --format csv
```

选择任务、状态、格式和文件：

```bash
pyr -w train export baseline --format json
pyr -w train export --status completed --output reports/results.csv
pyr -w train export --format json --output -
```

这里的 `--output -` 明确表示 stdout。

## 13. 配置：`config`

```bash
pyr -w train config list
pyr -w train config get manager_max_workers
pyr -w train config set manager_max_workers 4
pyr -w train config unset manager_max_workers
pyr -w train config path
```

`config set` 将值解析为 YAML，并根据已知配置项的类型验证。未知 key 或类型错误不会静默写入。

## 14. 系统快照：`metrics`

`metrics` 不需要工作区：

```bash
pyr metrics
pyr --json metrics
```

它输出一次 CPU、内存和 GPU 快照并退出，不进入持续刷新的仪表盘。

## 15. 显式 Web 入口：`ui`、`dev`

```bash
pyr ui                                  # 打开 workspace launcher
pyr ui train.py                         # 打开 script workspace
pyr ui train.py --config config.yaml    # 导入模板后打开
pyr ui --shell                          # 打开当前项目 shell workspace
pyr ui --shell --no-browser             # headless server
pyr dev train.py                        # 热更新开发模式
```

`ui` 和 `dev` 才负责启动 Web 服务。裸 `pyr` 与 `pyruns` 永远只显示帮助。

## 16. JSON 契约

推荐自动化调用：

```bash
pyr --json -w shell ls
pyr --json -w shell status
pyr --json -w shell show smoke
pyr --json -w shell log smoke --path
pyr --json -w shell config list
pyr --json metrics
```

JSON 只写 stdout；用户可读错误仍写 stderr，并由退出码表示成功或失败。不要通过解析彩色表格判断状态。

## 17. 退出码

```text
0    命令成功，且请求等待的任务全部成功
1    工作区、目标、运行时或任务失败
2    命令行用法错误
130  等待或跟随日志时被中断
```

例如，批量 `run` 中一个任务成功、一个失败，整体仍返回 `1`；这使 CI 无需额外解析输出。

## 18. AI / CI 最小协议

```bash
pyr init
pyr exec --name env-check -- python -V
pyr exec --name smoke --detach -- python train.py --epochs 1
pyr --json -w shell show smoke
pyr -w shell wait smoke --timeout 600
pyr -w shell log smoke
```

自动化原则：

1. 始终传完整的一次性命令。
2. 多工作区项目始终显式传 `-w`。
3. 始终使用精确任务名。
4. 结构化查询优先 `--json`。
5. 长任务使用 `--detach`，随后用 `wait`、`show`、`log`。
6. `rm` 直接执行软删除，不等待确认。
7. 不尝试进入、识别或驱动任何交互提示符，因为 CLI 没有交互终端模式。

## 19. 磁盘结构

```text
<project>/_pyruns_/
├── _pyruns_settings.yaml
├── _shell_/
│   ├── script_info.json
│   └── tasks/<task>/
│       ├── task_info.json
│       ├── config.ps1 | config.cmd | config.sh
│       └── run_logs/runN.log
└── <script>/
    ├── script_info.json
    ├── config_default.yaml
    └── tasks/<task>/
        ├── task_info.json
        ├── config.yaml
        └── run_logs/runN.log
```

CLI 和 Web UI 读取同一份磁盘状态，因此可以用 CLI 提交任务，再在 UI 中观察；也可以在 UI 创建任务后用 CLI 查询、等待、取消和导出。
