import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Port 5173 is not a preference. It is the port the `web` service occupies in
// the compose file and the only origin the API's CORS middleware allows
// (backend/app/main.py). Changing it here means changing it in two other
// places, so it is pinned rather than left to whatever Vite picks.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: true,
    // The API is reached through this proxy in development, so the browser
    // makes same-origin requests and the app ships one base URL — '' — in
    // both environments. VITE_API_BASE overrides it when the API is elsewhere.
    proxy: {
      '/api': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
      '/health': {
        target: process.env.VITE_API_TARGET || 'http://localhost:8000',
        changeOrigin: true,
      },
    },
  },
  build: { outDir: 'dist', sourcemap: true },
  test: {
    environment: 'node',
    include: ['src/**/*.test.js', 'src/**/*.test.jsx'],
  },
})
