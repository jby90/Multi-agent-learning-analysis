import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { loadKnowledgeCatalog, parseKnowledgeChunk } from './knowledgeCatalogPlugin'


describe('knowledge catalog build input', () => {
  it('parses the learner-facing metadata from one official knowledge chunk', () => {
    const source = `---
chunk_id: KB-003
knowledge_point: 完成率计算
difficulty: basic
prerequisites: [KB-002]
learning_goal: 能用加权口径计算完成率
---

正文不应进入目录元数据。`

    const actual = parseKnowledgeChunk(source)

    expect(actual).toEqual({
      chunkId: 'KB-003',
      knowledgePoint: '完成率计算',
      difficulty: 'basic',
      prerequisites: ['KB-002'],
    })
  })

  it('loads all ten official chunks from the repository directory', async () => {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const directory = path.resolve(here, '..', '..', 'agents', 'knowledge_base', 'chunks')

    const catalog = await loadKnowledgeCatalog(directory)

    expect(catalog).toHaveLength(10)
    expect(catalog.map((entry) => entry.chunkId)).toEqual([
      'KB-001', 'KB-002', 'KB-003', 'KB-004', 'KB-005',
      'KB-006', 'KB-007', 'KB-008', 'KB-009', 'KB-010',
    ])
  })
})
