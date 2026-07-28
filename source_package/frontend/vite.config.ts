import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

import { knowledgeCatalogPlugin } from './build/knowledgeCatalogPlugin'
import { profileCatalogPlugin } from './build/profileCatalogPlugin'
import { traceAssetsPlugin } from './build/traceAssetsPlugin'

export default defineConfig({
  plugins: [vue(), knowledgeCatalogPlugin(), profileCatalogPlugin(), traceAssetsPlugin()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
  },
})
