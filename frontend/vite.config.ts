import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// npm run dev 로 호스트에서 띄울 때는 compose 로 뜬 nginx(3040)를 그대로 백엔드로 쓴다.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:3040',
        changeOrigin: true,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: false },
})
