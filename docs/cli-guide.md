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
- `--detach` 只改变等待方式，不改变任务语义；它不能和 `--dry-run` 同时使用。
- `rm` 立即执行且不确认，但只是可恢复的软删除。
- Web UI 只能由 `pyr ui` / `pyruns ui` 或对应的 `dev` 命令显式启动。
- Windows 后台 runner、任务进程和环境探测不会创建额外控制台窗口。

### 输出属于接口的一部分

- 正常数据写 stdout。
- 进度提示和错误写 stderr。
- 用法错误返回 `2`。
- `--json` 提供严格、版本化的机器输出，只能放在明确支持它的子命令后；顶层包含 `schema_version: 1`。
- `log` 默认原样输出日志，不混入表格或装饰文本。

## 2. 全局选项

```text
-C, --directory PATH              从 PATH 目录执行
-w, --workspace NAME|PATH|SCRIPT  精确选择工作区
--debug                           内部异常时显示 traceback
--version                         输出版本并退出
```

项目上下文选项放在子命令前；需要稳定机器输出时，在支持的命令后加 `--json`：

```bash
pyr -w train ls --json
pyr -C D:/work/project -w shell status
```

每个子命令都有独立帮助：

```bash
pyr help -a
pyr help exec
pyr help run
pyr run --help
```

`pyr help COMMAND` 与 `pyr COMMAND --help` 等价。每份命令帮助都包含用途、参数、
典型示例和关键注意事项；其中 `pyr help exec` 是选择精确 argv、Shell 表达式、
现有脚本、环境变量和后续任务操作的完整决策指南。帮助命令只读，不会创建工作区。

默认帮助只列 `init exec add run ls status show log wait stop ui` 十一个日常命令，高级命令仍可直接执行；需要浏览全部命令时使用 `help -a`。

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

这里的 `--` 是 CLI 参数分隔符：它表示 Pyruns 的 `exec` 选项到此结束，后面的每一项都是目标命令的独立 argv。`--` 不会启动 shell 解析，所以管道、重定向、`$VAR`、通配符和 `&&` 都不会被 Pyruns 展开。Pyruns 会为每个参数做当前平台所需的转义，再把任务保存为 `config.ps1`、`config.cmd` 或 `config.sh`。参数里的空格和短横线不会被误认为 Pyruns 自己的选项。

### 无副作用预览

在真正创建 workspace、task 或启动 runner 前，可以查看完整执行计划：

```bash
pyr exec --dry-run --name smoke -- python -V
pyr exec --dry-run --name smoke --json -- python -V
```

计划会说明目标 workspace 是否需要创建、任务名是否可精确使用、工作目录、argv 或 shell 表达式、解释器和环境变量。`--dry-run` 不创建 `_pyruns_`、设置文件或任务目录，也不会执行用户命令。它与 `--detach` 互斥，因为预览不会产生可供后台接管的任务。显式任务名已存在时仍会像真实执行一样报错；省略 `--name` 且默认名称冲突时，计划会说明真实执行需要生成唯一后缀。

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

脚本路径可以包含空格。路径之后如果还有内容，它们是脚本自己的参数，不是 Pyruns 参数，并会按独立 argv 传递。文件不存在、平台不支持或解释器不可用时，`exec` 会在创建任务前给出明确错误。任务保存的是解释器调用与原脚本绝对路径，不复制脚本本身；每次运行会记录 stdout/stderr 日志、开始/结束时间、高精度运行时长、原始退出码、脚本内容哈希和所在 Git 状态。因此 `pyr exec -n setup -- ./xxx.sh` 是 `bash xxx.sh` 最直接的受跟踪替代，重跑也会保留脚本原目录语义并识别源文件变化。

`--detach`、`run`、`wait`、`stop`、`show` 和 `log` 对这类任务没有特殊规则，和普通 Shell 任务完全一致：

```bash
pyr exec --name setup --detach -- ./scripts/setup.sh
pyr -w shell wait setup
pyr -w shell run setup
pyr -w shell log setup
```

### Shell command string：`-c`

