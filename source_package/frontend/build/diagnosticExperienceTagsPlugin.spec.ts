import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { promises as fs } from 'node:fs'

import { describe, expect, it } from 'vitest'

import { parseDiagnosticExperienceTags } from './diagnosticExperienceTagsPlugin'

describe('diagnostic experience tag build input', () => {
  it('loads ten learner-visible route conditions from the frozen backend configuration', async () => {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const source = await fs.readFile(
      path.resolve(here, '..', '..', 'config', 'diagnostic_experience_tags_v3.json'),
      'utf8',
    )

    const tags = parseDiagnosticExperienceTags(source)

    expect(tags).toHaveLength(10)
    expect(tags.map((item) => item.knowledge_point)).toContain('偏差率与风险等级')
    expect(tags.map((item) => item.knowledge_point)).toContain('异常衰减规律')
  })
})
