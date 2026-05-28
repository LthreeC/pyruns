import { defineConfig } from "vitepress";

export default defineConfig({
  base: "/pyruns/",
  title: "Pyruns",
  description: "python调参、任务批量生成、批量运行、管理、监控",
  useWebFonts: false,
  themeConfig: {
    sidebar: [
      {
        text: "快速开始",
        items: [
          { text: "安装与快速开始", link: "/getting-started" },
          { text: "页面展示", link: "/showcase" },
        ],
      },
      {
        text: "核心指南",
        items: [
          { text: "配置架构详解", link: "/configuration" },
          { text: "批量生成语法", link: "/batch-syntax" },
          { text: "Web 界面操作", link: "/ui-guide" },
          { text: "命令行 CLI 控制", link: "/cli-guide" },
        ],
      },
      {
        text: "进阶参考",
        items: [
          { text: "开发者 API", link: "/api-reference" },
          { text: "架构及设计理念", link: "/architecture" },
        ],
      },
    ],
    socialLinks: [
      { icon: "github", link: "https://github.com/LthreeC/pyruns" },
    ],
  },
});
