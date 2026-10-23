import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// In development the API is a second process on 8080 (see fe/README.md); in the
// container FastAPI serves this build itself, so the proxy is dev-only.
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: { '/api': 'http://localhost:8080' },
  },
})
