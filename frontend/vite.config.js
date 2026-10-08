// frontend/vite.config.js
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Built app is served by FastAPI at /ui. In dev (npm run dev) the API and
// WebSocket are proxied to the running backend so the session cookie works.
const backend = 'http://127.0.0.1:8000'
export default defineConfig({
  base: '/ui/',
  plugins: [react()],
  server: { proxy: { '/api': backend, '/ws': { target: backend, ws: true } } },
})
