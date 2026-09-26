# 架构说明

Pyruns 当前的核心设计其实可以浓缩成一句话：

> 一次性 CLI 和 React/FastAPI Web UI 是两个并列控制面，core 层负责把任务真正落到磁盘并执行，磁盘才是最终真实来源。

这不是一个“先有页面、再去猜状态”的系统。  
它是一个“先把任务结构稳稳落下，再让 UI 把它看清楚”的系统。

## 1. 整体结构

```text
One-shot CLI (pyr / pyruns) ─────┐
                                 ├──→ Core Services (pyruns/core)
Web UI (frontend + pyruns/web) ──┘              ↓
                                  Utils / File IO / Settings
                                                ↓
                                  Workspace on disk (_pyruns_/...)
```

前端交付链路则是：

```text
frontend/  --build-->  pyruns/web/static/
```

## 2. 这套分层为什么顺

### `pyruns/cli/`

负责：

- 用 `argparse` 定义稳定的一次性命令
- 发现项目并精确选择 workspace
- 把任务执行、查询、日志、生命周期、配置和导出映射到 core 服务
- 用 stdout、stderr、JSON 和退出码提供自动化契约
- 显式启动 Web UI，而不是让裸命令产生隐藏副作用

CLI 不依赖 FastAPI，也没有交互式 REPL。`pyr exec --detach` 启动隐藏 runner；runner 接受任务后，调用端即可退出。

### `frontend/`

负责：

- 页面和交互
- Zustand 状态管理
- 轮询与 websocket
- xterm.js / CodeMirror 等展示层

关键页面：

- `dashboard`
- `generator`
- `manager`
- `monitor`

这层的重点是体验，不负责伪造真实状态。

### `pyruns/web/`

负责：

- FastAPI app 组装
- API 路由
- 当前 workspace runtime
- 聚合 dashboard、task list、task logs、workspace info

关键文件：

- `pyruns/web/app.py`
- `pyruns/web/runtime.py`

其中 `runtime.py` 是 Web 层最重要的状态枢纽之一，负责把“当前打开的是哪个工作区、当前使用什么 shell runtime、当前 tasks 目录在哪里”这些运行时信息统一组织起来。

首次读取任务详情或日志时，runtime 只按名称加载目标任务。列表、搜索、全局计数、任务事件流、运行及批量操作会先完整发现工作区任务；单任务读取不会把部分缓存标记为完整工作区。磁盘上尚未加载的排队任务也在完整发现时进入当前管理器的调度视野。完成全量加载后，`refresh=False` 保留已有任务快照，也不会主动发现外部新增任务。

新发现的任务在读入时取得元数据与配置快照，同一次刷新不再立即重复读取这些新任务；原有任务继续检查磁盘变化。后续显式或周期刷新仍检查配置内容，包括文件属性未变的等长改写。

普通列表和搜索请求在全量刷新成功结束后复用四秒缓存，使用单调时钟计时，避免耗时刷新刚结束就再次扫描。强制刷新和显式缓存失效仍会立即检查磁盘；抛出异常的刷新不会延长缓存窗口。

目录发现、任务载入和刷新会根据前三项的耗时与线程 CPU 时间判断是否并行；至少两项显示持续 I/O 等待才启动线程池，避免单次短暂停顿使整批读取进入更慢的并行路径。每项仍只处理一次，并按原顺序返回结果。

目录枚举利用直接子项的信息，逐项检查任务根、管理目录链和链接/重解析属性，减少重复的规范路径解析。元数据和载荷读取仍执行完整的路径边界校验。

完整任务详情按元数据快照中的曲线版本和运行数量读取外部曲线，已知运行仍包含新增的数据点。重跑新增的运行在刷新元数据后显示；如果旧曲线版本已被替换并回收，返回曲线读取错误，刷新后再读取对应版本，避免把新曲线配到旧记录上。曲线读取在管理器锁外进行。

Web CSV 导出通过任务摘要选择任务；CSV 与 JSON 报表中，每个任务的状态、运行历史和汇总指标均取自同一次元数据读取，避免重跑时混用不同时间点的数据，也不展开未使用的训练曲线。各任务分别读取，不构成工作区的全局事务快照。普通任务详情继续返回完整曲线。

### `pyruns/core/`

这是 Pyruns 真正干活的地方。

#### `task_generator.py`

负责把“用户想生成什么任务”真正写到磁盘：

- python task 写 `config.yaml`
- shell task 写当前 runtime 对应的 `config.ps1` / `config.cmd` / `config.sh`
- 同时生成 `task_info.json`

#### `task_manager.py`

负责任务管理：

- 扫描磁盘任务
- 刷新任务状态
- 批量运行、删除、pin
- 让内存视图始终跟磁盘文件对齐

