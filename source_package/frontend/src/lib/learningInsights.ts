import type {
  DifficultyLevel,
  KnowledgeCatalogEntry,
  TraceMessage,
  TraceView,
} from '../types/trace'
import { dataFieldLabel } from './tracePresentation'


export type ResourceSource = '岗位微课' | '实操任务'

export interface ResourceCoverageItem {
  name: string
  covered: boolean
  sources: ResourceSource[]
}

export interface ResourceCoverage {
  covered: number
  total: number
  items: ResourceCoverageItem[]
}

export type DifficultyStage = 'assessment' | 'lecture' | 'practice' | 'validation' | 'advanced'
export type DifficultyAction = 'step_up' | 'step_down'

export interface DifficultyPoint {
  stage: DifficultyStage
  label: string
  level: DifficultyLevel
  action?: DifficultyAction
}

export interface DifficultyJourney {
  points: DifficultyPoint[]
  currentResourceIndex: number
}

export interface LearningPlan {
  knowledgePoint: string
  difficulty: DifficultyLevel
  prerequisiteNames: string[]
  reason: string
}

interface CorrectionEvidence {
  wrongLabel: string
  wrongValue: string
  correctLabel: string
  correctValue: string
}


const LEVELS: DifficultyLevel[] = ['basic', 'applied', 'advanced']


function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}


function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && item.trim() !== '')
    : []
}


function difficulty(value: unknown): DifficultyLevel | undefined {
  return LEVELS.includes(value as DifficultyLevel) ? value as DifficultyLevel : undefined
}


function action(value: unknown): DifficultyAction | undefined {
  if (value === 'step_up' || value === 'step_down') return value
  return undefined
}


function applyAction(level: DifficultyLevel, nextAction?: DifficultyAction): DifficultyLevel {
  const index = LEVELS.indexOf(level)
  if (nextAction === 'step_up') return LEVELS[Math.min(index + 1, LEVELS.length - 1)] ?? level
  if (nextAction === 'step_down') return LEVELS[Math.max(index - 1, 0)] ?? level
  return level
}


function addSource(
  map: Map<string, Set<ResourceSource>>,
  point: string | undefined,
  source: ResourceSource,
): void {
  if (!point) return
  const sources = map.get(point) ?? new Set<ResourceSource>()
  sources.add(source)
  map.set(point, sources)
}


