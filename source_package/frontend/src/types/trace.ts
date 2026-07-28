export type AgentId =
  | 'diagnosis'
  | 'knowledge'
  | 'task'
  | 'verification'
  | 'review'
  | 'system'

export type RoleId =
  | 'produce'
  | 'verdict'
  | 'rebuttal'
  | 're_verdict'
  | 'probe'
  | 'system'

export type StateId =
  | 'S0_INIT'
  | 'S1_DIAGNOSIS'
  | 'S2_KNOWLEDGE'
  | 'S3_TASK'
  | 'S4_VERIFY'
  | 'S5_REVIEW'
  | 'S6_DEBATE'
  | 'S7_STUDENT'
  | 'S8_PROBE'
  | 'S9_PATH_UPDATE'
  | 'S10_DONE'
  | 'S_FAIL'

export interface TraceEvidence {
  kind: string
  ref: string
  quote?: string
  supportsClaim?: string
}

export interface TraceClaim {
  text: string
  kind: 'fact' | 'data_conclusion' | 'speculation' | string
}

export interface RuleHit {
  ruleId: string
  reason: string
  evidenceRef?: string
}

export interface TraceVerdict {
  decision: 'approve' | 'reject' | 'approve_with_fix' | string
  ruleHits: RuleHit[]
  difficultyAction?: string
}

export interface TraceTokenUsage {
  promptTokens: number
  completionTokens: number
  totalTokens: number
}

export interface TraceMessage {
  msgId: string
  traceId: string
  step: number
  agent: AgentId
  role: RoleId
  payloadType: string
  content: Record<string, unknown>
  evidence: TraceEvidence[]
  claims: TraceClaim[]
  verdict?: TraceVerdict
  retry?: Record<string, unknown>
  probe?: Record<string, unknown>
  studentProfileRef?: string
  timestamp: string
  latencyMs?: number
  model?: string
  tokenUsage?: TraceTokenUsage
  rejectedByBus: boolean
  busErrors: string[]
  raw?: Record<string, unknown>
}

export interface TraceDocument {
  fileName: string
  traceId: string
  profileId?: string
  messages: TraceMessage[]
}

export interface ProfileSnapshot extends Record<string, unknown> {
  profile_id?: string
  title?: string
  background?: string
  strengths?: unknown[]
  gaps_prior?: unknown[]
  difficulty_start?: string
}

export interface TruthBadge {
  label: string
  tone: 'cached' | 'fallback'
}

export interface Keyframe {
  kind: 'debate' | 'probe' | 'collision' | 'step_down'
  label: string
  step: number
}

export interface DataCollision {
  misconception: string
  wrongLabel: string
  wrongValue: string
  correctLabel: string
  correctValue: string
  step: number
}

export interface DebateGroup {
  id: string
  messages: TraceMessage[]
}

export interface TraceView {
  visibleMessages: TraceMessage[]
  currentState: StateId | string
  profile?: ProfileSnapshot
  knowledgeDimensions: string[]
  diagnosis?: TraceMessage
  lecture?: TraceMessage
  task?: TraceMessage
  sqlResult?: TraceMessage
  dataCollision?: DataCollision
  path?: TraceMessage
  debateGroups: DebateGroup[]
}

export interface TraceManifestEntry {
  fileName: string
  bytes: number
}

export type DifficultyLevel = 'basic' | 'applied' | 'advanced'

export interface KnowledgeCatalogEntry {
  chunkId: string
  knowledgePoint: string
  difficulty: DifficultyLevel
  prerequisites: string[]
}
