import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// ponytail: dev proxy instead of backend CORS; set VITE_API_TARGET to point elsewhere.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': process.env.VITE_API_TARGET ?? 'http://localhost:8000' } },
})
