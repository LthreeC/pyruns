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

命令示例以 Linux/Bash 为主；其他平台请按实际 Shell 调整引用、变量、续行、路径和标准输入
写法，见[跨平台差异](#exec-platform-notes)。

## 1. 设计约定

### 一次调用，一次操作

每个命令执行完都会退出。人、Shell 脚本、CI 和 AI agent 使用的是同一套接口，不需要识别提示符或维护会话状态。

### 默认行为必须安全且可预测

- 裸 `pyr`、`pyruns` 或 `pyr help` 列出全部命令及用途和快速示例；命令选项统一用 `pyr COMMAND --help` 查看。
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
pyr -C /home/user/work/project -w shell status
```

每个子命令都有独立帮助：

```bash
pyr exec --help
pyr run --help
```

命令帮助统一从 `pyr COMMAND --help` 进入。
每份命令帮助都包含用途、参数、典型示例和关键注意事项；其中 `pyr exec --help` 是选择
`-c` 命令字符串、`--stdin` 多行脚本、环境变量和后续任务操作的完整决策指南。帮助命令
只读，不会创建工作区。

默认帮助列出全部命令及一句用途说明，包括恢复、导出和项目配置；`help -a` 仍兼容，并额外显示命令分组摘要。

## 3. 工作区发现

Pyruns 从当前目录开始向父目录查找最近的 `_pyruns_`。找到后按以下规则选择工作区：

1. 显式传入 `-w` 时，只使用该精确选择。
2. 项目只有一个工作区时，自动选择它。
3. 项目存在多个工作区时，拒绝猜测并要求 `-w`。
4. 没找到项目时，提示先执行 `pyr init`。

上述向上查找规则用于查询和管理已有任务。`exec` 默认固定使用 `<当前目录>/_pyruns_/_shell_`，不会复用父目录的 shell workspace；`-C PATH` 可以显式改变这个当前目录。只有传入 workspace 的绝对或相对路径时，`exec` 才会写入其他位置。

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

`exec` 创建并立即运行一个新的 **Shell 类型任务**，目标可以是 Bash 脚本、Python 程序
或其他可执行命令。通过 `exec` 跑 `python train.py`，保存的仍是 Shell workspace 中的
命令任务。

`run` 用于运行已有任务，支持 Shell 任务和 Python 配置任务；还可用 `--config` 在
Python 脚本工作区批量生成并运行任务。因此 `exec` 与 `run` 不是按 Shell/Python 语言划分：

```bash
pyr exec -n train -c "python train.py"  # 创建一个命令任务并运行
pyr -w shell run train                    # 重跑这个命令任务
pyr init train.py --config default.yaml   # 创建 Python 参数配置工作区
pyr -w train run --config sweep.yaml      # 从配置生成并运行 Python 任务
```

### 任务命名

`exec` 不要求手动命名。省略名称时，每次会以当前本地时间生成任务名：

```bash
pyr exec -c "python -V"  # task_YYYY-MM-DD_HH-MM-SS
```

需要可读前缀并希望自动区分每次执行时，使用 `-nt` / `--name-timestamp`：

```bash
pyr exec -nt smoke -c "python -V"  # smoke_YYYY-MM-DD_HH-MM-SS
```

需要之后用固定名称执行 `show`、`log` 或 `run` 时，继续使用 `-n` / `--name`：

```bash
pyr exec -n smoke -c "python -V"  # 精确名称 smoke
```

`-n` 与 `-nt` 互斥。自动名称若在同一秒冲突，会安全追加唯一后缀；精确名称冲突仍直接报错，
不会覆盖已有任务。

任务启动时，Pyruns 会先把 START 横幅和 Source 状态写入 run log，再转存命令的 stdout/stderr，确保框架元数据不会夹在命令输出中间。Source 采集与子进程并行；采集期间输出线程持续排空 stdout/stderr，并使用超过阈值后自动落盘的临时缓冲，因此不需要人为休眠，也不会因等待 Source 而堵塞子进程。

`exec` 新建任务只接受 `-c "完整命令"` 或 `--stdin`。普通程序的参数放在命令字符串内；
需要记录长命令的全部正文、变量赋值、引号和换行时，使用 `--stdin`。

### 无副作用预览

在真正创建 workspace、task 或启动 runner 前，可以查看完整执行计划：

```bash
pyr exec --dry-run --name smoke -c "python -V"
pyr exec --dry-run --name smoke --json -c "python -V"
```

计划会说明目标 workspace 是否需要创建、任务名是否可精确使用、工作目录、Shell 命令、解释器和环境变量。`--dry-run` 不创建 `_pyruns_`、设置文件或任务目录，也不会执行用户命令。它与 `--detach` 互斥，因为预览不会产生可供后台接管的任务。显式任务名已存在时仍会像真实执行一样报错；自动时间戳名称冲突时，计划会说明真实执行需要生成唯一后缀。

### 直接运行 Shell 脚本文件

执行已有脚本时，把解释器和脚本路径一起放入 `-c` 命令字符串：

```bash
pyr exec --name setup -c "bash ./scripts/setup.sh"
```

路径中的空格应按工作区 Shell 规则引用；脚本自己的参数也放在命令字符串内。
任务保存的是完整调用命令和创建时的工作目录，重跑会重新读取原脚本文件。
需要保存提交时的脚本正文时使用 `--stdin`，源文件之后的修改不会影响重跑。
每次运行都会记录 stdout/stderr 日志、开始/结束时间、运行时长和原始退出码。

`--detach`、`run`、`wait`、`stop`、`show` 和 `log` 对这类任务没有特殊规则，和普通 Shell 任务完全一致：

```bash
pyr exec --name setup --detach -c "bash ./scripts/setup.sh"
pyr -w shell wait setup
pyr -w shell run setup
pyr -w shell log setup
```

### Shell command string：`-c`

短命令和管道、重定向、变量展开、通配符或 `&&` 等 Shell 表达式，可使用
`-c` / `--command`，将完整命令作为一个带引号的参数传入：

```bash
pyr exec --name report -c "python eval.py > metrics.txt"
pyr exec --name pipeline -c "python preprocess.py && python train.py | tee train.log"
pyr exec -c "python train.py --epochs 10" -n train -d
```

命令被完整引用为一个字符串时，Pyruns 选项可以放在 `-c "命令"` 前面或后面；目标程序
的选项放在引号内。`-c` 是 `exec` 的选项，因此仍需写在 `exec` 后面。
为兼容旧用法，`-c echo hello` 会将后续文本合并成 `echo hello`，此时所有 Pyruns 选项
应放在 `-c` 前。推荐始终引用完整命令。表达式含 `;`、`|`、重定向或变量时，要按调用端 Shell
的规则引用整段，否则调用端会在 Pyruns 启动前先拆分它。表达式由工作区解析到的 Shell
执行，引用规则和可用命令可能因 Bash、PowerShell 与 cmd.exe 而不同。单引号和双引号由
调用端解析；反引号在 Bash 中执行命令替换，在 PowerShell 中用于转义，不是跨平台的
脚本包裹符。复杂或多行命令使用 `--stdin`。

### 通过标准输入提交多行脚本

需要把长脚本的全部正文记录成一个任务时，推荐先保存为 `pipeline.sh`，然后在 Bash
终端执行下面第一条命令。`--stdin` 保存完整正文，`-nt pipeline` 生成带时间戳的任务名，
`-d` 提交后返回；去掉 `-d` 就会跟随日志并等待结果。重跑使用提交时保存的正文。
也可以通过管道或 heredoc 提交；`exec` 没有交互式脚本录入模式：

```bash
pyr exec -nt pipeline -d --stdin < pipeline.sh
pyr exec -n pipeline -d --stdin <<'BASH'
#!/usr/bin/env bash
set -euo pipefail

REPO_DIR=${TRAIN_REPO_DIR:-"$PWD"}
CONFIG="$REPO_DIR/configs/train.yaml"
export CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES-0,1}"
cd "$REPO_DIR"

