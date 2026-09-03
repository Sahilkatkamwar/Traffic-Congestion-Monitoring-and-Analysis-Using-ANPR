import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// FastAPI serves web/dist in production, so the build output goes there and
// asset URLs stay relative to the site root.
//
// In dev, vite serves the page and proxies everything the API owns -- including
// the websocket, which needs ws:true or it 404s on upgrade.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', ws: true },
      '/crops': { target: 'http://127.0.0.1:8000' },
      '/media': { target: 'http://127.0.0.1:8000' },
    },
  },
})