#### `executor.py`

负责实际执行任务：

- 根据 `task_kind` 选择命令构造方式
- python task 注入 `__PYRUNS_CONFIG__`
- shell task 解析 shell runtime
- 采集 stdout / stderr 到 `run_logs`
- 把实时日志推给 Monitor websocket

#### `system_metrics.py`

负责系统指标：

- CPU
- RAM
- NVIDIA GPU 利用率
- 显存占用 / 总量
- GPU 进程明细

### `pyruns/utils/`

这一层统一处理本地持久化、路径校验和进程管理。

#### `settings.py`

- 读写 `_pyruns_settings.yaml`
- 合并默认值
- 提供 save / load / get

#### `shell_runtime.py`

- 解析 `shell_mode`
- 区分 `follow` / `custom`
- 检测当前启动终端
- 给 executor、runtime、frontend 提供统一 shell runtime 信息

#### `task_files.py`

- 统一 task payload 文件读写
- 归一化 `workspace_kind` / `task_kind`

配置或脚本载荷的单次路径解析固定已验证的任务目录边界，防止父项目链接在目录校验与文件校验之间切换后，把另一个目录当作合法边界。候选文件和链接状态仍重新检查；后续独立访问会重新解析项目位置。

Windows 上，未使用本次操作固定边界的独立校验会直接比较两端已存在路径的原生规范形式，减少 `realpath` 为移除扩展路径前缀而重复打开文件的开销。任意一端解析失败或需要固定边界时，继续使用完整的 `realpath` 校验，避免将真实的尾随点文件名与不存在的普通文件名混为同一路径。

任务初次加载与刷新时，普通 YAML 配置使用独立的字典和列表视图，减少任务列表的 OmegaConf 对象构造。快速路径要求映射键均为字符串、值为普通标量或容器；插值、缺失值、特殊类型、非字符串键及深层结构继续由 OmegaConf 处理。YAML 别名的各分支单独复制，公开快照仍与内部配置隔离。执行提交前，普通视图转换为 OmegaConf，保留参数生成语义。`read_task_payload` 默认仍返回 OmegaConf 配置；文件格式和刷新失效规则保持原契约。

任务管理器通过 `read_task_payload_snapshot` 同时取得载荷和文件签名，减少加载阶段重复的路径解析。签名包含读取前的文件属性及实际读取字节的 SHA256 摘要；即使等长改写没有改变文件时间戳，后续刷新也能识别内容变化。加载和后续探测都从已打开的句柄读取文件属性，避免 Windows 上路径 `stat` 与句柄 `fstat` 的 `ctime` 差异导致反复重载未变化的配置。载荷探测只进行有界字节读取和摘要计算，内容变化后才重新解析配置。解码、超限或解析错误仍保留已读取字节的签名，避免反复解析未变化的错误配置。`read_task_payload` 保留原四元组返回值。

每次载入或更新任务时，配置文件名与内容读取使用同一次文件选择。隐式 Shell 配置在读取期间出现更优先文件时，当前快照仍保持文件名与内容一致，后续刷新再发现新的文件选择。

#### `info_io.py`

工作区目录校验每次重新读取管理目录链的文件状态，并在本次调用内复用叶目录的 `lstat` 结果检查链接属性和目录类型，减少重复查询。文件状态不会跨调用缓存，后续目录替换仍会重新校验。

- 读写 `task_info.json`
- 读写 `script_info.json`
- 解析日志路径

读取任务元数据时校验 `env` 为对象或 `null`。损坏的环境字段通过已有任务加载错误显示，其他任务仍可列出；修复文件并刷新后恢复，读取不会改写原文件。

任务元数据与设置写入按路径共享进程内的线程锁。注册表使用弱引用，持有锁和等待锁的调用保留强引用，最后一个使用者退出后即可回收；持续访问或删除新任务、切换工作区不会使闲置锁随历史路径数一直累积。注册过程仍互斥，跨进程写入继续由独占文件锁保护。

文件锁的归属信息必须完整写入后才能开始更新数据。初始化写入失败或被中断时，关闭句柄并按文件身份清理本次创建的锁；同名路径已被新持有者替换时保留新文件，后续写入可以在故障恢复后重新取锁。

任务元数据更新、设置保存或删除、任务名称预留完成后，在原锁句柄中追加释放标记再关闭句柄。Windows 读句柄暂时阻止删除且超过释放重试预算时，后续写入或同名预留可识别完整标记并回收锁文件，不会继续等待已经完成操作的存活进程。活动持有者、未写完整的标记和替代锁仍按原归属规则处理；正常释放的重试次数和取锁时限保持有界。

#### `file_io.py`

