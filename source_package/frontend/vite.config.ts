import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vitest/config'

import { knowledgeCatalogPlugin } from './build/knowledgeCatalogPlugin'
import { profileCatalogPlugin } from './build/profileCatalogPlugin'
import { traceAssetsPlugin } from './build/traceAssetsPlugin'
import { diagnosticExperienceTagsPlugin } from './build/diagnosticExperienceTagsPlugin'

export default defineConfig({
  plugins: [
    vue(),
    knowledgeCatalogPlugin(),
    profileCatalogPlugin(),
    diagnosticExperienceTagsPlugin(),
    traceAssetsPlugin(),
  ],
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
