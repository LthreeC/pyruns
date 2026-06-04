import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

function normalizeModuleId(id: string) {
  return id.replace(/\\/g, '/')
}

function includesPackage(id: string, packageName: string) {
  const marker = `/node_modules/${packageName}`
  return id.includes(`${marker}/`) || id.endsWith(marker)
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { '@': path.resolve(__dirname, 'src') },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8099', ws: true },
    },
  },
  build: {
    outDir: '../pyruns/web/static',
    emptyOutDir: true,
    chunkSizeWarningLimit: 800,
    rollupOptions: {
      output: {
        manualChunks(id) {
          const moduleId = normalizeModuleId(id)
          if (!moduleId.includes('/node_modules/')) {
            return undefined
          }
          if (
            includesPackage(moduleId, 'react') ||
            includesPackage(moduleId, 'react-dom') ||
            includesPackage(moduleId, 'react-router-dom') ||
            includesPackage(moduleId, '@remix-run/router') ||
            includesPackage(moduleId, 'scheduler')
          ) {
            return 'vendor-react'
          }
          if (moduleId.includes('/node_modules/@xterm/')) {
            return 'vendor-xterm'
          }
          if (
            moduleId.includes('/node_modules/@uiw/') ||
            moduleId.includes('/node_modules/@codemirror/') ||
            moduleId.includes('/node_modules/@lezer/') ||
            includesPackage(moduleId, 'codemirror') ||
            includesPackage(moduleId, 'style-mod') ||
            includesPackage(moduleId, 'w3c-keyname') ||
            includesPackage(moduleId, 'crelt')
          ) {
            return 'vendor-codemirror'
          }
          return undefined
        },
      },
    },
  },
})