python validate.py --config "$CONFIG"
python train.py --config "$CONFIG" \
  --epochs 10 --lr 0.001
BASH
```

这里的 `0,1` 只在启动环境未设置 `CUDA_VISIBLE_DEVICES` 时生效。通过 `-e` 传入的卡号、调度器分配的卡号，以及显式设置的空值都会保留。

需要完整记录命令中的显式参数、变量赋值、引号、续行和换行时，优先使用 `--stdin`，
尤其适合由自动化程序或 AI agent 提交长命令。它保存收到的脚本文本，不会自动保存运行时
展开后的实参、外部配置文件内容或调用端隐式继承的环境变量。重跑需要的变量请在脚本内
显式赋值/export，或通过 `-e` / `--env-file` 保存。

Pyruns 先读取完整的 UTF-8 文本，再保存为任务 Shell 脚本并交给 runner；后台运行和重跑
都使用保存的正文。`<<'BASH'` 的引号让调用端保留 `$变量`、引号、反斜杠和正文换行，
到任务执行时才展开；结束标记 `BASH` 必须独占一行。也可先用
`pyr exec -n pipeline --dry-run --json --stdin < pipeline.sh` 检查执行计划，预览不会创建
工作区、任务或进程。

`--stdin` 使用与 `-c` 相同的工作区 Shell，并保存解析到的解释器；脚本首行的 shebang
不会覆盖这个设置。Bash 脚本应从 Bash 终端提交，或将工作区 Shell 配置为 Bash。输入
支持 UTF-8 BOM，CRLF 会统一为 LF，其他正文空格与空行保留；大小上限与任务脚本一致，
为 4 MiB。`--stdin` 不能与 `-c` 同用，且在终端直接运行时会
报错，必须提供重定向、管道或 heredoc。任务进程不继承调用端的标准输入。

标准输入是常见的 CLI 约定：[Git `commit --file -`](https://git-scm.com/docs/git-commit)
从 stdin 读取提交正文；[Docker build](https://docs.docker.com/build/building/context/#empty-context)
支持从重定向或 heredoc 读取 Dockerfile。Pyruns 的 `--stdin` 使用同一输入方式，额外将
脚本文本保存到任务内。

### 调用端引用规则

所有平台都遵循同一命令契约：普通程序、变量、管道、分号、重定向、通配符和命令链
放在一个引用后的 `-c` 字符串中，或通过 `--stdin` 提交完整脚本。

Bash 示例：

```bash
pyr exec -c 'printf "%s\n" "$HOME" | sed "s/home/HOME/"'
```

这是调用端 shell 的固有限制：它会在启动 `pyr` 前解析自己的控制符，因此外部 CLI 无法在启动后找回未引用的剩余文本。颜色捕获使用伪终端：Linux/macOS 通过系统 PTY，Windows 在已有控制台时通过原生 ConPTY。没有已附着的 Windows 控制台时，会回退到隐藏管道，避免弹出控制台窗口，此时颜色可能丢失。伪终端捕获的 SGR 颜色进入 `runN.log`，清屏、光标定位和窗口标题等界面控制序列会被过滤。

命令由工作区 Shell 执行，可直接使用该 Shell 的内建命令。
命令失败时保留 Bash、PowerShell 或 cmd 自己产生的完整错误。

### 其他平台的操作差异 {#exec-platform-notes}

Pyruns 的选项相同，脚本语法由实际执行的 Shell 决定；默认跟随调用端 Shell。
其他平台应选择与脚本匹配的解释器，并按该 Shell 的规则引用命令和路径。

| 写法 | Bash | PowerShell | cmd |
| --- | --- | --- | --- |
| 读取环境变量 | `$NAME` | `$env:NAME` | `%NAME%` |
| 将完整命令作为一个参数传给 `-c` | 单引号或双引号 | 单引号或双引号 | 双引号；单引号是普通字符 |
| 行末续行符 | `\` | 反引号 | `^` |
| 成功后执行下一条命令 | `&&` | PowerShell 7+ 支持 `&&`，Windows PowerShell 5.1 不支持 | `&&` |

`< pipeline.sh` 和 heredoc 是 Bash 用法。PowerShell 使用管道或单引号 here-string；
记录已有脚本可读取整个 UTF-8 文件：

```powershell
Get-Content -Raw -Encoding UTF8 .\pipeline.ps1 |
  pyr exec -nt pipeline -d --stdin
