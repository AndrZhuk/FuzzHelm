import { fileURLToPath, URL } from 'node:url'
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// Панель ходить до FastAPI через проксі: у браузері немає CORS, а адреса бекенду
// задається однією змінною FUZZHELM_API (для контейнера — http://api:8000).
const API = process.env.FUZZHELM_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [vue()],
  resolve: { alias: { '@': fileURLToPath(new URL('./src', import.meta.url)) } },
  server: {
    port: 5173,
    host: true,
    // Редактор правил стартує з РЕАЛЬНИХ config/*.yaml (імпорт ?raw), щоб не тримати
    // другу копію бази правил, яка розійдеться з робочою.
    fs: { allow: ['..'] },
    proxy: {
      '/api': {
        target: API,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ''),
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true, chunkSizeWarningLimit: 1400 },
})
