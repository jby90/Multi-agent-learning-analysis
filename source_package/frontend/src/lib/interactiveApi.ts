export interface InteractiveProfile extends Record<string, unknown> {
  profile_id: string
  title: string
}

export interface InteractiveLearningContract extends Record<string, unknown> {
  contract_id: string
  domain_id: string
  domain_package_sha256: string
  difficulty: string
  target_knowledge_points: string[]
  misconceptions: string[]
}

export interface InteractiveEvidenceBundle extends Record<string, unknown> {
  bundle_id: string
  contract_id: string
  knowledge_point: string
  difficulty: string
  sources: Record<'knowledge' | 'business_data' | 'pedagogy', Record<string, unknown>>
}

export interface InteractiveResourceBranch extends Record<string, unknown> {
  branch_id: 'knowledge' | 'practice' | 'assessment'
  status: 'ready' | 'unavailable'
  required: boolean
  draft_id?: string
  payload_type?: string
  difficulty?: string
}

export interface InteractiveResourceBundle extends Record<string, unknown> {
  bundle_id: string
  contract_id: string
  evidence_bundle_id: string
  branches: InteractiveResourceBranch[]
}

export interface InteractivePretestQuestion {
  question_id: string
  knowledge_point: string
  stem: string
  options: Record<'A' | 'B' | 'C' | 'D', string>
}

export type InteractiveOutcome =
  | 'completed'
  | 'safe_rejected'
  | 'external_unavailable'
  | 'system_error'

export type InteractiveFailureOutcome = Exclude<InteractiveOutcome, 'completed'>

export class InteractiveApiError extends Error {
  readonly outcome?: InteractiveFailureOutcome

  constructor(message: string, outcome?: InteractiveFailureOutcome) {
    super(message)
    this.name = 'InteractiveApiError'
    this.outcome = outcome
  }
}

export interface InteractiveFollowUpTurn {
  round: number
  question: string
  answer: string
}

export type InteractiveInteraction =
  | {
      kind: 'free_text_follow_up'
      prompt: string
      round: number
      max_rounds: number
      turns: InteractiveFollowUpTurn[]
    }
  | {
      kind: 'data_collision'
      misconception: string
      wrong_label: string
      wrong_value: string
      correct_label: string
      correct_value: string
    }
  | {
      kind: 'next_learning_step'
      message: string
      knowledge_point?: string
    }
  | {
      kind: 'learning_notice'
      message: string
    }
  | {
      kind: 'review_notice'
      message: string
    }

export interface InteractiveState {
  session_id: string
  trace_id: string
  trace_path: string
  state: string
  awaiting: string
  mode: string
  profile: InteractiveProfile
  learning_contract?: InteractiveLearningContract | null
  evidence_bundle?: InteractiveEvidenceBundle | null
  resource_bundle?: InteractiveResourceBundle | null
  messages: Record<string, unknown>[]
  artifact: Record<string, unknown> | null
  interaction: InteractiveInteraction | null
  outcome?: InteractiveOutcome | null
}

export type AgentActivityId =
  | 'diagnosis'
  | 'knowledge'
  | 'task'
  | 'verification'
  | 'evidence_review'
  | 'pedagogy_review'
  | 'data_safety_review'
  | 'readability_review'
  | 'assessment'
  | 'review'

export type AgentActivityStatus =
  | 'idle'
  | 'queued'
  | 'working'
  | 'waiting'
  | 'collaborating'
  | 'reviewing'
  | 'debating'
  | 'approved'
  | 'blocked'
  | 'done'

export interface AgentActivityEvent {
  sequence: number
  trace_id: string
  agent: AgentActivityId
  status: AgentActivityStatus
  activity: string
  label: string
  stage: string
  peers: AgentActivityId[]
  timestamp: string
  details?: Record<string, unknown>
}