```

Windows PowerShell 5.1 提交前需设置 UTF-8 管道编码：
`$OutputEncoding = [System.Text.UTF8Encoding]::new($false)`。
WSL 使用 Linux 路径和程序，例如 Windows 的 `D:\data` 在默认挂载下通常是 `/mnt/d/data`。

多步脚本需自行控制遇错停止；Pyruns 按最终进程退出码判断结果。Bash 示例使用
`set -euo pipefail`；PowerShell 可用 `$ErrorActionPreference = 'Stop'` 控制 cmdlet 错误，
外部程序失败时检查 `$LASTEXITCODE` 并 `exit $LASTEXITCODE`；cmd 需检查 `errorlevel` 并退出。

### 环境变量

直接运行 `pyr exec` 时，脚本继承调用终端的环境，在脚本中执行的赋值对后续命令生效。网页侧栏的 Runtime Env 只用于网页提交。用 `pyr run` 重跑已有任务时，会在当前终端环境上应用该任务保存的 Env。

少量变量用 `-e` / `--env` 后接一个或多个 `KEY=VALUE`，再用 `-c` 提供完整命令；
`-e` 也可以按变量组重复使用：

```bash
pyr exec --name gpu0 -e CUDA_VISIBLE_DEVICES=0 TOKENIZERS_PARALLELISM=false SEED=42 -c "python train.py"
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
pyr exec --name gpu0 --env-file .env.train -e SEED=42 -c "python train.py"
```

合并顺序是：前面的 env 文件 < 后面的 env 文件 < 命令行 `-e`。文件不会执行 `export`、变量插值、命令替换或行内注释；`VALUE` 可以包含额外的 `=`。所有任务环境值都会明文保存到 `task_info.json` 并由 `show` 显示，不要放入密码、token 或其他密钥。

### 前台与 detach

默认情况下，`exec` 会跟随日志并等待结果。任务成功返回 `0`，任务失败返回 `1`。

任务结果以启动的命令为准。脚本内部若启动后台作业，应在退出前使用 Shell 的 `wait`
或 Python 的 `join()` 等待它们，并传播失败结果；命令退出后仍存活的后台进程不会延长任务。

```bash
pyr exec --name smoke -c "python smoke.py"
```

此时按 Ctrl+C 会向这次 `exec` 提交的任务请求取消，并在完成清理后返回 `130`。这与单独执行 `wait` 或 `log -f` 不同：后两者只观察已有任务，中断观察不会停止任务。

长任务使用 `--detach`。Pyruns 会在隐藏 runner 接受任务后返回：

```bash
pyr exec --name train --detach -c "python train.py --epochs 100"
pyr -w shell wait train
```

### GPU 空闲等待 / GPU wait scheduler：`--gpu-wait` {#gpu-wait}

这是 CLI 的一次性 GPU wait 参数：它让 `pyr exec` 或 `pyr run` 在满足显存、GPU 空闲率、稳定时间和多卡数量后再启动。要记录完整的多行 Shell 命令，使用 `--stdin`；普通单行命令使用 `-c`。不传 `--gpu-wait` 就直接运行。

`exec` 和 `run` 使用同一条规则，必须完整填写五项；裸数字的单位与 Web UI 一致：

```text
卡数-每卡空闲GiB-最低GPU空闲率%-连续满足秒数-最长等待小时
```

```bash
pyr exec -nt train -d --gpu-wait 2-20-70-15-2 -c "python train.py"
pyr exec -nt pipeline -d --gpu-wait 2-20-70-15-2 --stdin < pipeline.sh
pyr -w shell run pipeline --gpu-wait 1-8-70-15-2
pyr -w train run --config sweep.yaml --gpu-wait 1-8-70-15-2
```

`2-20-70-15-2` 表示需要两张卡，每张至少空闲 20 GiB、GPU 空闲率至少 70%，连续满足
15 秒后开始，最多等待 2 小时。请求的每张卡都必须达标；任意一张不达标就继续等待，
该卡恢复后重新计算连续满足时长。空闲率等于 `100 - GPU 利用率`；70% 空闲对应利用率
不超过 30%。超时仅限制启动前的排队时间，不限制命令执行时长，也不会自动退回 CPU。

CLI 的 `--gpu-wait` 仅对本次运行生效，再次 `run` 时需要重新传入。
不传 `--gpu-wait` 就不进行 GPU 等待，不继承 UI 设置或旧任务中的规则。
UI 的 Runtime 开关只影响之后从 UI 提交的任务。

- 五项都必须提供；`1-8` 等不完整规则会报用法错误。
- 持续时间 `0` 表示一旦满足条件即启动；超时 `0` 表示无限等待。
- `device=0,1-20-70-15-2` 指定两张物理卡的索引。
- 显式单位不区分大小写：`G/GiB`、`M/MiB` 为二进制，`GB/MB` 为十进制；时间接受
  `s/sec`、`m/min`、`h/hr`，例如 `2-20480MiB-70%-15SEC-120MIN`。
- 调度尊重 `CUDA_VISIBLE_DEVICES`，只选择请求的卡数。需要保存可见卡范围时使用 `-e`。
  每张卡在同一工作区只分配给一个 Pyruns 任务，外部任务通过实时指标检测。

```bash
pyr -w shell show pipeline --json   # 本次规则、当前等待原因与时间
pyr -w shell log pipeline -f        # 跟随排队和执行日志
pyr metrics                        # 当前 GPU 指标
pyr ui shell                       # GPU 设置和任务监控面板
```

UI 使用 `GPUs required`、`Minimum free memory (GiB)`、`Minimum GPU idle (%)`、
`Stable for (sec)`、`Maximum wait (hr)`。旧配置的最大利用率会迁移为最低空闲率；
显存占用百分比和每卡任务数已停用。UI 的最小空闲显存默认值为 `8 GiB`，可在 Runtime 中按任务需要调整。

等待输出会逐项对照当前值与要求，显示 `ready` / `blocked` 状态及具体阻塞原因：

| 规则字段 | 等待时查看什么 | 如何判断 |
| --- | --- | --- |
| 卡数 | 已就绪卡数 / 所需卡数 | 所需数量的 GPU 必须同时满足全部条件，并且可分配；`candidates` 只表示候选卡数 |
| 每卡空闲 GiB | 每张卡的 `free memory` 当前值与最低要求 | 每张所需卡都必须达到显存要求 |
| 最低 GPU 空闲率 % | 每张卡的 `idle` 当前值与最低要求 | `idle = 100 - utilization`；利用率 93% 就是空闲率 7%，要求空闲率 100% 则必须是利用率 0% |
| 连续满足秒数 | `stable` 已连续达标秒数 / 要求秒数 | 达到显存和空闲率要求后累计；阈值失守、指标缺失或采样中断时重置 |
| 最长等待小时 | `Wait` 已排队时长 / 超时上限及剩余时间 | `ready` 表示尚未超时；到期仍未就绪则超时，不限制任务执行时长；`0` 表示无限等待 |

未采集到 GPU 指标、不在 `CUDA_VISIBLE_DEVICES` 可见范围内，或已被同一工作区的其他任务预留，
都会显示对应原因。缺失的指标不会当成空闲；同一张卡的多个阻塞项会一起显示。
达到显存要求但空闲率不足，或两项均达标但 `stable` 时间不足，都仍需等待。
被其他任务预留的 GPU 可以显示 `stable 15/15 s [ready]`，同时因无法分配而显示 `blocked`。

在终端中，完整等待状态会原位换行更新；重定向到文件或管道时保留普通文本，便于记录和查看。

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

`-j/--jobs` 只控制同时运行的任务数。Pyruns 用线程协调这些任务，每个任务仍在
独立子进程中执行，因此 Python 训练、Shell 命令和外部程序都可以正常并行：

```bash
pyr -w train run seed1 seed2 --jobs 2
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
- 前台等待时按 Ctrl+C，会向这次 `run` 接受的任务请求取消并返回 `130`。
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

