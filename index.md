---
# https://vitepress.dev/reference/default-theme-home-page
layout: home

hero:
  name: "Pyruns"
  text: "Python 实验管理与监控 Web UI"
  tagline: 🧪 极简、轻量且强大。基于开箱即用的设计理念，让机器学习调参过程优雅而高效。
  image:
    src: https://img.shields.io/pypi/v/pyruns?style=for-the-badge&logo=python&logoColor=white
    alt: Pyruns Logo
  actions:
    - theme: brand
      text: 🚀 快速开始
      link: /docs/getting-started
    - theme: alt
      text: ⚙️ 配置指南
      link: /docs/configuration
    - theme: alt
      text: 在 GitHub 上查看
      link: https://github.com/LthreeC/pyruns

features:
  - title: 🔌 零代码侵入
    details: 自动解析 argparse 定义，直接将 Python 脚本渲染为图形化配置表单，无需修改任何业务代码。
    icon: ⚡
  - title: 🚀 批量生成语法
    details: 内置 | (笛卡尔积) 与 (|) (配对) 语法，分钟级构建大规模参数搜索网格，告别手动改参。
    icon: 🧪
  - title: 📊 实时监控看板
    details: 在浏览器中实时查看 ANSI 彩色日志，内置 CPU/GPU 资源监控，支持实验指标一键导出。
    icon: 📈
  - title: 🛠️ 任务并行调度
    details: 强大的任务管理后台，支持线程/进程双模式调度，轻松管理多核并发实验。
    icon: 🖥️
  - title: 📁 隔离的存储策略
    details: 采用 _pyruns_ 目录自动隔离不同脚本的实验记录，有效防止数据覆写与冲突。
    icon: ⚙️
  - title: 🎨 现代化 Web 交互
    details: 基于 NiceGUI 驱动，响应式布局设计，无论是本地桌面还是远程服务器都能完美适配。
    icon: ✨

---

### 📦 快速安装

在终端执行以下命令，开启优雅调参之旅：

```bash
pip install pyruns