# 快速开始

这份指南对应当前的 **React + FastAPI** 主链路版本。  
如果你想尽快看到 Pyruns 的真实手感，这一页就是最短路径。

## 先看效果

在开始之前，先看一眼现在的界面，会更容易进入状态。

![Generator 预览](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_generator.png)

## 1. 安装

```bash
pip install pyruns
```

安装完成后，建议优先记住这两个入口：

```bash
# 围绕某个 Python 脚本打开 script workspace
pyr train.py

# 围绕某个 Python 脚本并导入一份 YAML 模板
pyr train.py my_config.yaml
```

这两条是 Pyruns 的首选主路径。  
`pyr` 直接打开 shell workspace 当然也可以，但更适合作为补充方案，而不是用户第一眼看到的默认入口。

## 1.5 先看懂两种模式

Pyruns 现在有两种很明确的工作模式：

| 模式 | 入口 | 核心对象 | 任务文件 | 适合场景 |
| --- | --- | --- | --- | --- |
| `script` | `pyr train.py` / `pyr train.py my_config.yaml` | Python 脚本 | `config.yaml` | 参数调优、训练脚本、配置模板、批量实验 |
| `shell` | `pyr` | 当前目录 | `config.sh` | 终端命令任务、PowerShell / cmd / bash 工作流 |

可以把它们理解成：

- `script` 模式：Pyruns 在帮你管理“脚本驱动的实验”
- `shell` 模式：Pyruns 在帮你管理“目录驱动的命令任务”

如果你在做本地开发：

```bash
git clone https://github.com/LthreeC/pyruns.git
cd pyruns
pip install -e .
```

## 2. 打开第一个脚本工作区

```bash
pyr train.py
```

执行后，Pyruns 会围绕脚本创建一个稳定的本地工作区：

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

这里的几个关键文件各司其职：

- `script_info.json`：保存脚本与工作区元信息
- `config_default.yaml`：Generator 的默认模板来源
- `tasks/`：所有生成任务的真实落盘位置

如果你已经有一份现成模板，也可以直接这样开：

```bash
pyr train.py my_config.yaml
```

它会把 `my_config.yaml` 作为当前工作区的默认模板。

## 3. 直接打开当前目录的 Shell Workspace

如果你现在还没有固定脚本，只是想先把当前目录里的命令任务纳入 Pyruns：

```bash
pyr
```

这条命令会直接打开当前目录的 shell workspace，并进入 React UI。

对应的工作区结构是：

```text
project/
└─ _pyruns_/
   └─ _shell_/
      ├─ script_info.json
      └─ tasks/
```

这里的 shell 任务会保存为：

```text
_pyruns_/_shell_/tasks/<task_name>/config.sh
```

![Shell Generator 预览](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_generator.png)

## 4. Script Workspace 适合什么

Script workspace 最适合两类脚本：

### `argparse` 脚本

Pyruns 会尽量提取参数定义，生成可编辑表单，再把修改后的值拼回命令行参数。

### `pyruns.load()` 脚本

Pyruns 会为每个任务写入独立的 `config.yaml`，并在执行时通过 `__PYRUNS_CONFIG__` 指向它。

这也是为什么 `script` 模式通常应该被当作首选入口：

- 它更适合标准 Python 实验脚本
- 它和参数编辑、模板管理、batch 生成的结合最自然
- 它更符合大多数用户第一次接触 Pyruns 时的预期

## 5. shell 的默认语义

shell 模式最重要的一点不是“像不像 bash”，而是：

- 默认 `shell_mode: follow`
- 默认跟随启动 `pyr` 的当前终端 / shell
- 继续继承当前 Python 进程环境
- 不自动做跨 shell 语法翻译

这意味着：

- conda 环境会继承
- `PATH` 会继承
- 启动 `pyr` 时已有的环境变量会继承

同时要注意，shell 模式和 script 模式的任务并不是一回事：

- script 模式的任务核心是 `config.yaml`
- shell 模式的任务核心是 `config.sh`
- script 模式更强调“参数如何组织”
- shell 模式更强调“命令如何直接执行”

平台差异只体现在“跟随谁”：

- Windows：通常跟随 PowerShell 或 cmd
- Linux / macOS：通常跟随当前 POSIX shell

如果你确实要固定到某个 shell，可在 `_pyruns_settings.yaml` 中写：

```yaml
shell_mode: custom
shell_executable: C:\Program Files\PowerShell\7\pwsh.exe
```

或者：

```yaml
shell_mode: custom
shell_executable: /bin/bash
```

## 6. 三个主页面怎么用

### Generator

- script workspace：`form` / `yaml`
- shell workspace：`shell`
- 可做单任务，也可做 batch 批量任务

### Manager

- 查找任务
- 批量运行 / 删除
- 查看任务详情
- 一键跳转日志

### Monitor

- 实时日志
- 历史日志切换
- 多任务日志导出
- 从 tab 直接进入时默认空选中

![Manager 预览](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_manager.png)

![Monitor 预览](https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/tab_monitor.png)

## 7. `pyr` / `pyr ui` / `pyr cli` 分别做什么

- `pyr`
  直接打开当前目录的 shell workspace。
- `pyr ui`
  打开 launcher，更适合先选脚本再进入 script workspace。
- `pyr cli`
  进入交互式 CLI；如果当前目录已有工作区，会直接接管这份工作区。

如果你忘了入口差异，可以随时运行：

```bash
pyr help
```

## 8. 第一个 shell 任务示例

进入 shell workspace 后，在 Generator 中输入：

```powershell
Write-Host "hello from pyruns shell"
python --version
```

或者在 Linux / macOS 下：

```bash
echo "hello from pyruns shell"
python --version
```

注意：

- 你写的语法必须匹配当前 follow/custom 对应的 shell
- Pyruns 不会把 PowerShell 自动翻译成 bash，也不会反过来翻译

## 9. 第一个 batch 示例

在 script workspace 的 `form` 或 `yaml` 模式里：

```yaml
lr: 0.001 | 0.01 | 0.1
batch_size: 32 | 64
optimizer: adam | sgd
```

这会展开成多个独立任务，每个任务都拥有自己的：

- `task_info.json`
- `config.yaml`
- `run_logs/runN.log`
- `artifacts/runN/`（当脚本通过 `pyruns.artifact_dir()` 保存文件时）

批量生成完成之后，最适合继续去看的页面就是 Manager，因为任务会以独立卡片的形式稳定落盘，状态和历史都会马上变得可见。

## 10. 前端开发与打包

前端源码在：

```text
frontend/
```

打包命令：

```bash
cd frontend
npm run build
```

打包结果会写入：

```text
pyruns/web/static/
```

后端会直接服务这里的静态资源。

## 下一步

- [界面指南](ui-guide.md)
- [配置说明](configuration.md)
- [架构说明](architecture.md)
- [CLI 指南](cli-guide.md)
