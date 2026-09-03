import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    host: '0.0.0.0',
    allowedHosts: true,  // required for cloudflared/ngrok tunnels
    port: 5173,
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})