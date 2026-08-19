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


function practiceTaskMessage(view: TraceView): TraceMessage | undefined {
  // The practice/validation trajectory stages describe the hands-on task
  // the student executed SQL against (event "product_ready").  Conclusion
  // assessments ("assessment_ready") and follow-up questions
  // ("follow_up_question_ready") are graded checkpoints; their R-03
  // verdicts carry advisory difficulty_action labels that may default to
  // step_down without any real difficulty change, so they must never be
  // treated as trajectory decisions.
  return [...view.visibleMessages].reverse().find((message) => (
    (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
    && stringValue(message.content.knowledge_point) !== undefined
    && (message.content.event === undefined || message.content.event === 'product_ready')
  ))
}


export function resourceCoverage(view: TraceView): ResourceCoverage {
  // 覆盖按全会话累计：每一档微课的铺垫面与每次实操任务的主题都保留。
  // 更换难度单元时新微课的铺垫集合可能不同（例如应用档铺垫责任单元、
  // 进阶档铺垫三道工序），若只看当前帧会出现"越学覆盖越少"的回落；
  // 累计语义与「本次会话资源覆盖」文案一致，回放中数字只增不减。
  const sources = new Map<string, Set<ResourceSource>>()
  for (const message of view.visibleMessages) {
    if (message.rejectedByBus) continue
    if (message.payloadType === 'lecture_note') {
      addSource(sources, stringValue(message.content.knowledge_point), '岗位微课')
      for (const point of stringList(message.content.coverage)) {
        addSource(sources, point, '岗位微课')
      }
    } else if (
      message.agent === 'task'
      && message.role === 'produce'
      && (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
      && (message.content.event === undefined || message.content.event === 'product_ready')
    ) {
      addSource(sources, stringValue(message.content.knowledge_point), '实操任务')
    }
  }

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
  authoritativeCurrentDifficulty?: DifficultyLevel | string | null,
): DifficultyJourney {
  const points: DifficultyPoint[] = []
  const assessed = difficulty(view.diagnosis?.content.selected_difficulty)
    ?? difficulty(view.diagnosis?.content.difficulty)
  if (assessed) {
    points.push({ stage: 'assessment', label: '测评', level: assessed })
  }

  if (view.lecture) {
    const level = difficulty(view.lecture.content.difficulty)
      ?? catalogLevel(view.lecture.content.knowledge_point, catalog)
      ?? points.at(-1)?.level
      ?? 'basic'
    points.push({ stage: 'lecture', label: '微课', level })
  }

  const task = practiceTaskMessage(view)
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
    const explicitLevel = difficulty(view.path.content.difficulty)
    const practiceLevel = points.find((point) => point.stage === 'practice')?.level
    let lastPathIdx = -1
    let lastTaskIdx = -1
    for (let i = 0; i < view.visibleMessages.length; i++) {
      const msg = view.visibleMessages[i]
      if (msg.payloadType === 'learning_path_update') lastPathIdx = i
      if ((msg.payloadType === 'quiz_set' || msg.payloadType === 'practice_guide')
        && typeof msg.content?.knowledge_point === 'string') lastTaskIdx = i
    }
    const taskAfterPath = lastTaskIdx > lastPathIdx
    const steppedDown = taskAfterPath && practiceLevel && explicitLevel
      && LEVELS.indexOf(explicitLevel) > LEVELS.indexOf(practiceLevel)
    points.push({
      stage: 'advanced',
      label: '进阶',
      level: steppedDown ? practiceLevel : (explicitLevel ?? applyAction(prior, nextAction)),
      ...(!steppedDown && nextAction ? { action: nextAction } : {}),
    })
  }

  const authoritativeLevel = difficulty(authoritativeCurrentDifficulty)
  const currentPoint = points.at(-1)
  if (authoritativeLevel && currentPoint) {
    if (currentPoint.level !== authoritativeLevel) {
      currentPoint.level = authoritativeLevel
      delete currentPoint.action
    }
  }

  const practiceIndex = points.findIndex((point) => point.stage === 'practice')
  const lectureIndex = points.findIndex((point) => point.stage === 'lecture')
  const stateStageMap: Record<string, DifficultyStage> = {
    S0_INIT: 'assessment',
    S1_DIAGNOSIS: 'assessment',
    S2_KNOWLEDGE: 'lecture',
    S3_TASK: 'practice',
    S4_VERIFY: 'practice',
    S5_REVIEW: 'validation',
    S6_DEBATE: 'validation',
    S7_STUDENT: 'validation',
    S8_PROBE: 'validation',
    S9_PATH_UPDATE: 'advanced',
    S10_DONE: 'advanced',
    S_FAIL: 'advanced',
  }
  const mappedStage = stateStageMap[view.currentState]
  const mappedIndex = mappedStage
    ? points.findIndex((point) => point.stage === mappedStage)
    : -1
  return {
    points,
    currentResourceIndex: mappedIndex >= 0
      ? mappedIndex
      : (practiceIndex >= 0 ? practiceIndex : Math.max(lectureIndex, 0)),
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
  // 下一知识点尚未开始，其起始档由它自己的岗前测评路由决定、暂未可知；
  // 这里给目录基准档（稳定值），不再跟随当前单元的实时难度闪烁。
  const nextDifficulty = candidate.difficulty
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
    // 纠错模板只描述计划量/实际完成量口径混淆。其他口径的结果列（如
    // 船号/完成率）不构成"把X当成Y"的证据，与数据对撞帧的判定对齐。
    && stringList(message.content.columns).includes('plan_qty')
    && stringList(message.content.columns).includes('actual_qty')
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