`--` 只是标准参数分隔符。只有需要管道、重定向、变量展开、通配符或 `&&` 等 shell 语法时，才改用与 `sh -c` 习惯一致的 `-c` / `--command`：

```bash
pyr exec --name report -c "python eval.py > metrics.txt"
pyr exec --name pipeline -c "python preprocess.py && python train.py | tee train.log"
```

`-c` 后面必须只有一个完整 command string，因此外层引号不能省略；`-c echo hello` 会被拒绝。字符串由工作区解析到的 shell 执行，引用规则和可用命令可能因 Bash、PowerShell 与 cmd.exe 而不同。普通程序或脚本文件不需要这些能力时，使用 `--` 后的精确 argv。

### 环境变量

少量变量用 `-e` / `--env` 后接一个或多个 `KEY=VALUE`。`--` 是明确边界，所以多个变量不会吞掉目标命令；`-e` 也可以按变量组重复使用：

```bash
pyr exec --name gpu0 -e CUDA_VISIBLE_DEVICES=0 TOKENIZERS_PARALLELISM=false SEED=42 -- python train.py
```

Pyruns 已自动为子进程设置 `PYTHONUNBUFFERED=1`、`PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`，一般无需再写入任务环境。

在 Bash/sh 等 POSIX shell 中，`CUDA_VISIBLE_DEVICES=0 pyr exec ...` 对当前这次运行通常有同样的注入效果，因为 runner 和任务进程会继承调用端环境。但它不会保存到任务元数据；换终端、从 Web UI 启动或稍后执行 `pyr run` 时可能不同。需要稳定重跑和 `show` 可见时使用 `-e` 或 `--env-file`。

变量较多时使用可重复的 `--env-file PATH`。文件是 UTF-8 文本，只接受空行、整行 `#` 注释和 `KEY=VALUE`：

```dotenv
# .env.train
CUDA_VISIBLE_DEVICES=0
TOKENIZERS_PARALLELISM=false
```

```bash
pyr exec --name gpu0 --env-file .env.train -e SEED=42 -- python train.py
```

合并顺序是：前面的 env 文件 < 后面的 env 文件 < 命令行 `-e`。文件不会执行 `export`、变量插值、命令替换或行内注释；`VALUE` 可以包含额外的 `=`。所有任务环境值都会明文保存到 `task_info.json` 并由 `show` 显示，不要放入密码、token 或其他密钥。

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
pyr -w train run seed1 seed2 seed3 --jobs 3
```

执行模式可选 `thread` 或 `process`：

```bash
pyr -w train run seed1 seed2 --jobs 2 --backend process
```

也可以创建并立即运行：

```bash
pyr -w train run --config configs/quick.yaml --name quick
```

先预览 YAML 展开后的任务数量、候选名称和并发参数：

```bash
pyr -w train run --config configs/quick.yaml --name quick --dry-run
pyr -w train run --config configs/quick.yaml --name quick --dry-run --json
```

这里的 `--dry-run` 只适用于 `run --config CONFIG`；它会读取并验证 YAML，但不会创建或运行任务。`run EXISTING --dry-run` 会作为用法错误拒绝，避免让“预览重跑”产生含糊语义。

约束如下：

- 不能同时传位置任务名和 `--config`。
- `--name` 只与 `--config` 一起使用。
- 默认等待所有任务进入最终状态。
- 任一任务失败，批量命令返回 `1`。
- `--detach` 在 runner 接受全部任务后返回。
- `-j/--jobs` 不会超过实际选择的任务数量。
- runner 只接收部分任务时会列出 `claimed` / `unclaimed` 名称并返回 `1`，不会伪报整批成功。

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

正常任务列表会显示 `PIN` 标记，并在 JSON 摘要中提供 `pinned` 布尔值。置顶任务在所有排序方式下始终位于普通任务之前；`--reverse` 只反转置顶组和普通组各自内部的顺序。

查看工作区汇总：

```bash
pyr -w train status
pyr -w train status --json
```

查看一个精确任务：

```bash
pyr -w train show baseline
pyr -w train show baseline --json
pyr -w train show baseline@2
pyr -w train show baseline --run 2
```

`show` 包含置顶状态、任务目录、payload、run index、PID、最新日志、配置、环境变量、备注和加载错误。`show --json` 还提供对齐的运行时长、退出码、源码状态、record 和 track 历史。`TASK@RUN` 与 `TASK --run RUN` 都可选择一个历史运行，并显示该次运行的开始时间、结束时间、时长、原始退出码、PID、源码状态、record、track 和日志路径。

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
pyr -w train log baseline@2 --path --json
pyr -w train log baseline --path --json
```

