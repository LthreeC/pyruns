# pyruns

![logo](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/pyruns_logo2.png)

[English](README-en.md) | 简体中文

[![PyPI version](https://img.shields.io/pypi/v/pyruns.svg)](https://pypi.org/project/pyruns/)
[![Python versions](https://img.shields.io/pypi/pyversions/pyruns.svg)](https://pypi.org/project/pyruns/)
[![License](https://img.shields.io/pypi/l/pyruns.svg)](https://github.com/LthreeC/pyruns/blob/main/LICENSE)
[![Docs](https://img.shields.io/badge/docs-GitHub%20Pages-2563eb.svg)](https://lthreec.github.io/pyruns/)

Pyruns 是一个磁盘优先、面向复现实验和终端任务的运行管理器。它把命令、配置、日志、环境、运行历史和指标保存在项目的 `_pyruns_` 目录中，并提供 Git 式一次性 CLI 与可选 Web UI。

![Generator](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

## 30 秒开始

```bash
pip install pyruns

# 两个正式入口完全等价；下面用短写 pyr
pyr --help
pyruns --help
pyr help -a  # 查看全部命令
pyr help exec  # 查看 --、-c、脚本和环境变量的完整选择指南

# 初始化当前项目的 shell workspace
pyr init

# 运行并记录一个命令
pyr exec -n smoke -- python -V

# 查询结果
pyr -w shell ls
pyr -w shell show smoke
pyr -w shell log smoke
```

长任务使用 `--detach`，随后用独立命令控制：

```bash
pyr exec -n train -d -- python train.py --epochs 100
pyr -w shell status
pyr -w shell wait train
pyr -w shell log train
```

需要 Web UI 时显式启动：

```bash
pyr ui
pyr ui train.py
pyr ui shell
```

`pyr` 与 `pyruns` 是完全等价的正式入口；前者适合高频输入，后者更容易识别项目名。裸命令显示精简帮助，`help -a` 展开全部命令；两者都没有需要持续操控的交互式 REPL。

## 为什么它有用

- 每个任务都有稳定目录，不再靠终端滚屏和记忆找结果。
- 命令、参数、环境变量、日志、指标和 artifacts 一起落盘。
- Shell 命令与 Python 配置实验使用同一套任务生命周期。
- CLI 一次调用完成一件事，适合人、脚本、CI 和 AI agent。
- 前台执行返回真实结果；批量任务任一失败，整体退出非零。
- detached runner 独立于调用终端，Windows 上不会弹出额外控制台窗口。
- Web UI 与 CLI 共享磁盘状态，不存在两套数据源。

![Home](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_home.png)

## Git 式 CLI

```text
pyr [GLOBAL OPTIONS] COMMAND [COMMAND OPTIONS]
```

常用全局参数：

```text
-C, --directory PATH
-w, --workspace NAME|PATH|SCRIPT.py
--json
--no-color
--debug
--version
```

`-w` 只在 `ls`、`run`、`show`、`log` 等任务命令需要消除多 workspace 歧义时使用；项目只有一个 workspace 时可以省略。`exec` 固定使用 shell workspace，Web UI 则直接写成 `pyr ui shell`、`pyr ui train` 或 `pyr ui train.py`。

正式命令集：

| 命令 | 用途 |
| --- | --- |
| `init` | 初始化 shell 或 Python script workspace |
| `exec` | 创建并运行一个受跟踪的终端命令或 Shell 脚本 |
| `add` | 从 YAML 添加不可变任务快照 |
| `run` | 运行一个或多个精确任务名 |
| `ls` | 稳定过滤和排序任务 |
| `status` | 查看 workspace 状态汇总 |
| `show` | 查看任务元数据和路径 |
| `log` | 打印、跟随或定位日志 |
| `wait` | 等待已在运行的任务 |
| `stop` | 向拥有任务的 runner 请求停止，并标记为 `cancelled` |
| `rm` / `restore` | 软删除与恢复任务 |
| `mv` / `pin` | 管理任务名称与置顶状态 |
| `export` | 导出 CSV 或 JSON 记录 |
| `config` | 查看或修改项目设置 |
| `metrics` | 输出一次 CPU、内存和 GPU 快照 |
| `ui` / `dev` | 显式启动 Web UI |
| `help` | 查看总帮助或子命令帮助 |

每个命令都提供独立的场景化帮助；例如 `pyr help exec` 会直接说明精确 argv、Shell
表达式、脚本执行和环境变量持久化之间的区别。完整说明见
[CLI 详细指南](docs/cli-guide.md)。

## 两种工作区

### Shell Workspace

用于任意终端命令、仓库复现、安装、预处理、训练、评估和流水线：

```bash
pyr init
pyr exec -n env-check -- python -V
pyr exec -n install -- python -m pip install -r requirements.txt
pyr exec -n baseline -d -- python train.py --config baseline.yaml
```

Shell 脚本文件也直接交给 `exec`，不需要手写解释器：

```bash
pyr exec -n setup -- ./scripts/setup.sh
pyr exec -n setup-ps -- .\scripts\setup.ps1
pyr exec -n setup-cmd -- .\scripts\setup.cmd
pyr exec -n setup-bat -- .\scripts\setup.bat
```

这就是对 `bash xxx.sh` / `pwsh -File xxx.ps1` 最常用的受跟踪替代：Pyruns 根据 `.sh`、`.ps1`、`.cmd`、`.bat` 扩展名选择 Bash/sh、PowerShell 或 `cmd.exe`。文件路径之后的内容是该脚本自己的参数，不是 Pyruns 参数。Pyruns 会保留参数边界，并记录日志、开始/结束时间、高精度运行时长、原始退出码、脚本内容哈希和 Git 状态。任务重跑仍使用原脚本路径，因此依赖脚本原目录的相对路径语义不会改变。

`--` 是标准的 CLI 参数边界，不是 Pyruns 的一种“模式”：

- `--` 是参数分隔符，表示 Pyruns 自己的选项到此结束；后面的每一项都是目标程序的独立 argv，Pyruns 不做管道、重定向、变量展开或通配符解析。
- `-c` / `--command` 明确接收一个 shell command string，命名和 `sh -c`、`python -c` 的习惯一致。

普通程序和脚本路径优先使用 `--`：

```bash
pyr exec -n preprocess -- ./scripts/preprocess.sh "dataset A" --fast
pyr exec -n train -- python train.py --lr 0.001
```

当命令确实依赖管道、重定向、变量展开、通配符或 `&&` 时，使用 `-c`：

```bash
pyr exec -n report -c "python eval.py > metrics.txt"
pyr exec -n pipeline -c "python preprocess.py && python train.py | tee train.log"
```

`-c` 后必须是一个完整 command string，因此外层引号不能省略；`-c echo hello` 会被拒绝。Shell 语法会带来平台和引用差异，不需要这些语法时继续使用 `--` 后的精确 argv。

少量任务环境变量只需写一次 `-e`，后面连续列出多个 `KEY=VALUE`，并用 `--` 与目标命令分隔：

```bash
pyr exec -n train -e CUDA_VISIBLE_DEVICES=0 TOKENIZERS_PARALLELISM=false SEED=42 -- python train.py
```

`-e` / `--env` 本身仍可重复，旧命令保持兼容。

Pyruns 已自动为子进程设置 `PYTHONUNBUFFERED=1`、`PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`，通常不需要重复传入。

在 POSIX shell 中，`CUDA_VISIBLE_DEVICES=0 pyr exec ...` 的当前这次运行也会把变量继承给子进程；但该值不会写入任务元数据，之后从另一个终端、Web UI 或 `pyr run` 重跑时不保证仍然存在。需要可复现、可由 `show` 检查的任务配置时使用 `-e` 或 `--env-file`。

变量较多时使用 UTF-8 env 文件：

```dotenv
# .env.train
CUDA_VISIBLE_DEVICES=0
TOKENIZERS_PARALLELISM=false
```

```bash
pyr exec -n train --env-file .env.train -e SEED=42 -- python train.py
```

`--env-file` 可重复，后面的文件覆盖前面的文件，命令行 `-e` 最后覆盖所有文件。文件只接受空行、整行 `#` 注释和 `KEY=VALUE`，不会执行 shell 插值。任务环境会明文保存在元数据并由 `show` 显示，因此不要在其中保存密钥。

执行前可做真正无副作用的预览；加入全局 `--json` 可得到稳定计划对象：

```bash
pyr exec --dry-run -n report -- python eval.py
pyr --json exec --dry-run -n report -- python eval.py
```

预览不会创建 `_pyruns_`、任务或设置文件，也不会启动用户命令。

Shell 任务保存在：

```text
<project>/_pyruns_/_shell_/tasks/<task>/
├── task_info.json
├── config.ps1 | config.cmd | config.sh
└── run_logs/runN.log
```

### Script Workspace

用于 `argparse`、`pyruns.load()`、YAML 配置、batch 展开和参数化实验：

```bash
pyr init train.py
pyr -w train add configs/quick.yaml
pyr -w train run quick
```

创建并立即运行：

```bash
pyr -w train run --from configs/sweep.yaml -n sweep -j 4
pyr -w train run --from configs/sweep.yaml -n sweep -j 4 --dry-run
```

`run --from ... --dry-run` 会验证 YAML 并列出展开后的候选任务，但不创建或运行它们。

Script 任务保存在：

```text
<project>/_pyruns_/train/tasks/<task>/
├── task_info.json
├── config.yaml
├── run_logs/runN.log
└── artifacts/runN/
```

## 工作区选择

Pyruns 会从当前目录向父目录寻找最近的 `_pyruns_`。只有一个 workspace 时自动选择；存在多个时必须显式传 `-w`，不会猜测：

```bash
pyr -w shell ls
pyr -w train status
pyr -w ./train.py show baseline
pyr -w ./_pyruns_/train log baseline
```

任务必须使用精确名称，不支持序号和模糊匹配。`show` 与 `log` 额外支持 `TASK@RUN` 选择历史运行，因此 `@` 不能出现在新任务名中。

## Python 脚本接入

### 零侵入 `argparse`

```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--lr", type=float, default=1e-3)
parser.add_argument("--epochs", type=int, default=10)
args = parser.parse_args()
```

```bash
pyr init train.py
pyr ui train.py
```

Pyruns 会解析脚本参数并建立默认配置模板。

### `pyruns.load()` 配置

```python
import os

import pyruns

cfg = pyruns.load()
print(cfg.training.lr)
```

首次初始化时传入 YAML：

```bash
pyr init train.py --config configs/default.yaml
pyr -w train add configs/default.yaml -n baseline
pyr -w train run baseline
```

## 脚本内 API

| API | 用途 |
| --- | --- |
| `pyruns.load()` | 加载当前任务配置并返回点号访问对象 |
| `pyruns.read(path=None)` | 显式读取 YAML / JSON 配置 |
| `pyruns.record(**kwargs)` | 保存当前 run 的最终指标 |
| `pyruns.track(**kwargs)` | 追加时间序列指标 |
| `pyruns.get_task_dir()` | 返回当前任务目录 |
| `pyruns.get_run_index()` | 返回当前 run 编号 |
| `pyruns.artifact_dir()` | 创建并返回 `artifacts/runN` |

```python
import pyruns

cfg = pyruns.load()

for epoch in range(cfg.training.epochs):
    loss = train_one_epoch()
    pyruns.track(epoch=epoch, loss=loss)

pyruns.record(final_loss=loss, seed=cfg.training.seed)
model.save(os.path.join(pyruns.artifact_dir(), "model.pt"))
```

## 查询、日志和生命周期

```bash
pyr -w train ls -s running --status queued
pyr -w train status
pyr -w train show baseline
pyr -w train show baseline@2
pyr -w train log baseline -f
pyr -w train log baseline@2
pyr -w train wait baseline --timeout 600
pyr -w train stop baseline
pyr -w train mv baseline baseline-lr1e3
pyr -w train pin baseline-lr1e3
pyr -w train rm baseline-lr1e3
pyr -w train ls --trash
pyr -w train restore baseline-lr1e3
```

`rm` 会立即执行，不询问确认，但它只是可恢复的软删除。

## JSON 与自动化

全局 `--json` 放在子命令之前：

```bash
pyr --json -w shell ls
pyr --json -w shell status
pyr --json -w shell show smoke
pyr --json -w shell show smoke@2
pyr --json -w shell log smoke --path
pyr --json -w shell log smoke@2 --path
pyr --json config list
pyr --json metrics
```

日志默认原样写 stdout；需要结构化引用时使用 `log --path`。导出默认写 stdout：

```bash
pyr -w train export -f csv
pyr -w train export baseline -f json
pyr -w train export -s completed -o results.csv
```

退出码：

```text
0    命令和等待的任务全部成功
1    工作区、目标、运行时或任务失败
2    命令行用法错误
130  等待或跟随日志时被中断
```

## Web UI

![Manager](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

```bash
pyr ui
pyr ui train.py
pyr ui train.py --config configs/default.yaml
pyr ui shell
pyr ui shell --no-browser
pyr dev train.py
```

- Generator：编辑脚本配置或 shell payload，并创建任务。
- Manager：搜索、筛选、运行、取消、重命名、置顶和删除任务。
- Monitor：查看运行日志、指标和任务详情。
- Dashboard：查看项目级运行状态和资源概览。

![Monitor](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_monitor.png)

## 磁盘是最终状态源

```text
<project>/_pyruns_/
├── _pyruns_settings.yaml
├── _shell_/
│   ├── script_info.json
│   └── tasks/
└── <script_name>/
    ├── script_info.json
    ├── config_default.yaml
    └── tasks/
```

CLI 和 Web UI 都只是在这套磁盘状态上工作，因此任务不会因为关闭某个界面而消失，也能被版本控制、备份工具和自动化脚本直接检查。

## 文档

- [安装与快速开始](docs/getting-started.md)
- [CLI 详细指南](docs/cli-guide.md)
- [配置说明](docs/configuration.md)
- [Web UI 指南](docs/ui-guide.md)
- [批量配置语法](docs/batch-syntax.md)
- [脚本 API](docs/api-reference.md)
- [架构说明](docs/architecture.md)

## License

MIT