export interface InteractiveApi {
  createSession(profileId: string): Promise<InteractiveState>
  getState(sessionId: string): Promise<InteractiveState>
  getPretest(sessionId: string): Promise<InteractivePretestQuestion[]>
  submitPretest(
    sessionId: string,
    answers: Record<string, string>,
  ): Promise<InteractiveState>
  advance(sessionId: string): Promise<InteractiveState>
  continueLearning(sessionId: string): Promise<InteractiveState>
  submitSql(sessionId: string, sql: string): Promise<InteractiveState>
  submitFollowUp(
    sessionId: string,
    text: string,
    clientTurnId: string,
  ): Promise<InteractiveState>
  subscribeAgentEvents?(
    sessionId: string,
    onEvent: (event: AgentActivityEvent) => void,
  ): () => void
}

function trimTrailingSlash(value: string): string {
  return value.replace(/\/+$/, '')
}

function publicApiError(value: unknown): InteractiveApiError {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return new InteractiveApiError('实操通道暂时不可用，请稍后再试。')
  }
  const outcome = (value as { outcome?: unknown }).outcome
  if (outcome === 'safe_rejected') {
    return new InteractiveApiError(
      '本题的查询未通过数据安全检查，请调整后重试。',
      outcome,
    )
  }
  if (outcome === 'external_unavailable') {
    return new InteractiveApiError('服务暂时不可用，请稍后再试。', outcome)
  }
  if (outcome === 'system_error') {
    return new InteractiveApiError('当前步骤暂时无法继续，请稍后再试。', outcome)
  }
  return new InteractiveApiError('实操通道暂时不可用，请稍后再试。')
}

export function createInteractiveApi(
  baseUrl = import.meta.env.VITE_INTERACTIVE_API_BASE ?? '',
): InteractiveApi {
  const base = trimTrailingSlash(baseUrl)

  async function request<T>(
    path: string,
    method: 'GET' | 'POST',
    body?: Record<string, unknown>,
  ): Promise<T> {
    let response: Response
    try {
      response = await fetch(`${base}${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        ...(body ? { body: JSON.stringify(body) } : {}),
      })
    } catch {
      throw new InteractiveApiError(
        '服务暂时不可用，请稍后再试。',
        'external_unavailable',
      )
    }
    let value: unknown
    try {
      value = await response.json()
    } catch {
      throw new InteractiveApiError(
        '实操通道暂时不可用，请稍后再试。',
        'system_error',
      )
    }
    if (!response.ok) {
      throw publicApiError(value)
    }
    return value as T
  }

  return {
    createSession: (profileId) => request(
      '/api/sessions',
      'POST',
      { profile_id: profileId },
    ),
    getState: (sessionId) => request(
      `/api/sessions/${encodeURIComponent(sessionId)}`,
      'GET',
    ),
    getPretest: async (sessionId) => {
      const response = await request<{ questions: InteractivePretestQuestion[] }>(
        `/api/sessions/${encodeURIComponent(sessionId)}/pretest`,
        'GET',
      )
      return response.questions
    },
    submitPretest: (sessionId, answers) => request(
      `/api/sessions/${encodeURIComponent(sessionId)}/pretest`,
      'POST',
      { answers },
    ),
    advance: (sessionId) => request(
      `/api/sessions/${encodeURIComponent(sessionId)}/advance`,
      'POST',
      {},
    ),
    continueLearning: (sessionId) => request(
      `/api/sessions/${encodeURIComponent(sessionId)}/continue`,
      'POST',
      {},
    ),
    submitSql: (sessionId, sql) => request(
      `/api/sessions/${encodeURIComponent(sessionId)}/sql`,
      'POST',
      { sql },
    ),
    submitFollowUp: (sessionId, text, clientTurnId) => request(
      `/api/sessions/${encodeURIComponent(sessionId)}/follow-up`,
      'POST',
      {
        text,
        client_turn_id: clientTurnId,
      },
    ),
    subscribeAgentEvents: (sessionId, onEvent) => {
      const source = new EventSource(
        `${base}/api/sessions/${encodeURIComponent(sessionId)}/events`,
      )
      source.addEventListener('agent-activity', (message) => {
        try {
          const value: unknown = JSON.parse((message as MessageEvent<string>).data)
          if (
            typeof value === 'object'
            && value !== null
            && !Array.isArray(value)
            && typeof (value as AgentActivityEvent).sequence === 'number'
          ) onEvent(value as AgentActivityEvent)
        } catch {
          // Observability is non-authoritative; REST commands remain available.
        }
      })
      return () => source.close()
    },
  }
}
