import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/webhook': 'http://localhost:8000',
      '/stream': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/simulate': 'http://localhost:8000',
      '/policy': 'http://localhost:8000',
      '/health': 'http://localhost:8000',
      '/traces': 'http://localhost:8000',
      '/issuers': 'http://localhost:8000',
      '/retries': 'http://localhost:8000',
    },
  },
})
