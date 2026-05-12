import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      // 与 docker/nginx 一致：浏览器请求 /api/* → 后端 /*（FastAPI 路由无 /api 前缀）
      '/api': {
        target: 'http://localhost:8001',
        changeOrigin: true,
        rewrite: (path) => {
          const stripped = path.replace(/^\/api/, '');
          return stripped === '' ? '/' : stripped;
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
  },
})
