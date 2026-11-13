import { readFileSync } from 'node:fs'
import { fileURLToPath, URL } from 'node:url'

import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

const version = readFileSync(new URL('../VERSION', import.meta.url), 'utf8').trim()

export default defineConfig({
  plugins: [
    vue(),
    {
      name: 'app-version',
      transformIndexHtml() {
        return [{ tag: 'meta', attrs: { name: 'application-version', content: version } }]
      },
    },
  ],
  base: '/',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: fileURLToPath(new URL('../static/dist', import.meta.url)),
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8787',
      '^/s/': 'http://127.0.0.1:8787',
      '^/i/': 'http://127.0.0.1:8787',
    },
  },
})