配置、设置、模板、任务元数据及 CLI 提交载荷共用受限字节读取。读取时以已打开文件的大小估算首次分配，避免小文件按整个大小上限分配缓冲区；后续持续读取到 EOF 或字节上限。文件大小变化和短读不会绕过原有超限检查，各入口保留自己的编码和解析规则。

`tests/test_bounded_file_io.py` 在独立子进程中核对七个读取入口的完整结果，以及小文档的 Python 分配峰值；当前门槛为每次 256 KiB，用于阻止按 4/32 MiB 上限预分配的回退。该检查随跨平台 pytest 执行，Windows 测试子进程使用无窗口创建标志。

可在仓库根目录运行 `python scripts/benchmark_file_reads.py --output .tmp/file-reads.json`，保存各入口的分配峰值及五组预热后的读取耗时。`--iterations 0` 只检查结果和分配上限。导入与初次读取不计入测量；结果中的临时目录说明数据所在磁盘，分配峰值不等于进程 RSS，耗时也不代表冷磁盘或完整 API 请求。CI 不设置绝对耗时门槛。

#### `lock_queue.py`

同一任务的多个进程竞争写入时，在 `.pyruns-lock-waiters` 中登记短期票据，按序尝试取得 `info_io.py` 的独占文件锁。无竞争时无需创建票据。票据只控制尝试顺序，数据写入仍由独占文件锁保护；票据过期不会释放正在持有的文件锁。

等待者退出、PID 被复用、票据过期或任务改名后，其票据会被回收。清理前核对路径和文件身份，避免误删替代文件。与不识别票据的旧版本混用时，原文件锁仍保证互斥，但旧写入者不遵循排队顺序。

#### `log_io.py`

- 读取日志文件
- 处理编码回退
- 标准化终端换行

#### `sort_utils.py`

- 搜索和排序逻辑
- Manager / Monitor 的任务深度搜索与状态过滤

## 3. 磁盘是最终状态源

这是 Pyruns 很重要的设计原则。

前端状态和运行时内存都只是视图。  
真正能被信任、能被恢复、能被共享的是磁盘上的这些文件：

- `task_info.json`
- `config.yaml` / `config.ps1` / `config.cmd` / `config.sh`
- `run_logs/`

这带来的好处很直接：

- 页面刷新后状态可恢复
- 不依赖数据库
- CLI / Web UI 可以共用同一套任务状态
- 任务本身很容易检查、备份、迁移

## 4. 两条工作流

### Script Workspace 工作流

```text
pyr init train.py
  → bootstrap_workspace(...)
  → _pyruns_/train/
pyr -w train add config.yaml
  → TaskGenerator.create_tasks(..., task_kind="python")
  → tasks/<task_name>/config.yaml
pyr -w train run <exact-task-name>
  → hidden runner → executor injects __PYRUNS_CONFIG__
```

### Shell Workspace 工作流

```text
pyr init
  → _pyruns_/_shell_/
pyr exec --name <task> -- <argv...>
  → TaskGenerator.create_shell_task(...)
  → tasks/<task>/{config.ps1|config.cmd|config.sh}
  → hidden runner → executor resolves shell runtime
  → shell task runs and writes run_logs/runN.log
```

## 5. shell 执行策略

Pyruns 对 shell 模式的理解不是“模拟 bash”，而是：

> 尽量跟随你调用 `pyr` 或启动 Web UI 时的原终端语义

默认值：

- `shell_mode = follow`

只有显式切到：

- `shell_mode = custom`

才会启用 `shell_executable`。

### Windows

- 跟随 PowerShell：按 PowerShell 语义执行
- 跟随 cmd：按 cmd 语义执行
- wrapper 只负责把任务文本交给原生终端
- 不做跨 shell 翻译
- 后台 runner 和任务进程使用无窗口创建标志，不产生额外控制台弹窗

### Linux / macOS

- 默认跟随当前 shell
- 强调 follow，而不是“bash 优先”
- macOS 的 PTY 写入端由采集器保留到子进程退出且已排空输出，避免短命令结束时丢失尚未读取的日志。取消或启动后任务归属变化时也会关闭采集端。

## 6. 前端日志流为什么稳定

Monitor 页面依赖两种数据源：

### 初始历史加载

- `GET /api/tasks/{name}/logs`

### 运行中增量流

- `WS /api/tasks/{name}/logs/stream`

当前实现重点包括：

- 切任务时会重置日志选择状态
- 日志订阅的首次读取、文件状态轮询及工作区锁等待在线程中执行；返回轮询结果后复核流偏移，避免把实时日志推进误判为文件截断
- 从排队日志切换到运行日志时，先读完排队尾部，再发送暂存的原始实时文本；暂存同时受条数和字符数限制，超限时从磁盘补读
- 任务事件订阅的初始化、工作区检查和释放在线程中执行；取消连接后仍完成资源交接和清理，避免遗留监听计数及收发协程
- websocket chunk 会校验任务身份，避免串到错误任务
- 历史日志会做终端换行标准化，避免 xterm 出现“逐行右移”的假缩进

