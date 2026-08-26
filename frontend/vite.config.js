import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 3000,
    proxy: {
      '/api': 'http://127.0.0.1:8000',
      '/cache': 'http://127.0.0.1:8000',
      '/demo_example': 'http://127.0.0.1:8000',
    },
  },
  build: {
    outDir: 'dist',
  },
})
