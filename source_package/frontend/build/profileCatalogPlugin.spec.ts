import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

import { loadLearnerProfiles, parseLearnerProfile } from './profileCatalogPlugin'


describe('learner profile build input', () => {
  it('projects only approved human-facing fields', () => {
    const profile = parseLearnerProfile(JSON.stringify({
      profile_id: 'planner_new',
      title: '新入职生产计划员',
      background: '会数据分析工具，需要学习生产口径',
      strengths: ['SQL基础'],
      knowledge_scope: ['三道工序与传导关系', '计划量与实际量口径'],
      lecture_style: '这是给模型的指令',
      difficulty_start: 'basic',
    }), 'planner_new.json')

    expect(profile).toEqual({
      id: 'planner_new',
      title: '新入职生产计划员',
      background: '会数据分析工具，需要学习生产口径',
      strengths: ['SQL基础'],
      knowledgeScope: ['三道工序与传导关系', '计划量与实际量口径'],
      practiceMode: 'sql',
    })
    expect(profile).not.toHaveProperty('lecture_style')
    expect(profile).not.toHaveProperty('difficulty_start')
  })

  it('loads the three approved profiles from repository data', async () => {
    const here = path.dirname(fileURLToPath(import.meta.url))
    const directory = path.resolve(here, '..', '..', 'agents', 'profiles')

    const profiles = await loadLearnerProfiles(directory)

    expect(profiles.map((profile) => profile.id)).toEqual([
      'planner_new', 'craft_engineer', 'line_leader',
    ])
    expect(profiles.every((profile) => profile.title && profile.background)).toBe(true)
  })
})