## 7. UI 为什么适合做 GitHub Pages 展示

现在的 React UI 已经足够完整，完全可以做一个静态展示版：

- Dashboard 展示假系统指标
- Generator 展示表单 / YAML / Shell 三种态
- Manager 展示任务卡片和详情抽屉
- Monitor 展示模拟日志流和终端界面

也就是说，GitHub Pages 很适合承载：

- 文档站
- 截图展示
- mock 数据驱动的静态 UI demo

不适合承载的部分则是：

- 真正执行任务
- 实时 FastAPI runtime
- 真实任务调度

## 8. 持续质量检查

Python CI 覆盖 Linux Python 3.10–3.13、Windows/macOS Python 3.12，并为每个组合保存独立的 JUnit 测试报告，保留 14 天；测试失败时也上传已生成的报告。前端、三平台浏览器和隔离 wheel 另有检查。

安装包作业构建一次 wheel，并在 Linux Python 3.10、Windows/macOS Python 3.12 的独立环境中安装同一个产物，检查依赖并在源码目录外创建临时项目。除命令入口和 shell 参数外，还检查配置快照、任务失败后重跑、两轮各 600 步的指标与记录、历史日志、CSV/JSON 导出，以及重命名、删除和恢复。检查结束时核对任务与 runner 进程已退出。wheel、各平台的安装指纹、命令输出和检查结果保存 14 天；失败时同时保留临时项目用于定位问题。

使用只安装了 wheel 的 Python 环境可单独复现：

```bash
/path/to/wheel-venv/bin/python -I scripts/check_installed_lifecycle.py --output test-results/wheel/report.json
```

静态门槛包含原有 flake8 错误检查，以及 `pyruns/`、`scripts/` 的 Ruff E4/E7/E9/F/B/ASYNC 规则。ty 覆盖这两个目录中的全部 Python 模块，按全部平台解析类型。工具版本固定在 `lint` 可选依赖中。

类型检查保留两类有注释的定点豁免：Linux 检查环境未安装的 Windows 专用 `winpty` 导入，以及检查器未正确分析异常路径而报告的六处 `finally` 清理条件。写入或替换失败时，这些清理条件仍会成立，不能删除。没有按整个文件关闭类型检查。

在仓库根目录、已激活的 Python 环境中运行：

```bash
python -m pip install -e ".[test,lint]"
python -m flake8 . --count --select=E9,F63,F7,F82 --show-source --statistics
python -m ruff check pyruns scripts
python -m ty check
python -m pytest -q --junitxml=test-results/pytest.xml
```

如果 ty 与项目依赖安装在不同环境，使用 `ty check --python <项目虚拟环境目录>` 指定依赖环境。静态检查补充运行回归；小文档读取的分配上限检查仍随跨平台 pytest 执行。

三平台 CI 还运行规模基准：默认 Web runtime 的 1,000/10,000 任务列表、分页及名称搜索，以及 10 万/50 万步指标历史的追加、完整读取和读写并发。夹具包含配置、记录、曲线、环境和日志，覆盖已完成、失败、取消及待运行任务；请求通过真实 ASGI 应用处理，逐项校验夹具字段和完整响应稳定性。

```bash
python scripts/benchmark_scaling.py --tasks 1000 10000 --history-steps 100000 500000 --samples 60 --output test-results/scaling.json
```

结果记录提交、运行环境、文件系统、每次耗时、分位数及单独测量的 Python 分配峰值，保留 14 天。首次列表使用新 runtime，文件已由夹具创建并处于系统缓存中；它不代表冷盘启动。数据生成和结果验证不计入耗时，指标读取本身的解码仍计入。当前以数据正确性、实际读写重叠和资源释放作为失败门槛，耗时用于持续观察，尚未设置跨机器统一的延迟阈值。可用 `--suite workspace` 或 `--suite metrics` 单独运行一类，`--temp-root` 选择夹具所在文件系统。

## 9. 推荐补图位置

如果你想让这一页更有展示感，最适合补的图是：

- 一张整体架构流程图
- 一张前端到 `pyruns/web/static/` 的交付链路图
- 一张 shell follow/custom 语义的流程示意图

如果后面要补图，建议统一放在：

- `https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/architecture_overview.png`
- `https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/build_pipeline.png`
- `https://raw.githubusercontent.com/LthreeC/pyruns/main/docs/assets/shell_runtime_flow.png`
