import { resolve } from 'path'
import { defineConfig, externalizeDepsPlugin } from 'electron-vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  main: {
    plugins: [externalizeDepsPlugin()],
    build: {
      rollupOptions: { input: { index: resolve(__dirname, 'src/main/index.ts') } }
    }
  },
  preload: {
    plugins: [externalizeDepsPlugin()],
    build: {
      rollupOptions: { input: { index: resolve(__dirname, 'src/preload/index.ts') } }
    }
  },
  renderer: {
    root: 'src/renderer',
    resolve: {
      alias: [
        {
          // monaco-editor 0.56's package.json exports map redirects every
          // subpath into esm/vs/, breaking the canonical deep worker imports
          // ("monaco-editor/esm/vs/editor/editor.worker?worker"). Alias them
          // to the real files before exports resolution sees them.
          find: /^monaco-editor\/esm\//,
          replacement: resolve(__dirname, 'node_modules/monaco-editor/esm/') + '/',
        },
      ],
    },
    optimizeDeps: {
      // Dev-only: the dep optimizer can't handle `?worker` entries (it tries
      // to pre-bundle them as regular deps and crashes). Serving monaco's
      // plain ESM unoptimized is the standard monaco + Vite dev setup.
      exclude: ['monaco-editor'],
    },
    build: {
      rollupOptions: { input: { index: resolve(__dirname, 'src/renderer/index.html') } }
    },
    plugins: [react()]
  }
})