`QUERY` 不区分大小写，匹配任务名、备注以及保存的配置或命令正文；`--trash` 仅匹配名称。需要搜索日志内容时使用 Web UI 检索。

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

Python 任务使用启动时捕获的配置，并保存该次配置、工作目录、环境覆盖值与启动参数。Bash 任务直接执行当前的 `config.sh`。
`show TASK@RUN --json` 的 `selected_run.input` 返回该次快照，也支持旧 Shell 运行保存的快照。Shell 新运行及其他缺少快照的记录返回 `null`。

```bash
pyr -w train diff baseline@1 baseline@2
pyr -w train diff baseline@1 candidate@1 --json
```

`diff` 比较同一工作区内两次运行的输入，成功比较返回 `0`（包含有差异的情况），缺少快照返回 `1`。
展示最多 256 KiB 或 4000 行；截断时终端会提示，JSON 返回 `truncated=true`，完整输入仍可通过 `show TASK@RUN --json` 读取。
快照不复制外部脚本、数据集或全部继承环境；`-c "bash train.sh"` 记录该命令，`--stdin < train.sh` 才保存脚本正文。

## 9. 日志：`log`

打印最新一次运行日志：

```bash
pyr -w train log baseline
```

持续跟随当前运行：

```bash
pyr -w train log baseline --follow
pyr -w train log baseline --tail 100 -f
pyr -w train log baseline --tail 0 -f
```

