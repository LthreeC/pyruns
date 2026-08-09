# 快速开始

本页从安装开始，分别完成一个 shell 任务和一个 Python 配置任务，并说明如何查询、取消、导出和打开 Web UI。

## 1. 安装与确认入口

```bash
pip install pyruns
pyr --version
pyr --help
pyruns --version
pyruns --help
pyr help -a
pyr help exec
```

Pyruns 同时安装 `pyr` 与 `pyruns` 两个完全等价的正式命令。本文使用更短的 `pyr`；任何示例都可以原样换成 `pyruns`。裸命令与 `--help` 打印精简帮助，`help -a` 展开全部命令；它们都不会创建工作区或启动 Web 服务。

当你不确定应该使用 `--`、`-c`、脚本直传、`-e` 还是 `--env-file` 时，直接运行
`pyr help exec`；它按常见场景给出选择规则和后续查看日志、等待、重跑的命令。

## 2. 第一个 shell 任务

进入一个项目目录：

```bash
cd my-project
pyr exec -n smoke -- python -V
```

`exec` 会自动创建 shell workspace、任务和第一次运行记录：

```text
my-project/
└── _pyruns_/
    ├── _pyruns_settings.yaml
    └── _shell_/
        ├── script_info.json
        └── tasks/
```

`--` 是“Pyruns 参数到此结束”的分隔符，后面是目标程序的精确参数向量。它本身不启用 shell；即使命令自身带 `--epochs`、`-p` 等选项，也不会与 Pyruns 参数混淆。

只预览计划、不创建 workspace/task 也不运行命令：

```bash
pyr exec --dry-run -n smoke -- python -V
pyr exec --dry-run -n smoke --json -- python -V
```

任务结束后查询：

```bash
pyr ls
pyr show smoke
pyr log smoke
```

这里只有一个 workspace，因此可以省略 `-w shell`；多 workspace 项目再显式选择。

任务目录类似：

```text
_pyruns_/_shell_/tasks/smoke/
├── task_info.json
├── config.ps1 | config.cmd | config.sh
└── run_logs/run1.log
```

## 3. 长任务与后台运行

默认 `exec` 会跟随日志并把任务成功或失败反映到退出码。长任务可使用 `--detach`：

```bash
pyr exec -n train -d -- python train.py --epochs 100
```

runner 接受任务后，命令立即返回；关闭调用终端不会终止任务。Windows 上 runner 和任务进程不会弹出新的控制台窗口。

随后可以在任意新终端中执行：

```bash
pyr status
pyr show train
pyr log train -f
pyr wait train --timeout 3600
```

取消活动任务：

```bash
pyr stop train
```

取消请求会送到真正拥有该任务的 runner，而不是只修改显示状态。

## 4. 什么时候使用 `-c`

普通程序或脚本文件总是优先使用 `--` 后的精确 argv。直接运行脚本文件时，Pyruns 会按扩展名选择解释器，这可以直接替代 `bash xxx.sh`，同时保留日志、运行时长、退出码和源码状态：

```bash
pyr exec -n eval -- python eval.py --checkpoint "best model.pt"
pyr exec -n preprocess -- ./scripts/preprocess.sh "dataset A" --fast
```

只有需要管道、重定向、变量展开、通配符或 `&&` 等 shell 解析时才使用 `-c` / `--command`。它后面必须是一整个被引用的 command string：

```bash
pyr exec -n report -c "python eval.py > metrics.txt"
pyr exec -n pipeline -c "python preprocess.py && python train.py | tee train.log"
```

少量环境变量只需写一次 `-e`，`--` 明确标记目标命令的开始：

```bash
pyr exec -n gpu0 -e CUDA_VISIBLE_DEVICES=0 TOKENIZERS_PARALLELISM=false SEED=42 -- python train.py
```

`-e` / `--env` 可以按变量组重复使用。Pyruns 已自动为子进程设置 `PYTHONUNBUFFERED=1`、`PYTHONIOENCODING=utf-8` 和 `PYTHONUTF8=1`。

变量较多时写入 UTF-8 env 文件：

```dotenv
# .env.train
CUDA_VISIBLE_DEVICES=0
TOKENIZERS_PARALLELISM=false
```

```bash
pyr exec -n gpu0 --env-file .env.train -e SEED=42 -- python train.py
```

`--env-file` 可重复：后面的文件覆盖前面的文件，命令行 `-e` 再覆盖文件值。文件只读取空行、整行 `#` 注释和 `KEY=VALUE`，不做 shell 插值。变量会明文保存到任务元数据，所以不要用于密钥。

在 POSIX shell 中也可写 `CUDA_VISIBLE_DEVICES=0 pyr exec ...`，当前运行会继承该变量；但它不会被任务保存，后续从别的终端、Web UI 或 `pyr run` 重跑时不保证相同。需要可复现时仍应使用 `-e` 或 `--env-file`。

## 5. 第一个 Python script workspace

假设 `train.py` 使用 `argparse`：

```python
import argparse

parser = argparse.ArgumentParser()
parser.add_argument("--lr", type=float, default=0.001)
parser.add_argument("--epochs", type=int, default=10)
args = parser.parse_args()

print(f"lr={args.lr}, epochs={args.epochs}")
```

初始化：

```bash
pyr init train.py
```

这会创建 `_pyruns_/train/`，并准备 `config_default.yaml`。

