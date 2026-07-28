import type {
  AgentId,
  RoleId,
  RuleHit,
  TraceClaim,
  TraceDocument,
  TraceEvidence,
  TraceMessage,
  TraceTokenUsage,
  TraceVerdict,
} from '../types/trace'


const AGENTS = new Set<AgentId>([
  'diagnosis',
  'knowledge',
  'task',
  'verification',
  'review',
  'system',
])

const ROLES = new Set<RoleId>([
  'produce',
  'verdict',
  'rebuttal',
  're_verdict',
  'probe',
  'system',
])


export class TraceParseError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'TraceParseError'
  }
}


function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}


function requiredString(
  value: unknown,
  fileName: string,
  lineNumber: number,
  label: string,
): string {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new TraceParseError(`${fileName}第${lineNumber}行缺少${label}`)
  }
  return value
}


function optionalRecord(value: unknown): Record<string, unknown> | undefined {
  return isRecord(value) ? value : undefined
}


function parseEvidence(value: unknown): TraceEvidence[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!isRecord(item) || typeof item.kind !== 'string' || typeof item.ref !== 'string') {
      return []
    }
    const evidence: TraceEvidence = { kind: item.kind, ref: item.ref }
    if (typeof item.quote === 'string') evidence.quote = item.quote
    if (typeof item.supports_claim === 'string') {
      evidence.supportsClaim = item.supports_claim
    }
    return [evidence]
  })
}


function parseClaims(value: unknown): TraceClaim[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!isRecord(item) || typeof item.text !== 'string' || typeof item.kind !== 'string') {
      return []
    }
    return [{ text: item.text, kind: item.kind }]
  })
}


function parseRuleHits(value: unknown): RuleHit[] {
  if (!Array.isArray(value)) return []
  return value.flatMap((item) => {
    if (!isRecord(item) || typeof item.rule_id !== 'string' || typeof item.reason !== 'string') {
      return []
    }
    const hit: RuleHit = { ruleId: item.rule_id, reason: item.reason }
    if (typeof item.evidence_ref === 'string') hit.evidenceRef = item.evidence_ref
    return [hit]
  })
}


function parseVerdict(value: unknown): TraceVerdict | undefined {
  if (!isRecord(value) || typeof value.decision !== 'string') return undefined
  const verdict: TraceVerdict = {
    decision: value.decision,
    ruleHits: parseRuleHits(value.rule_hits),
  }
  if (typeof value.difficulty_action === 'string') {
    verdict.difficultyAction = value.difficulty_action
  }
  return verdict
}


function parseTokenUsage(value: unknown): TraceTokenUsage | undefined {
  if (!isRecord(value)) return undefined
  const prompt = value.prompt_tokens
  const completion = value.completion_tokens
  const total = value.total_tokens
  if (
    typeof prompt !== 'number'
    || typeof completion !== 'number'
    || typeof total !== 'number'
  ) return undefined
  return { promptTokens: prompt, completionTokens: completion, totalTokens: total }
}


function parseMessage(
  value: unknown,
  fileName: string,
  lineNumber: number,
): TraceMessage {
  if (!isRecord(value)) {
    throw new TraceParseError(`${fileName}第${lineNumber}行不是会话消息`)
  }
  const traceId = requiredString(value.trace_id, fileName, lineNumber, '会话编号')
  const msgId = requiredString(value.msg_id, fileName, lineNumber, '消息编号')
  if (!Number.isInteger(value.step) || typeof value.step !== 'number' || value.step < 1) {
    throw new TraceParseError(`${fileName}第${lineNumber}行的步骤编号无效`)
  }
  const agentValue = requiredString(value.agent, fileName, lineNumber, '发出角色')
  const roleValue = requiredString(value.role, fileName, lineNumber, '消息动作')
  if (!AGENTS.has(agentValue as AgentId)) {
    throw new TraceParseError(`${fileName}第${lineNumber}行含未识别的发出角色`)
  }
  if (!ROLES.has(roleValue as RoleId)) {
    throw new TraceParseError(`${fileName}第${lineNumber}行含未识别的消息动作`)
  }
  if (!isRecord(value.payload)) {
    throw new TraceParseError(`${fileName}第${lineNumber}行缺少消息内容`)
  }
  const payloadType = requiredString(
    value.payload.type,
    fileName,
    lineNumber,
    '内容类别',
  )
  if (!isRecord(value.payload.content)) {
    throw new TraceParseError(`${fileName}第${lineNumber}行的消息内容无效`)
  }
  const timestamp = requiredString(value.timestamp, fileName, lineNumber, '记录时间')
  const message: TraceMessage = {
    msgId,
    traceId,
    step: value.step,
    agent: agentValue as AgentId,
    role: roleValue as RoleId,
    payloadType,
    content: value.payload.content,
    evidence: parseEvidence(value.evidence),
    claims: parseClaims(value.claims),
    timestamp,
    rejectedByBus: value.rejected_by_bus === true,
    busErrors: Array.isArray(value.bus_errors)
      ? value.bus_errors.filter((item): item is string => typeof item === 'string')
      : [],
    raw: value,
  }
  const verdict = parseVerdict(value.verdict)
  if (verdict) message.verdict = verdict
  const retry = optionalRecord(value.retry)
  if (retry) message.retry = retry
  const probe = optionalRecord(value.probe)
  if (probe) message.probe = probe
  if (typeof value.student_profile_ref === 'string') {
    message.studentProfileRef = value.student_profile_ref
  }
  if (typeof value.latency_ms === 'number') message.latencyMs = value.latency_ms
  if (typeof value.model === 'string') message.model = value.model
  const tokenUsage = parseTokenUsage(value.token_usage)
  if (tokenUsage) message.tokenUsage = tokenUsage
  return message
}


export function parseTraceJsonl(source: string, fileName: string): TraceDocument {
  const messages: TraceMessage[] = []
  source.split(/\r?\n/).forEach((line, index) => {
    if (line.trim() === '') return
    let value: unknown
    try {
      value = JSON.parse(line)
    } catch {
      throw new TraceParseError(`${fileName}第${index + 1}行不是有效的会话记录`)
    }
    messages.push(parseMessage(value, fileName, index + 1))
  })
  if (messages.length === 0) {
    throw new TraceParseError(`${fileName}没有可回放的会话记录`)
  }
  const traceIds = new Set(messages.map((message) => message.traceId))
  if (traceIds.size !== 1) {
    throw new TraceParseError(`${fileName}包含多个会话编号`)
  }
  const steps = messages.map((message) => message.step)
  if (new Set(steps).size !== steps.length) {
    throw new TraceParseError(`${fileName}步骤编号重复`)
  }
  messages.sort((left, right) => left.step - right.step)
  if (messages.some((message, index) => message.step !== index + 1)) {
    throw new TraceParseError(`${fileName}步骤编号必须从1连续递增`)
  }
  const sessionStart = messages.find(
    (message) => message.payloadType === 'control'
      && message.content.action === 'session_start',
  )
  if (!sessionStart) {
    throw new TraceParseError(`${fileName}缺少会话建立记录`)
  }
  const profileValue = sessionStart.content.student_profile_ref
  return {
    fileName,
    traceId: messages[0]?.traceId ?? '',
    profileId: typeof profileValue === 'string'
      ? profileValue
      : sessionStart.studentProfileRef,
    messages,
  }
}
