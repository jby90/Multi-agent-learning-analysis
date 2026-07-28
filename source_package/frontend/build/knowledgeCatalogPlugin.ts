import { promises as fs } from 'node:fs'
import path from 'node:path'

import type { Plugin, ResolvedConfig } from 'vite'

import type { KnowledgeCatalogEntry } from '../src/types/trace'


const PUBLIC_ID = 'virtual:knowledge-catalog'
const RESOLVED_ID = `\0${PUBLIC_ID}`
const DIFFICULTIES = new Set<KnowledgeCatalogEntry['difficulty']>([
  'basic',
  'applied',
  'advanced',
])


function unquote(value: string): string {
  const trimmed = value.trim()
  if (
    (trimmed.startsWith('"') && trimmed.endsWith('"'))
    || (trimmed.startsWith("'") && trimmed.endsWith("'"))
  ) {
    return trimmed.slice(1, -1)
  }
  return trimmed
}


function inlineList(value: string): string[] {
  const trimmed = value.trim()
  if (trimmed === '[]') return []
  if (!trimmed.startsWith('[') || !trimmed.endsWith(']')) {
    throw new Error(`知识切片列表格式无效：${value}`)
  }
  return trimmed
    .slice(1, -1)
    .split(',')
    .map(unquote)
    .filter(Boolean)
}


export function parseKnowledgeChunk(source: string): KnowledgeCatalogEntry {
  const frontmatter = source.match(/^---\r?\n([\s\S]*?)\r?\n---(?:\r?\n|$)/)?.[1]
  if (!frontmatter) throw new Error('知识切片缺少 frontmatter')

  const fields = new Map<string, string>()
  for (const line of frontmatter.split(/\r?\n/)) {
    const separator = line.indexOf(':')
    if (separator <= 0) continue
    fields.set(line.slice(0, separator).trim(), line.slice(separator + 1).trim())
  }

  const chunkId = unquote(fields.get('chunk_id') ?? '')
  const knowledgePoint = unquote(fields.get('knowledge_point') ?? '')
  const difficulty = unquote(fields.get('difficulty') ?? '')
  if (!chunkId || !knowledgePoint || !DIFFICULTIES.has(difficulty as KnowledgeCatalogEntry['difficulty'])) {
    throw new Error('知识切片缺少有效的编号、知识点或难度')
  }

  return {
    chunkId,
    knowledgePoint,
    difficulty: difficulty as KnowledgeCatalogEntry['difficulty'],
    prerequisites: inlineList(fields.get('prerequisites') ?? '[]'),
  }
}


export async function loadKnowledgeCatalog(directory: string): Promise<KnowledgeCatalogEntry[]> {
  const names = (await fs.readdir(directory))
    .filter((name) => /^KB-\d+_.+\.md$/u.test(name))
    .sort((left, right) => left.localeCompare(right, 'zh-CN', { numeric: true }))
  return Promise.all(names.map(async (name) => (
    parseKnowledgeChunk(await fs.readFile(path.join(directory, name), 'utf8'))
  )))
}


export function knowledgeCatalogPlugin(): Plugin {
  let config: ResolvedConfig
  let catalogDirectory = ''
  return {
    name: 'ref-knowledge-catalog',
    configResolved(resolved) {
      config = resolved
      catalogDirectory = path.resolve(config.root, '..', 'agents', 'knowledge_base', 'chunks')
    },
    resolveId(id) {
      if (id === PUBLIC_ID) return RESOLVED_ID
      return undefined
    },
    async load(id) {
      if (id !== RESOLVED_ID) return undefined
      const catalog = await loadKnowledgeCatalog(catalogDirectory)
      return `export default ${JSON.stringify(catalog)};`
    },
  }
}