`TASK@RUN` 是 `show` 和 `log` 的历史运行短语法，等价于 `TASK --run RUN`。`RUN` 必须是已有的正整数运行编号；`TASK@RUN` 不能再和 `--run` 组合，历史日志也不能 `--follow`。`@` 因此是保留分隔符，不能用于新任务名。

`log` 没有全屏交互查看器。`log -f` 只是持续向 stdout 输出字节，并不是交互终端。原始日志模式不能与 `--json` 混用；需要机器可读数据时先取 `--path`。日志尚不存在时 `log --path` 也返回失败，不会输出一个虚构的未来文件路径。

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

取消请求写入任务元数据，拥有任务的 runner 读取请求并终止对应任务，而不是让另一个 CLI 进程假装拥有它。成功停止后的终态是 `cancelled`，不会再与真正的执行失败 `failed` 混在一起；取消后的任务仍可用 `run TASK` 重跑。`stop --timeout 0` 表示无限等待。

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

这里的 `--output -` 明确表示 stdout。记录格式只由 `--format` 选择，输出文件名后缀不会隐式改变格式。
CSV 与 JSON 使用相同语义：每个任务的每次运行各占一条记录，并附加该次运行可用的 monitor 指标；即使任务没有写入 monitor 指标，运行时间、退出码和 PID 等基础记录也仍会导出。

## 13. 配置：`config`

```bash
pyr config list
pyr config get monitor_scrollback
pyr config set monitor_scrollback 200000
pyr config unset monitor_scrollback
pyr config path
```

`config set` 将值解析为 YAML，并根据已知配置项的类型验证。未知 key 或类型错误不会静默写入。
批量运行的并发数与后端不是项目配置；每次用 `run -j/--jobs --backend` 显式指定。

## 14. 系统快照：`metrics`

`metrics` 不需要工作区：

```bash
pyr metrics
pyr metrics --json
```

它输出一次 CPU、内存和 GPU 快照并退出，不进入持续刷新的仪表盘。

## 15. 显式 Web 入口：`ui`、`dev`

```bash
pyr ui                                  # 打开 workspace launcher
pyr ui train.py                         # 打开 script workspace
pyr ui train.py --config config.yaml    # 导入模板后打开
pyr ui shell                            # 打开当前项目 shell workspace
pyr ui shell --no-browser               # headless server
pyr dev train.py                        # 热更新开发模式
```

UI 只监听本机回环地址，并为每次启动生成新的随机访问令牌。自动打开浏览器时会完成
令牌到 `HttpOnly` 会话 cookie 的交换；使用 `--no-browser` 时必须复制终端打印的完整 URL。
不要共享该 URL；Web UI 不是远程多用户服务。

`ui` 和 `dev` 才负责启动 Web 服务。裸 `pyr` 与 `pyruns` 永远只显示帮助。UI 的目标直接写在 `ui` 后面；`-w` 只用于 `ls`、`run`、`show` 等任务工作区命令。长时间运行的 UI 命令不接受 `--json`。

## 16. JSON 契约

推荐自动化调用：

```bash
pyr -w shell ls --json
pyr -w shell status --json
pyr -w shell show smoke --json
pyr -w shell log smoke --path --json
pyr config list --json
pyr metrics --json
```

JSON 只写 stdout；用户可读错误仍写 stderr，并由退出码表示成功或失败。YAML 日期和时间戳转换为 ISO 8601 字符串；不支持的对象、NaN 和 Infinity 会明确失败，不会被任意字符串化。不要通过解析彩色表格判断状态。

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
pyr -w shell show smoke --json
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