function learningTask(view: TraceView): TraceMessage | undefined {
  return [...view.visibleMessages].reverse().find((message) => (
    (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
    && stringValue(message.content.knowledge_point) !== undefined
  ))
}


export function resourceCoverage(view: TraceView): ResourceCoverage {
  const sources = new Map<string, Set<ResourceSource>>()
  addSource(sources, stringValue(view.lecture?.content.knowledge_point), '岗位微课')
  for (const point of stringList(view.lecture?.content.coverage)) {
    addSource(sources, point, '岗位微课')
  }
  addSource(sources, stringValue(learningTask(view)?.content.knowledge_point), '实操任务')

  const items = stringList(view.diagnosis?.content.blind_spots).map((name) => ({
    name,
    covered: sources.has(name),
    sources: [...(sources.get(name) ?? [])],
  }))
  return {
    covered: items.filter((item) => item.covered).length,
    total: items.length,
    items,
  }
}


function reviewedTaskAction(
  view: TraceView,
  task: TraceMessage | undefined,
): DifficultyAction | undefined {
  if (!task) return undefined
  const review = [...view.visibleMessages].reverse().find((message) => (
    message.payloadType === 'review_verdict'
    && message.content.reviewed_msg_id === task.msgId
  ))
  return action(review?.verdict?.difficultyAction)
    ?? action(review?.content.difficulty_action)
}


function catalogLevel(
  point: unknown,
  catalog: KnowledgeCatalogEntry[],
): DifficultyLevel | undefined {
  const name = stringValue(point)
  return name
    ? catalog.find((entry) => entry.knowledgePoint === name)?.difficulty
    : undefined
}


export function difficultyJourney(
  view: TraceView,
  catalog: KnowledgeCatalogEntry[],
): DifficultyJourney {
  const points: DifficultyPoint[] = []
  const assessed = difficulty(view.diagnosis?.content.difficulty)
  if (assessed) {
    points.push({ stage: 'assessment', label: '测评', level: assessed })
  }

  if (view.lecture) {
    const level = catalogLevel(view.lecture.content.knowledge_point, catalog)
      ?? points.at(-1)?.level
      ?? 'basic'
    points.push({ stage: 'lecture', label: '微课', level })
  }

  const task = learningTask(view)
  if (task) {
    const level = difficulty(task.content.difficulty)
      ?? points.at(-1)?.level
      ?? 'basic'
    points.push({ stage: 'practice', label: '实操', level })
  }

  const taskAction = reviewedTaskAction(view, task)
  if (task && view.sqlResult) {
    const prior = points.at(-1)?.level ?? 'basic'
    points.push({
      stage: 'validation',
      label: '验证',
      level: applyAction(prior, taskAction),
      ...(taskAction ? { action: taskAction } : {}),
    })
  }

  if (view.path) {
    const prior = points.at(-1)?.level ?? assessed ?? 'basic'
    const nextAction = action(view.path.content.difficulty_action)
    points.push({
      stage: 'advanced',
      label: '进阶',
      level: applyAction(prior, nextAction),
      ...(nextAction ? { action: nextAction } : {}),
    })
  }

  const practiceIndex = points.findIndex((point) => point.stage === 'practice')
  const lectureIndex = points.findIndex((point) => point.stage === 'lecture')
  return {
    points,
    currentResourceIndex: practiceIndex >= 0 ? practiceIndex : Math.max(lectureIndex, 0),
  }
}


function topologicalCatalog(catalog: KnowledgeCatalogEntry[]): KnowledgeCatalogEntry[] {
  const byId = new Map(catalog.map((entry) => [entry.chunkId, entry]))
  const ordered: KnowledgeCatalogEntry[] = []
  const active = new Set<string>()
  const complete = new Set<string>()

  const visit = (entry: KnowledgeCatalogEntry): void => {
    if (complete.has(entry.chunkId)) return
    if (active.has(entry.chunkId)) throw new Error(`知识切片先修关系存在循环：${entry.chunkId}`)
    active.add(entry.chunkId)
    for (const prerequisite of entry.prerequisites) {
      const required = byId.get(prerequisite)
      if (required) visit(required)
    }
    active.delete(entry.chunkId)
    complete.add(entry.chunkId)
    ordered.push(entry)
  }

  for (const entry of [...catalog].sort((left, right) => (
    left.chunkId.localeCompare(right.chunkId, 'zh-CN', { numeric: true })
  ))) visit(entry)
  return ordered
}


export function nextLearningPlan(
  view: TraceView,
  catalog: KnowledgeCatalogEntry[],
): LearningPlan | undefined {
  const coverage = resourceCoverage(view)
  const remaining = new Set(
    coverage.items.filter((item) => !item.covered).map((item) => item.name),
  )
  const candidate = topologicalCatalog(catalog).find((entry) => (
    remaining.has(entry.knowledgePoint)
  ))
  if (!candidate) return undefined

  const byId = new Map(catalog.map((entry) => [entry.chunkId, entry]))
  const prerequisiteNames = candidate.prerequisites.flatMap((chunkId) => {
    const point = byId.get(chunkId)?.knowledgePoint
    return point ? [point] : []
  })
  const nextDifficulty = difficultyJourney(view, catalog).points.at(-1)?.level
    ?? candidate.difficulty
  const reason = prerequisiteNames.length
    ? `承接${prerequisiteNames.join('、')}，继续补齐尚未覆盖的知识盲区。`
    : '从尚未覆盖的知识盲区中，按课程先后顺序继续学习。'
  return {
    knowledgePoint: candidate.knowledgePoint,
    difficulty: nextDifficulty,
    prerequisiteNames,
    reason,
  }
}


function firstResultRow(message: TraceMessage): Record<string, unknown> | undefined {
  const rows = message.content.rows
  if (!Array.isArray(rows)) return undefined
  const row = rows[0]
  return typeof row === 'object' && row !== null && !Array.isArray(row)
    ? row as Record<string, unknown>
    : undefined
}


function correctionEvidence(view: TraceView): CorrectionEvidence | undefined {
  const outcome = [...view.visibleMessages].reverse().find((message) => (
    message.content.event === 'probe_outcome'
    && message.content.answer_result === 'correct'
    && stringValue(message.content.target_misconception) !== undefined
  ))
  if (!outcome) return undefined

  const result = [...view.visibleMessages].reverse().find((message) => (
    message.step < outcome.step
    && message.payloadType === 'sql_result'
    && message.content.event === 'query_completed'
  ))
  if (!result) return undefined
  const columns = stringList(result.content.columns)
  const row = firstResultRow(result)
  if (!row) return undefined

  const wrongField = columns.includes('plan_qty') ? 'plan_qty' : columns[0]
  const correctField = columns.includes('actual_qty') ? 'actual_qty' : columns[1]
  if (!wrongField || !correctField || row[wrongField] === undefined || row[correctField] === undefined) {
    return undefined
  }
  return {
    wrongLabel: dataFieldLabel(wrongField),
    wrongValue: String(row[wrongField]),
    correctLabel: dataFieldLabel(correctField),
    correctValue: String(row[correctField]),
  }
}


export function learningSummary(
  view: TraceView,
  catalog: KnowledgeCatalogEntry[],
): string | undefined {
  if (!view.path) return undefined
  const plan = nextLearningPlan(view, catalog)
  if (!plan) return undefined
  const nextPoint = plan.knowledgePoint

  const correction = correctionEvidence(view)
  if (correction) {
    const values = `${correction.wrongLabel}${correction.wrongValue}、${correction.correctLabel}${correction.correctValue}`
    return `刚才你把${correction.wrongLabel}当成了${correction.correctLabel}——你自己查出的数据（${values}）纠正了这一点。下一步：${nextPoint}。`
  }

  const currentPoint = stringValue(view.lecture?.content.knowledge_point)
    ?? stringValue(learningTask(view)?.content.knowledge_point)
    ?? stringValue(view.path.content.current_node)
  return currentPoint
    ? `你完成了${currentPoint}的学习与实操。下一步：${nextPoint}。`
    : undefined
}