`--tail N` 只显示最后 N 行；配合 `-f` 会接着输出新增内容，`--tail 0 -f` 仅看新增内容。
默认仍输出完整日志。历史 run 也支持 `--tail`，但不能与 `--path` 组合。

查看历史 run 或只获取路径：

```bash
pyr -w train log baseline@2
pyr -w train log baseline --run 2
pyr -w train log baseline --path
pyr -w train log baseline@2 --path --json
pyr -w train log baseline --path --json
```

`TASK@RUN` 是 `show` 和 `log` 的历史运行短语法，等价于 `TASK --run RUN`。`RUN` 必须是已有的正整数运行编号；`TASK@RUN` 不能再和 `--run` 组合，历史日志也不能 `--follow`。`@` 因此是保留分隔符，不能用于新任务名。

`log` 没有全屏交互查看器。`log -f` 只是持续向 stdout 输出字节，并不是交互终端。按 Ctrl+C 只停止跟随并返回 `130`，任务继续运行。原始日志模式不能与 `--json` 混用；需要机器可读数据时先取 `--path`。日志尚不存在时 `log --path` 也返回失败，不会输出一个虚构的未来文件路径。

## 10. 等待和停止：`wait`、`stop`

等待已提交任务，或读取已结束任务的结果：

```bash
pyr -w train wait baseline
pyr -w train wait seed1 seed2 --timeout 600
```

