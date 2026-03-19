# 快速开始

这份文档对应当前 React + FastAPI 主链路版本。

## 1. 安装

```bash
pip install pyruns
```

开发模式：

```bash
git clone https://github.com/LthreeC/pyruns.git
cd pyruns
pip install -e .
```

## 2. 打开第一个脚本工作区

```bash
pyr train.py
```

执行后会围绕脚本创建工作区：

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

其中：

- `script_info.json` 保存脚本和工作区元信息
- `config_default.yaml` 是 Generator 的默认模板
- `tasks/` 下是每一个生成出来的任务

如果你已经有一个 YAML 模板，也可以这样启动：

```bash
pyr train.py my_config.yaml
```

它会把 `my_config.yaml` 作为当前工作区的默认模板。

## 3. 什么时候适合 Script Workspace

Script workspace 适合这两类脚本：

### `argparse` 脚本

Pyruns 会尝试提取参数定义，生成表单，并把修改后的值再拼回命令行参数。

### `pyruns.load()` 脚本

Pyruns 会为每个任务写入 `config.yaml`，并在执行时通过 `__PYRUNS_CONFIG__` 指向它。

## 4. 打开 Shell Workspace

先进入一个普通 script workspace，然后在左侧点击 `Open Shell Mode`。

切换后工作区变为：

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

## 5. shell 默认执行语义

当前默认规则非常简单：

- `shell_mode: follow`
- 跟随启动 `pyr` 的当前终端 / shell
- 不自动做 shell 语法翻译
- 继续继承当前 Python 进程环境

这意味着：

- conda 环境会继承
- `PATH` 会继承
- 你在启动 `pyr` 时已有的环境变量会继承

平台差异只体现在“跟随谁”：

- Windows：通常跟随 PowerShell 或 cmd
- Linux / macOS：通常跟随当前 POSIX shell

如果你确实要改成固定 shell，可在 `_pyruns_settings.yaml` 中写：

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
- 打开日志
- 查看任务详情

### Monitor

- 实时日志
- 历史日志切换
- 多任务日志导出
- 直接从侧边栏进入时默认空选中

## 7. 第一个 shell 任务示例

进入 shell workspace 后，在 Generator 中输入：

```powershell
Write-Host "hello from pyruns shell"
python --version
```

或者在 Linux/macOS 下：

```bash
echo "hello from pyruns shell"
python --version
```

注意：

- 你写的语法必须匹配当前 follow/custom 对应的 shell
- Pyruns 不会把 PowerShell 自动翻译成 bash，也不会反过来翻译

## 8. 第一个 batch 任务示例

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

## 9. 前端开发与打包

前端源码：

```text
frontend/
```

打包命令：

```bash
cd frontend
npm run build
```

打包结果：

```text
pyruns/web/static/
```

后端会直接服务这里的静态资源。

## 下一步

- [配置说明](configuration.md)
- [架构说明](architecture.md)
- [UI 指南](ui-guide.md)
- [CLI 指南](cli-guide.md)
