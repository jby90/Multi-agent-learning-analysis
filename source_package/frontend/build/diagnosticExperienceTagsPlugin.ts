import { promises as fs } from 'node:fs'
import path from 'node:path'

import type { Plugin, ResolvedConfig } from 'vite'

import type { DiagnosticExperienceTag } from '../src/types/diagnostic'


const PUBLIC_ID = 'virtual:diagnostic-experience-tags'
const RESOLVED_ID = `\0${PUBLIC_ID}`

function requiredString(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`diagnostic experience tag is missing ${field}`)
  }
  return value.trim()
}

export function parseDiagnosticExperienceTags(source: string): DiagnosticExperienceTag[] {
  const value: unknown = JSON.parse(source)
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error('diagnostic experience tag configuration must be an object')
  }
  const tags = (value as { tags?: unknown }).tags
  if (!Array.isArray(tags)) {
    throw new Error('diagnostic experience tag configuration must contain tags')
  }
  const parsed = tags.map((item) => {
    if (typeof item !== 'object' || item === null || Array.isArray(item)) {
      throw new Error('diagnostic experience tag must be an object')
    }
    const record = item as Record<string, unknown>
    return {
      tag_id: requiredString(record.tag_id, 'tag_id'),
      knowledge_point: requiredString(record.knowledge_point, 'knowledge_point'),
      label: requiredString(record.label, 'label'),
    }
  })
  if (new Set(parsed.map((item) => item.tag_id)).size !== parsed.length) {
    throw new Error('diagnostic experience tag IDs must be unique')
  }
  return parsed
}

export function diagnosticExperienceTagsPlugin(): Plugin {
  let filePath = ''
  return {
    name: 'ref-diagnostic-experience-tags',
    configResolved(config: ResolvedConfig) {
      filePath = path.resolve(config.root, '..', 'config', 'diagnostic_experience_tags_v3.json')
    },
    resolveId(id) {
      if (id === PUBLIC_ID) return RESOLVED_ID
      return undefined
    },
    async load(id) {
      if (id !== RESOLVED_ID) return undefined
      const tags = parseDiagnosticExperienceTags(await fs.readFile(filePath, 'utf8'))
      return `export default ${JSON.stringify(tags)};`
    },
  }
}
