---
layout: home

hero:
  name: "Pyruns"
  text: "可复现任务的 Git 式 CLI 与 Web UI"
  tagline: "用一次性命令运行、记录和控制终端任务与 Python 实验，需要可视化时再显式打开 Web UI。"
  image:
    src: https://img.shields.io/pypi/v/pyruns?style=for-the-badge&logo=python&logoColor=white
    alt: Pyruns version
  actions:
    - theme: brand
      text: 快速开始
      link: /getting-started
    - theme: alt
      text: 配置指南
      link: /configuration
    - theme: alt
      text: GitHub
      link: https://github.com/LthreeC/pyruns

features:
  - title: 零代码侵入
    details: 自动解析 argparse 定义，直接将 Python 脚本渲染为图形化配置表单，无需修改任何业务代码。
  - title: 批量生成语法
    details: 内置 | 笛卡尔积与 (|) 配对语法，分钟级构建大规模参数搜索网格，告别手动改参。
  - title: 实时监控看板
    details: 在浏览器中实时查看 ANSI 彩色日志，内置 CPU/GPU 资源监控，支持实验指标一键导出。
  - title: 任务并行调度
    details: 支持线程/进程双模式调度，轻松管理多核并发实验。
  - title: Git 式一次性 CLI
    details: pyr 与 pyruns 双正式入口、明确子命令、稳定 JSON 与退出码；适合终端、CI 和 AI agent，不需要操控交互提示符。
  - title: 隔离的存储策略
    details: 采用 _pyruns_ 目录自动隔离不同脚本的实验记录，有效防止数据覆写与冲突。
---

### 快速安装

```bash
pip install pyruns
pyr exec -n smoke -- python -V
```

从 [安装与快速开始](/getting-started) 进入完整使用流程，或直接查看 [Web 界面操作](/ui-guide) 和 [命令行 CLI 控制](/cli-guide)。
