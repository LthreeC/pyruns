[1 tool called]

Let me first thoroughly study the codebase to write accurate docs:

[17 tools called]

Now let me check the remaining UI files:

[2 tools called]

Now let me read the remaining utility and component files:

[5 tools called]

Now let me check the remaining component files:

[3 tools called]

Now I have a thorough understanding of the entire codebase. Let me also check the design.md and the `__init__.py` for utils:

[5 tools called]

Now I have a comprehensive understanding of every file. Let me create the docs directory and documentation files:

[14 tools called]

Now I have everything needed for the development guide:

[1 tool called]

Now let me create an index page for the docs:

[1 tool called]

Now let me check the current state of `docs/development.md` and complete it:

[2 tools called]

The `docs/development.md` is already complete. Let me verify all the docs files are in place and check the overall docs directory:

[3 tools called]

All documentation files are complete. Let me do a final review of the docs directory:

[6 tools called]

All documentation files are complete and ready. Here's a summary of the `docs/` directory:

```
docs/
├── index.md            # 文档索引与导航
├── getting-started.md  # 安装与快速开始
├── architecture.md     # 项目架构详解（三层架构、数据流、设计原则）
├── configuration.md    # 配置系统（YAML、task_info.json、ConfigNode）
├── api-reference.md    # 公开 API 与工具函数参考
├── ui-guide.md         # UI 三页面详细操作指南
├── batch-syntax.md     # Product / Zip 批量生成语法详解
└── development.md      # 开发者指南（环境搭建、代码规范、NiceGUI 要点）
```

### 文档内容概览

| 文档 | 核心内容 |
|------|----------|
| `index.md` | 总目录，按用户/开发者/问题排查分类，含快速链接 |
| `getting-started.md` | 系统要求、安装方法、第一个实验完整流程、目录结构说明、环境变量 |
| `architecture.md` | 三层架构图（CLI → UI → Core → Utils）、模块详解（TaskManager 状态机、executor 流程）、轮询与快照对比、CSS 注入策略、延迟加载设计原则 |
| `configuration.md` | `config_default.yaml` 与 `config.yaml` 的区别、`task_info.json` 完整字段说明、ConfigNode 点号访问、类型推断规则、配置优先级 |
| `api-reference.md` | `pyruns.read()`/`load()`/`add_monitor()` 完整签名与示例、ConfigNode 方法、所有 `utils/` 工具函数文档 |
| `ui-guide.md` | Generator/Manager/Monitor 三页面布局图、操作流程、轮询机制、状态颜色编码、通知消息 |
| `batch-syntax.md` | Product（`|`）/ Zip（`(|)`）语法详解、混合使用公式、嵌套参数支持、确认对话框、错误处理 |
| `development.md` | 开发环境搭建、文件组织原则（单文件 ≤300 行）、命名约定、NiceGUI 四大开发要点（延迟加载/per-client CSS/嵌套 refreshable/快照对比）、添加新功能的步骤模板、手动测试清单、项目重构变更日志 |