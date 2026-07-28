import type {
  DataCollision,
  DebateGroup,
  Keyframe,
  ProfileSnapshot,
  StateId,
  TraceDocument,
  TraceMessage,
  TraceView,
} from '../types/trace'


function latestByType(
  messages: TraceMessage[],
  payloadTypes: string[],
): TraceMessage | undefined {
  return [...messages].reverse().find((message) => payloadTypes.includes(message.payloadType))
}


function dataCollisionAtFrame(messages: TraceMessage[]): DataCollision | undefined {
  const frame = messages.at(-1)
  if (frame?.content.transition_id !== 'T16') return undefined
  const outcome = [...messages].reverse().find(
    (message) => message.content.event === 'probe_outcome'
      && message.content.answer_result === 'correct',
  )
  if (!outcome) return undefined
  const result = [...messages]
    .reverse()
    .find((message) => message.payloadType === 'sql_result'
      && message.step < outcome.step
      && message.content.event === 'query_completed')
  const rows = result?.content.rows
  const row = Array.isArray(rows) && rows.length
    && typeof rows[0] === 'object' && rows[0] !== null && !Array.isArray(rows[0])
    ? rows[0] as Record<string, unknown>
    : undefined
  if (!row || row.plan_qty === undefined || row.actual_qty === undefined) return undefined
  return {
    misconception: typeof outcome.content.target_misconception === 'string'
      ? outcome.content.target_misconception
      : 'M-01',
    wrongLabel: '计划量',
    wrongValue: String(row.plan_qty),
    correctLabel: '实际完成量',
    correctValue: String(row.actual_qty),
    step: frame.step,
  }
}


function profileFrom(messages: TraceMessage[]): {
  profile?: ProfileSnapshot
  knowledgeDimensions: string[]
} {
  const loaded = [...messages].reverse().find(
    (message) => message.content.action === 'profile_loaded',
  )
  if (!loaded) return { knowledgeDimensions: [] }
  const profileValue = loaded.content.profile
  const profile = typeof profileValue === 'object'
    && profileValue !== null
    && !Array.isArray(profileValue)
    ? profileValue as ProfileSnapshot
    : undefined
  const knowledgeDimensions = Array.isArray(loaded.content.knowledge_dimensions)
    ? loaded.content.knowledge_dimensions.filter(
      (item): item is string => typeof item === 'string',
    )
    : []
  return { profile, knowledgeDimensions }
}


function currentState(messages: TraceMessage[]): StateId | string {
  let state: StateId | string = 'S0_INIT'
  for (const message of messages) {
    if (message.rejectedByBus) continue
    if (message.content.action === 'session_start' && typeof message.content.state === 'string') {
      state = message.content.state
    }
    if (
      message.content.action === 'state_transition'
      && typeof message.content.to_state === 'string'
    ) {
      state = message.content.to_state
    }
  }
  return state
}


function debateGroups(messages: TraceMessage[]): DebateGroup[] {
  const groups: DebateGroup[] = []
  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index]
    if (!message || message.role !== 'verdict' || message.verdict?.decision !== 'reject') {
      continue
    }
    const cards: TraceMessage[] = [message]
    for (let cursor = index + 1; cursor < messages.length; cursor += 1) {
      const candidate = messages[cursor]
      if (!candidate) continue
      if (candidate.role === 'rebuttal') cards.push(candidate)
      if (candidate.role === 're_verdict') {
        cards.push(candidate)
        groups.push({ id: message.msgId, messages: cards })
        index = cursor
        break
      }
      if (candidate.role === 'verdict') break
    }
  }
  return groups
}


export function listKeyframes(trace: TraceDocument): Keyframe[] {
  const keyframes: Keyframe[] = []
  for (const message of trace.messages) {
    if (message.role === 'verdict' && message.verdict?.decision === 'reject') {
      keyframes.push({ kind: 'debate', label: '复审开始', step: message.step })
    }
    if (message.content.transition_id === 'T15') {
      keyframes.push({ kind: 'probe', label: '验证任务开始', step: message.step })
    }
    if (message.content.transition_id === 'T16') {
      keyframes.push({ kind: 'collision', label: '数据验证', step: message.step })
    }
    if (message.content.transition_id === 'T17') {
      keyframes.push({ kind: 'step_down', label: '补充讲解', step: message.step })
    }
  }
  return keyframes.sort((left, right) => left.step - right.step)
}


export function buildTraceView(trace: TraceDocument, cursor: number): TraceView {
  const count = Math.max(0, Math.min(Math.trunc(cursor), trace.messages.length))
  const visibleMessages = trace.messages.slice(0, count)
  const profileData = profileFrom(visibleMessages)
  return {
    visibleMessages,
    currentState: currentState(visibleMessages),
    profile: profileData.profile,
    knowledgeDimensions: profileData.knowledgeDimensions,
    diagnosis: latestByType(visibleMessages, ['profile_assessment']),
    lecture: latestByType(visibleMessages, ['lecture_note']),
    task: latestByType(visibleMessages, ['quiz_set', 'practice_guide']),
    sqlResult: latestByType(visibleMessages, ['sql_result']),
    dataCollision: dataCollisionAtFrame(visibleMessages),
    path: latestByType(visibleMessages, ['learning_path_update']),
    debateGroups: debateGroups(visibleMessages),
  }
}
