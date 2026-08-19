import { promises as fs } from 'node:fs'
import path from 'node:path'

import type { Plugin, ResolvedConfig } from 'vite'

import type { LearnerProfileOption } from '../src/types/profile'


const PUBLIC_ID = 'virtual:profile-catalog'
const RESOLVED_ID = `\0${PUBLIC_ID}`
const PROFILE_IDS = ['planner_new', 'craft_engineer', 'line_leader'] as const


function stringField(value: unknown, field: string, fileName: string): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new Error(`${fileName}缺少有效的${field}`)
  }
  return value.trim()
}


export function parseLearnerProfile(source: string, fileName: string): LearnerProfileOption {
  let value: unknown
  try {
    value = JSON.parse(source)
  } catch {
    throw new Error(`${fileName}不是有效的画像数据`)
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    throw new Error(`${fileName}不是有效的画像数据`)
  }
  const profile = value as Record<string, unknown>
  const strengths = Array.isArray(profile.strengths)
    ? profile.strengths.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
    : []
  return {
    id: stringField(profile.profile_id, '画像编号', fileName),
    title: stringField(profile.title, '岗位名称', fileName),
    background: stringField(profile.background, '岗位背景', fileName),
    strengths,
    // 闭环一：画像学习领域清单（训练关注点按域过滤的数据源）
    knowledgeScope: Array.isArray(profile.knowledge_scope)
      ? profile.knowledge_scope.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
      : [],
    // 闭环五：实操模式（sql=学员书写；data_present=系统代执行并呈现）
    practiceMode: typeof profile.practice_mode === 'string' ? profile.practice_mode : 'sql',
  }
}


export async function loadLearnerProfiles(directory: string): Promise<LearnerProfileOption[]> {
  return Promise.all(PROFILE_IDS.map(async (profileId) => {
    const fileName = `${profileId}.json`
    const profile = parseLearnerProfile(
      await fs.readFile(path.join(directory, fileName), 'utf8'),
      fileName,
    )
    if (profile.id !== profileId) {
      throw new Error(`${fileName}的画像编号与文件名不一致`)
    }
    return profile
  }))
}


export function profileCatalogPlugin(): Plugin {
  let directory = ''
  return {
    name: 'ref-profile-catalog',
    configResolved(config: ResolvedConfig) {
      directory = path.resolve(config.root, '..', 'agents', 'profiles')
    },
    resolveId(id) {
      if (id === PUBLIC_ID) return RESOLVED_ID
      return undefined
    },
    async load(id) {
      if (id !== RESOLVED_ID) return undefined
      return `export default ${JSON.stringify(await loadLearnerProfiles(directory))};`
    },
  }
}
