import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// All data comes from the NASA-ACEs backend. In dev it is proxied under /api
// (BACKEND_URL=http://127.0.0.1:8000 to use a local backend); production builds
// call VITE_API_URL from .env.production directly.
const BACKEND_URL = process.env.BACKEND_URL || 'https://backend-production-27695356.up.railway.app'

const apiProxy = {
  '/api': {
    target: BACKEND_URL,
    changeOrigin: true,
    rewrite: (p: string) => p.replace(/^\/api/, ''),
  },
}

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { proxy: apiProxy },
  preview: { proxy: apiProxy },
})