`timeout=0` 表示无限等待。已结束任务立即返回记录的结果。pending 任务尚未交给 runner，因此 `wait` 会拒绝它。正数超时或 Ctrl+C 都只停止等待，不会停止任务；需要取消任务时使用 `stop`。

向真正拥有任务的 runner 请求取消：

```bash
pyr -w train stop baseline
pyr -w train stop seed1 seed2 --timeout 15
```

取消请求写入任务元数据，拥有任务的 runner 读取请求并终止对应任务。成功停止后的终态是 `cancelled`，取消后的任务仍可用 `run TASK` 重跑。`stop --timeout 0` 表示无限等待。

本机 runner 心跳过期时，`stop` 会核验记录中的 PID 和进程创建时间，确认整个进程树退出后才将任务标为 `cancelled`。远程任务或无法核验进程身份的任务会保留停止请求和运行状态，等待所属 runner 处理；等待超时只结束当前等待。

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

选中任务的元数据缺失、损坏或无法读取时，导出会失败并指出任务名称。指定 `--output` 文件时，已有文件会保留，修复任务数据后可重新导出。

## 13. 配置：`config`

```bash
pyr config list
pyr config get monitor_scrollback
pyr config set monitor_scrollback 200000
pyr config set task_storage local
pyr config unset monitor_scrollback
pyr config path
```

`config set` 将值解析为 YAML，并根据已知配置项的类型验证。未知 key 或类型错误不会静默写入。

`task_storage` 控制新任务保存位置。默认 `shared` 写入 `tasks/`；使用 `local` 写入本机专属的同级目录。两种模式下 `pyr ls` 均可查看通用任务和本机任务。
批量运行的并发数不是项目配置；需要时在每次运行中用 `run -j/--jobs` 显式指定。

## 14. 系统快照：`metrics`

`metrics` 不需要工作区：

```bash
pyr metrics
pyr metrics --json
```

它输出一次 CPU、内存和 GPU 快照并退出，不进入持续刷新的仪表盘。

安装或运行异常时可执行一次诊断：

```bash
pyr doctor
pyr doctor --json
pyr -w shell doctor
```

`doctor` 检查依赖及运行模块导入、Shell、ripgrep 和可选 NVIDIA GPU；不需要先初始化项目。
已有工作区使用临时文件检查读写、原子替换和文件锁，不改写任务或配置。缺少 GPU 不判定安装失败；必要检查失败返回 `1`，输出修复提示。

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
令牌到持久 `HttpOnly` 会话 cookie 的交换。会话在使用时续期，并可跨同一主机、端口和
工作区的正常更新或重启保留；清除浏览器数据或本地 `~/.pyruns/sessions` 状态后需要重新
打开最新 URL。使用 `--no-browser` 时必须复制终端打印的完整 URL。不要共享该 URL；
Web UI 不是远程多用户服务。

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
130  命令被 Ctrl+C 中断
```

例如，批量 `run` 中一个任务成功、一个失败，整体仍返回 `1`；这使 CI 无需额外解析输出。

## 18. AI / CI 最小协议

```bash
pyr init
pyr exec --name env-check -c "python -V"
pyr exec --name smoke --detach -c "python train.py --epochs 1"
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
│       ├── config.sh | config.ps1 | config.cmd
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
