import { defineConfig } from 'vitepress'

// https://vitepress.dev/reference/site-config
export default defineConfig({
  title: "Pyruns",
  description: "python调参、任务批量生成、批量运行、管理、监控",
  themeConfig: {
    // 侧边栏配置
    sidebar: [
      {
        text: '快速开始',
        items: [
          { text: '🚀 安装与快速开始', link: '/docs/getting-started' },
        ]
      },
      {
        text: '核心指南',
        items: [
          { text: '⚙️ 配置架构详解', link: '/docs/configuration' },
          { text: '🧪 批量生成语法', link: '/docs/batch-syntax' },
          { text: '🖥️ Web 界面操作', link: '/docs/ui-guide' },
        ]
      },
      {
        text: '进阶参考',
        items: [
          { text: '🛠️ 开发者 API', link: '/docs/api-reference' },
          { text: '📐 架构及设计理念', link: '/docs/architecture' },
        ]
      }
    ],
    // 社交链接
    socialLinks: [
      { icon: 'github', link: 'https://github.com/LthreeC/pyruns' }
    ]
  }
})