创建一份配置，例如 `configs/quick.yaml`：

```yaml
lr: 0.001
epochs: 2
```

创建并运行：

```bash
pyr -w train add configs/quick.yaml -n quick
pyr -w train run quick
```

也可以合并为一次操作：

```bash
pyr -w train run --config configs/quick.yaml -n quick
pyr -w train run --config configs/quick.yaml -n quick --dry-run
```

## 6. `pyruns.load()` 脚本

如果脚本直接读取 YAML：

```python
import pyruns

cfg = pyruns.load()
print(cfg.training.lr)
```

第一次初始化时提供模板：

```bash
pyr init train.py --config configs/default.yaml
```

后续仍使用 `add` 和 `run`：

```bash
pyr -w train add configs/sweep.yaml -n sweep
pyr -w train run sweep_[1-of-4] sweep_[2-of-4] -j 2
```

任务名必须精确填写；Pyruns 不接受列表序号或模糊名称。

## 7. Batch 配置

Pyruns 可以把 YAML 中的 batch 表达式展开成多个任务。示例：

```yaml
lr: 0.0001 | 0.001
seed: 1 | 2
epochs: 10
```

```bash
pyr -w train add configs/sweep.yaml -n sweep
pyr -w train ls sweep
pyr -w train run sweep_[1-of-4] sweep_[2-of-4] -j 2
```

完整语法见 [批量配置语法](batch-syntax.md)。

## 8. 多工作区项目

Pyruns 从当前目录向父目录查找最近的 `_pyruns_`：

- 只有一个 workspace：自动选择。
- 多个 workspace：必须使用 `-w`。
- 没有 workspace：`exec` 自动创建 shell workspace；其他任务命令提示先 `pyr init`。

```bash
pyr -w shell ls
pyr -w train ls
pyr -w ./train.py status
pyr -w ./_pyruns_/train show quick
pyr -w ./_pyruns_/train show quick@2
pyr -w ./_pyruns_/train show quick --run 2
```

从另一个目录操作项目可以使用 `-C`：

```bash
pyr -C D:/work/my-project -w shell status
```

`-C`、`-w` 等项目上下文参数放在子命令之前；`--json` 只能放在明确支持它的子命令后。

## 9. 稳定 JSON 输出

所有 `--json` 结果都是严格 JSON 对象，顶层带有 `"schema_version": 1`。
非有限数字不会被输出为非标准的 NaN 或 Infinity。

脚本、CI 和 AI agent 应优先读取 JSON，而不是解析表格：

```bash
pyr -w shell ls --json
pyr -w shell status --json
pyr -w shell show smoke --json
pyr -w shell show smoke@2 --json
pyr -w shell show smoke --run 2 --json
pyr -w shell log smoke --path --json
pyr -w shell log smoke@2 --path --json
pyr config list --json
pyr metrics --json
```

`log` 默认原样输出日志，因此只有 `log --path` 支持 `--json`。

退出码：

```text
0    命令和等待的任务全部成功
1    工作区、目标、运行时或任务失败
2    命令行用法错误
130  等待或跟随日志时被中断
```

## 10. 任务生命周期

```bash
pyr -w train mv quick quick-lr1e3
pyr -w train pin quick-lr1e3
pyr -w train pin quick-lr1e3 --off
pyr -w train rm quick-lr1e3
pyr -w train ls --trash
pyr -w train restore quick-lr1e3
```

`rm` 不询问确认并立即执行，但只是可恢复的软删除。

## 11. 导出与配置

导出默认写 stdout：

```bash
pyr -w train export -f csv
pyr -w train export quick -f json
pyr -w train export -s completed -o results.csv
```

管理项目配置：

```bash
pyr config list
pyr config get monitor_scrollback
pyr config set monitor_scrollback 200000
pyr config unset monitor_scrollback
pyr config path
```

批量运行的并发数与后端在每次 `run` 时通过 `-j/--jobs` 和 `--backend` 明确选择，
不会藏在项目配置里。

## 12. 显式打开 Web UI

CLI 是默认控制面；需要可视化浏览时显式启动：

```bash
pyr ui                                  # workspace launcher
pyr ui train.py                         # script workspace
pyr ui train.py --config config.yaml    # 导入模板
pyr ui shell                            # shell workspace
pyr ui shell --no-browser               # headless server
pyr dev train.py                        # 开发热更新
```

每次 UI 启动都会打印一个带随机访问令牌的本机 URL。浏览器首次打开后会改用
`HttpOnly` 会话 cookie，并从地址栏移除令牌；`--no-browser` 场景需要复制完整 URL，
且不要分享给其他用户。UI 只面向本机使用，不是远程多用户服务。

Web UI 与 CLI 共用 `_pyruns_` 状态。可以用 CLI 提交，再在 UI 中观察，或者反过来查询和取消。

## 13. 从源码开发

```bash
git clone https://github.com/LthreeC/pyruns.git
cd pyruns
python -m pip install -e .
pyr --help
```

前端开发与构建：

```bash
npm --prefix frontend install
npm --prefix frontend run dev
npm --prefix frontend run build
```

## 下一步

- [CLI 详细指南](cli-guide.md)
- [配置说明](configuration.md)
- [Web UI 指南](ui-guide.md)
- [批量配置语法](batch-syntax.md)
- [脚本 API](api-reference.md)
- [架构说明](architecture.md)
