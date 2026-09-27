import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import path from 'path'

export default defineConfig({
  root: 'src/renderer',
  plugins: [react()],
  base: './',
  build: {
    outDir: path.resolve(__dirname, 'dist/renderer'),
    emptyOutDir: true,
    rollupOptions: {
      // Two HTML entries: the main window and the frameless voice overlay.
      input: {
        index: path.resolve(__dirname, 'src/renderer/index.html'),
        overlay: path.resolve(__dirname, 'src/renderer/overlay.html'),
      },
    },
  },
  server: {
    port: 5173,
  },
  // vitest: colocated *.test.js under the root. Node, not jsdom — the units
  // under test (the conversation store, the API client) touch no DOM.
  test: {
    environment: 'node',
    include: ['**/*.test.{js,jsx}'],
  },
})
