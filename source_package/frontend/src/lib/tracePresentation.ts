import profileCatalog from 'virtual:profile-catalog'

import type {
  AgentId,
  RoleId,
  StateId,
  TraceMessage,
  TraceView,
  TruthBadge,
} from '../types/trace'


/**
 * 展示层统一岗位文案：以现行画像目录（agents/profiles/*.json，与实操通道
 * 的岗位选择页同源）为准；旧 demo trace 内嵌的旧版背景文案只作兜底。
 * trace 是评测证据不可改写，因此三处（岗位选择页/单画像/三画像）的口径
 * 统一发生在展示层。
 */
export function currentProfileCopy(
  profileId: string | undefined,
  traceProfile: { title?: string; background?: string } | undefined,
): { title: string; background: string } {
  const current = profileId
    ? profileCatalog.find((profile) => profile.id === profileId)
    : undefined
  return {
    title: current?.title ?? traceProfile?.title ?? '岗位画像',
    background: current?.background ?? traceProfile?.background ?? '岗位背景随会话载入。',
  }
}


export const STATE_ORDER: StateId[] = [
  'S0_INIT',
  'S1_DIAGNOSIS',
  'S2_KNOWLEDGE',
  'S3_TASK',
  'S4_VERIFY',
  'S5_REVIEW',
  'S6_DEBATE',
  'S7_STUDENT',
  'S8_PROBE',
  'S9_PATH_UPDATE',
  'S10_DONE',
]

const AGENT_LABELS: Record<AgentId, string> = {
  diagnosis: '学情诊断',
  knowledge: '领域知识',
  task: '实操任务',
  verification: '数据验证',
  review: '专业审核',
  system: '流程调度',
}

const AGENT_PURPOSES: Partial<Record<AgentId, string>> = {
  diagnosis: '识别知识盲区',
  knowledge: '匹配并组织讲义',
  task: '生成岗位练习',
  verification: '用查询结果核对结论',
  review: '按规则驳回或放行',
}

const ROLE_LABELS: Record<RoleId, string> = {
  produce: '生成内容',
  verdict: '初次审核',
  rebuttal: '补充说明',
  re_verdict: '再次审核',
  probe: '验证任务',
  system: '流程调度',
}

export const STATE_LABELS: Record<StateId, string> = {
  S0_INIT: '会话建立',
  S1_DIAGNOSIS: '岗前测评',
  S2_KNOWLEDGE: '岗位微课',
  S3_TASK: '实操任务',
  S4_VERIFY: '数据验证',
  S5_REVIEW: '审核把关',
  S6_DEBATE: '辩论复审',
  S7_STUDENT: '学员作答',
  S8_PROBE: '数据验证',
  S9_PATH_UPDATE: '培养路径更新',
  S10_DONE: '培养目标达成',
  S_FAIL: '安全终止',
}

const RULE_LABELS: Record<string, string> = {
  'R-01': '口径混淆',
  'R-02': '引用不足',
  'R-03': '难度错配',
  'R-04': '结论未申报',
  'R-05': '查询证据异常',
  'R-06': '表达可读性问题',
}

const SANDBOX_LABELS: Record<string, string> = {
  'S-01': '只能做数据查询',
  'S-02': '一次只能查一条',
  'S-03': '只能查授权的数据表',
  'S-04': '只能查授权的字段',
  'S-05': '含不允许的危险操作',
  'S-06': '含不允许的危险操作',
  'S-07': '查询语句为空或写法有误',
  'S-08': '超出安全查询范围',
}

const MISCONCEPTION_LABELS: Record<string, string> = {
  'M-01': '计划量与实际量的区分',
  'M-02': '工序传导时滞的判断',
  'M-03': '单月波动与长期趋势的区分',
  'M-04': '汇总口径的区分',
  'M-05': '异常衰减的判断',
}

const DIMENSION_LABELS: Record<string, string> = {
  三道工序与传导关系: '工序传导',
  计划量与实际量口径: '计划/实际口径',
  完成率计算: '完成率',
  偏差率与风险等级: '偏差风险',
  月度聚合方法: '月度聚合',
  异常识别标准: '异常识别',
  传导时滞分析: '传导时滞',
  工序间传导时滞: '工序传导',
  异常衰减规律: '异常衰减',
  异常衰减识别: '异常衰减',
  责任单元定位: '责任定位',
  跨工序归因方法: '跨工序归因',
}

const FIELD_LABELS: Record<string, string> = {
  fact_production_progress: '生产进度表',
  dim_date: '日期维表',
  dim_process: '工序维表',
  dim_ship: '船舶维表',
  dim_workshop: '车间维表',
  plan_qty: '计划量',
  actual_qty: '实际完成量',
  complete_rate: '完成率',
  completion_rate: '完成率',
  deviation_rate: '偏差率',
  ship_no: '船号',
  process_code: '工序',
  period_date: '日期',
  workshop: '车间',
  workshop_code: '责任单元',
  month_label: '月份',
  high_risk_rows: '高风险记录数',
  responsibility_unit: '责任单元',
  risk_level: '风险等级',
  delay_days: '延迟天数',
  shortfall_intensity: '缺口强度',
  anomaly_flag: '异常标记',
  batch_code: '批次编码',
  batch_no: '批次号',
  process_name: '工序名称',
  process_order: '工序顺序',
  progress_id: '进度记录编号',
  quality_pass_qty: '质量合格量',
  rework_qty: '返工量',
  ship_type: '船型',
  source_type: '数据来源类型',
  DEPT: '部门',
  PROCESS: '工序',
  YEARNUM: '年总量',
  MONTHNUM: '月计划数',
  ACTUALNUM: '实际数',
  FINISHRATE: '完成率',
  ONTIMERATE: '准时率',
  SERWARNNUM: '严重预警数',
  WARNNUM: '预警数',
  FINISHNUM: '完成数',
  ONTIMENUM: '准时数',
  PLANNUM: '计划数',
  ndjhs: '年度计划数',
  JHYLJ: '年计划累计数',
  NDSJS: '年度实际数',
  TRENDMONTH: '月份',
  TRENDYEAR: '年份',
  QG_NUM: '切割实际值',
  XZL_NUM: '小组立实际值',
}

function ownValue<T>(values: Readonly<Record<string, T>>, key: string): T | undefined {
  return Object.prototype.hasOwnProperty.call(values, key) ? values[key] : undefined
}


export function firstLectureGoal(value: unknown): string | undefined {
  if (typeof value !== 'string') return undefined
  const lines = value.split(/\r?\n/)
  const heading = lines.findIndex((line) => /^#{1,6}\s*本节目标\s*$/u.test(line.trim()))
  if (heading < 0) return undefined
  for (const line of lines.slice(heading + 1)) {
    if (/^#{1,6}\s+/u.test(line.trim())) return undefined
    const bullet = line.match(/^\s*[-*+]\s+(.+?)\s*$/u)?.[1]
    if (bullet) return bullet.replace(/[*_`]/gu, '').trim() || undefined
  }
  return undefined
}


export function contextualizedTaskStem(view: TraceView): string | undefined {
  const task = view.visibleMessages.find((message) => (
    !message.rejectedByBus
    && message.agent === 'task'
    && message.role === 'produce'
    && message.content.event === 'product_ready'
    && ['quiz_set', 'practice_guide'].includes(message.payloadType)
  ))
  if (!task) return undefined
  const stem = typeof task.content.contextualized_stem === 'string'
    ? task.content.contextualized_stem.trim()
    : ''
  const standard = typeof task.content.standard_stem === 'string'
    ? task.content.standard_stem.trim()
    : ''
  if (stem && stem !== standard) return stem
  // 旧版 trace（个性化题干功能上线前录制）没有 contextualized_stem 字段：
  // 退回会话实际下发的题干 question，保证回放/三画像能看到真实任务，
  // 而不是恒为"暂无岗位实操任务"。
  const question = typeof task.content.question === 'string'
    ? task.content.question.trim()
    : ''
  return question || undefined
}


export function abbreviateDimension(dimension: string): string {
  return DIMENSION_LABELS[dimension]
    ?? dimension.replace(/方法|分析|标准|规律|关系|计算/g, '').slice(0, 6)
}


const PROCESS_VALUE_LABELS: Record<string, string> = {
  YCL: '预处理',
  ZZTP: '制作托盘',
  AZTP: '安装托盘',
}

export function processValueLabel(code: string): string {
  return PROCESS_VALUE_LABELS[code] ?? code
}

const SHIP_IN_SQL_RE = /ship_no\s*=\s*'([^']+)'/giu
const PROCESS_IN_SQL_RE = /process_code\s*=\s*'([^']+)'/giu
const PROCESS_IN_LIST_RE = /process_code\s+in\s*\(([^)]+)\)/giu
const MONTH_UPPER_RE = /period_date\s*<\s*'(\d{4})-(\d{2})-/iu
const MONTH_LOWER_RE = /period_date\s*>=\s*'(\d{4})-(\d{2})-/iu

/** 从当前可见 trace 提取训练数据范围（船号/工序/数据截止）。 */
export function deriveTrainingScope(
  view: { sqlResult?: { content: Record<string, unknown> } } | undefined,
): { ship?: string; processes: string[]; cutoff?: string } {
  const content = view?.sqlResult?.content
  let ship: string | undefined
  const processes: string[] = []
  const months: string[] = []

  const addProcess = (code: string): void => {
    if (code && !processes.includes(code)) processes.push(code)
  }

  // 来源一：结果行中的维度列（宽表查询直接可见）。
  const rows = content?.rows
  if (Array.isArray(rows)) {
    for (const row of rows) {
      if (typeof row !== 'object' || row === null) continue
      const record = row as Record<string, unknown>
      if (!ship && typeof record.ship_no === 'string') ship = record.ship_no
      if (typeof record.process_code === 'string') addProcess(record.process_code)
      if (typeof record.month_label === 'string') months.push(record.month_label)
    }
  }

  // 来源二：查询语句的 WHERE 条件——单值结果（如仅一列完成率）时，
  // 船号/工序/时间范围只在 SQL 文本里。
  const sqlText = [content?.generated_sql, content?.executed_sql]
    .filter((part): part is string => typeof part === 'string')
    .join('\n')
  if (sqlText) {
    if (!ship) {
      const shipMatch = SHIP_IN_SQL_RE.exec(sqlText)
      if (shipMatch) ship = shipMatch[1]
    }
    for (const match of sqlText.matchAll(PROCESS_IN_SQL_RE)) addProcess(match[1])
    for (const listMatch of sqlText.matchAll(PROCESS_IN_LIST_RE)) {
      for (const inner of listMatch[1].matchAll(/'([^']+)'/gu)) addProcess(inner[1])
    }
    const upper = MONTH_UPPER_RE.exec(sqlText)
    if (upper) {
      // 上界为开区间次月首日，数据实际覆盖到上界前一月。
      const year = Number(upper[1])
      const month = Number(upper[2])
      const prev = month === 1 ? 12 : month - 1
      const prevYear = month === 1 ? year - 1 : year
      months.push(`${prevYear}-${String(prev).padStart(2, '0')}`)
    } else {
      const lower = MONTH_LOWER_RE.exec(sqlText)
      if (lower) months.push(`${lower[1]}-${lower[2]}`)
    }
  }

  const maxMonth = months.filter(Boolean).sort().at(-1)
  // 数据截止：观测到的最大月份的月末。
  const cutoff = maxMonth ? `${maxMonth.slice(0, 7)} 月末` : undefined
  return { ship, processes, cutoff }
}

export function dataFieldLabel(field: string): string {
  const approvedLabel = ownValue(FIELD_LABELS, field)
  if (approvedLabel === undefined) return '数据字段'
  return publicDisplayText(field, approvedLabel, '数据字段')
}


const MECHANISM_COPY: Array<[RegExp, string]> = [
  [/\bdifficulty_gap\s*=\s*1\b/gi, '难度相差一级'],
  [/\bstep_up\b/gi, '难度提升'],
  [/\bstep_down\b/gi, '补充基础讲解'],
  [/\bbasic\b/gi, '基础档'],
  [/\bapplied\b/gi, '应用档'],
  [/\badvanced\b/gi, '进阶档'],
  [/\s+vs\s+/gi, '与'],
  [/五智能体协同实训/g, '岗位训练'],
  [/协同实训/g, '岗位训练'],
  [/数据撞脸/g, '数据验证'],
  [/系统编排/g, '流程调度'],
  [/编排器/g, '流程调度'],
  [/编排/g, '调度'],
  [/反证查询/g, '验证查询'],
  [/反证追问/g, '数据验证'],
  [/反证任务/g, '验证任务'],
  [/反证数据/g, '验证数据'],
  [/反证/g, '验证'],
]

const SOURCE_COPY: Array<[RegExp, string]> = [
  [/\braw\s*未明确[，,]\s*以现场口径为准/giu, '以现场实际口径为准'],
  [/\braw\s*未明确/giu, '现有资料未明确'],
  [/\braw\s*数据/giu, '现有资料'],
  [/\braw\b/giu, '现有资料'],
  ...Object.entries(RULE_LABELS).map(([ruleId, label]) => ([
    new RegExp(`\\b${ruleId}\\b(?:\\s*[：:·]?\\s*${label})?`, 'giu'),
    label,
  ] as [RegExp, string])),
  ...Object.entries(SANDBOX_LABELS).map(([ruleId, label]) => ([
    new RegExp(`\\b${ruleId}\\b(?:\\s*[：:·]?\\s*${label})?`, 'giu'),
    label,
  ] as [RegExp, string])),
  [/\bchunks?\b/giu, '资料片段'],
  [/\bSEC-[A-Za-z0-9_-]+\b/giu, '资料来源'],
  [/\bKB-[A-Za-z0-9_-]+\b/giu, '资料来源'],
]

const PUBLIC_TEXT_FALLBACK = '当前内容暂时无法展示，请稍后再试。'
const ZERO_WIDTH_PUBLIC_TEXT = /[\u200b-\u200f\u202a-\u202e\u2060\ufeff]/u
const SHARED_ENGINEERING_PUBLIC_TEXT: RegExp[] = [
  /(?:no_matching_transition|msg_id|rule_hits|rebuttal|verdict)/iu,
  /(?:outcome|completed|safe_rejected|external_unavailable|system_error)/iu,
  /(?:injected_for_demo|injection_label|_manual_false_reject)/iu,
  /routing_[A-Za-z0-9_]*/iu,
  /(?:Agent|LLM|orchestrator)/iu,
  /(?:msgId|traceId|sessionId|ruleHits|templateId|evidenceRef|reviewedMsgId)/iu,
  /\bHTTP\s*[1-5][0-9]{2}\b/iu,
  /\/api(?:\/|\b)/iu,
  /[MR]-[0-9]+/iu,
  /S-[0-9]+/iu,
  /(?:人工误驳|人工误判|人工注入|故障注入)/u,
  /(?:智能体|状态机|协议字段|转移名|意图路由|路由字段|工程实现|状态码|接口路径|请求体)/u,
]
const CORE_ENGINEERING_PUBLIC_TEXT: RegExp[] = [
  /T[0-9]+/iu,
  /S[0-9]+/iu,
  /Q[0-9]+/iu,
  ...SHARED_ENGINEERING_PUBLIC_TEXT,
]
const QUERY_ENGINEERING_PUBLIC_TEXT: RegExp[] = [
  /T[0-9]+/u,
  /S[0-9]+/u,
  /Q[0-9]+/u,
  ...SHARED_ENGINEERING_PUBLIC_TEXT,
]
const FORBIDDEN_PUBLIC_TEXT: RegExp[] = [
  ...CORE_ENGINEERING_PUBLIC_TEXT,
  /\bT-(?:FS\d+|\d+(?:-[A-Z]+)*)\b/iu,
  /\b(?:diagnostic|difficulty|family|misconception|route|state)\b/iu,
  /\b(?:trace_id|session_id|msg_id|template_id|transition_id|from_state|to_state|payload_type|rule_hits|evidence_ref|reviewed_msg_id|difficulty_action|target_misconception|learning_contract|domain_package_sha256)\b/iu,
]


function translatedFields(text: string): string {
  return Object.entries(FIELD_LABELS).reduce(
    (value, [field, label]) => value.replace(new RegExp(`\\b${field}\\b`, 'g'), label),
    text,
  )
}

function sourceSafeText(text: string): string {
  return SOURCE_COPY.reduce(
    (value, [pattern, replacement]) => value.replace(pattern, replacement),
    text,
  )
}

function normalizedPublicText(text: string): string {
  return text.normalize('NFKC')
}


function containsForbiddenPublicText(text: string): boolean {
  if (ZERO_WIDTH_PUBLIC_TEXT.test(text)) return true
  const normalized = normalizedPublicText(text)
  return FORBIDDEN_PUBLIC_TEXT.some((pattern) => pattern.test(normalized))
}


export function isLearnerSafeText(text: string): boolean {
  return typeof text === 'string'
    && text.trim() !== ''
    && !containsForbiddenPublicText(text)
}


function publicFallback(fallback: string): string {
  return isLearnerSafeText(fallback) ? fallback : PUBLIC_TEXT_FALLBACK
}

export function publicQueryText(
  rawText: string,
  fallback = '查询内容已隐藏',
): string {
  if (
    rawText.trim() === ''
    || ZERO_WIDTH_PUBLIC_TEXT.test(rawText)
    || QUERY_ENGINEERING_PUBLIC_TEXT.some(
      (pattern) => pattern.test(normalizedPublicText(rawText)),
    )
  ) {
    return publicFallback(fallback)
  }
  return rawText
}

function publicText(text: string, fallback = PUBLIC_TEXT_FALLBACK): string {
  return text.trim() === '' || containsForbiddenPublicText(text)
    ? publicFallback(fallback)
    : text
}

// The task generator prepends "前置提示：本题关联前置知识 「KP」——goal；…。"
// to graded questions that cross into a prerequisite knowledge point — the
// R-03 reviewer needs this note inside the product, but learners should
// never see it; every display path strips it for a uniform clean stem.
const SCAFFOLD_HINT_PREFIX = /^前置提示：本题关联前置知识 [^。]*。/u

function stripScaffoldHint(text: string): string {
  if (!SCAFFOLD_HINT_PREFIX.test(text)) return text
  return text.replace(SCAFFOLD_HINT_PREFIX, '').trim() || text
}


export function publicDisplayText(
  rawText: string,
  translatedText = rawText,
  fallback = PUBLIC_TEXT_FALLBACK,
): string {
  const approvedFieldTranslation = ownValue(FIELD_LABELS, rawText) === translatedText
  if (
    translatedText.trim() === ''
    || (!approvedFieldTranslation && containsForbiddenPublicText(rawText))
    || containsForbiddenPublicText(translatedText)
  ) {
    return publicFallback(fallback)
  }
  return stripScaffoldHint(translatedText)
}

function translatedPublicText(text: string): string {
  const mechanisms = MECHANISM_COPY.reduce(
    (value, [pattern, replacement]) => value.replace(pattern, replacement),
    sourceSafeText(translatedFields(stripScaffoldHint(text))),
  )
  return Object.entries(MISCONCEPTION_LABELS).reduce(
    (value, [code, label]) => value.replaceAll(code, label),
    mechanisms,
  )
}


export function collaborationText(text: string): string {
  return publicText(translatedPublicText(text))
}


export function learnerText(text: string): string {
  return publicText(translatedPublicText(text))
}


export function learnerTextOr(text: string, fallback: string): string {
  return publicText(translatedPublicText(text), fallback)
}

export const VERIFICATION_FAILURE_EVENTS = [
  'refuse_out_of_scope',
  'sandbox_rejected',
  'template_authority_rejected',
  'query_empty',
  'query_timeout',
  'query_failed',
] as const

export type VerificationFailureEvent = typeof VERIFICATION_FAILURE_EVENTS[number]

export interface VerificationFailureCopy {
  event: VerificationFailureEvent
  title: string
  learnerMessage: string
  ruleId?: string
}

export function verificationFailureCopy(
  event: unknown,
  content: Record<string, unknown> = {},
): VerificationFailureCopy | undefined {
  if (!VERIFICATION_FAILURE_EVENTS.includes(event as VerificationFailureEvent)) {
    return undefined
  }
  if (event === 'refuse_out_of_scope') {
    return {
      event,
      title: '问题不在本次训练范围',
      learnerMessage: '这个问题不在本次训练的数据范围内，请换一个与岗位任务相关的问题。',
    }
  }
  if (event === 'sandbox_rejected') {
    const ruleId = typeof content.rule_id === 'string' ? content.rule_id : undefined
    const fallback = '本题的查询未通过数据安全检查，请调整后重试。'
    const learnerMessage = learnerTextOr(
      typeof content.student_message === 'string' ? content.student_message : fallback,
      fallback,
    )
      .replace(/只读查询/g, '查询')
      .replace(/安全校验/g, '安全检查')
    return {
      event,
      ...(ruleId ? { ruleId } : {}),
      title: `${sandboxLabel(ruleId) ?? '安全规则'} · 查询被拦下`,
      learnerMessage,
    }
  }
  if (event === 'template_authority_rejected') {
    const fallback = '查询结构与本题目标尚未完全对应，请核对对象、月份、筛选条件和分组维度后重试。'
    return {
      event,
      title: '查询内容需要调整',
      learnerMessage: learnerTextOr(
        typeof content.student_message === 'string' ? content.student_message : fallback,
        fallback,
      ),
    }
  }
  if (event === 'query_empty') {
    const fallback = '本次查询没有返回数据，请调整查询条件后重试。'
    return {
      event,
      title: '本次查询没有返回数据',
      learnerMessage: learnerTextOr(
        typeof content.student_message === 'string' ? content.student_message : fallback,
        fallback,
      ),
    }
  }
  const fallback = '服务暂时不可用，请稍后再试。'
  return {
    event: event as VerificationFailureEvent,
    title: '查询服务暂时不可用',
    learnerMessage: learnerTextOr(
      typeof content.student_message === 'string' ? content.student_message : fallback,
      fallback,
    ).replace(/只读查询/g, '查询'),
  }
}


export function agentLabel(agent: string): string {
  return AGENT_LABELS[agent as AgentId] ?? '未识别角色'
}


const PAYLOAD_TYPE_LABELS: Record<string, string> = {
  profile_assessment: '学情诊断',
  lecture_note: '岗位微课',
  practice_guide: '实操指引',
  quiz_set: '练习题',
  sql_result: '数据查询',
  review_verdict: '专业审核',
  rebuttal_case: '补充说明',
  probe_questions: '验证任务',
  learning_path_update: '培养路径',
  control: '流程调度',
}


export function payloadTypeLabel(payloadType: string): string {
  return PAYLOAD_TYPE_LABELS[payloadType] ?? '会话内容'
}


const AWAITING_LABELS: Record<string, string> = {
  pretest: '岗前测评',
  diagnostic_probe: '补充诊断',
  sql: '数据实操',
  follow_up: '理解核对',
  advance: '等待推进',
  done: '已完成',
}


export function awaitingLabel(awaiting: string): string {
  return AWAITING_LABELS[awaiting] ?? awaiting
}


export function agentPurpose(agent: string): string {
  return AGENT_PURPOSES[agent as AgentId] ?? ''
}


export function roleLabel(role: string): string {
  return ROLE_LABELS[role as RoleId] ?? '未识别动作'
}


export function stateLabel(state: string): string {
  return STATE_LABELS[state as StateId] ?? '未识别状态'
}


export function ruleLabel(ruleId: string): string {
  return RULE_LABELS[ruleId] ?? '审核规则（未识别）'
}


export function sandboxLabel(ruleId: string | undefined): string | undefined {
  return ruleId ? ownValue(SANDBOX_LABELS, ruleId) : undefined
}


export function misconceptionLabel(misconception: string): string {
  return MISCONCEPTION_LABELS[misconception] ?? '错误认知（未识别）'
}


export function decisionLabel(decision: string | undefined): string {
  if (decision === 'approve') return '审核通过'
  if (decision === 'approve_with_fix') return '校正后通过'
  if (decision === 'reject') return '审核驳回'
  return '审核结果已记录'
}


function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() !== '' ? value : undefined
}


export function messageSummary(message: TraceMessage): string {
  if (message.rejectedByBus) return '记录未通过完整性检查'
  const content = message.content
  const transitionTarget = stringValue(content.to_state)
  const transitionSource = stringValue(content.from_state)
  if (transitionTarget && transitionSource) {
    return `${stateLabel(transitionSource)} → ${stateLabel(transitionTarget)}`
  }
  if (content.action === 'session_start') return '岗位培养会话已建立'
  if (content.action === 'profile_loaded') {
    const profile = content.profile
    if (typeof profile === 'object' && profile !== null && !Array.isArray(profile)) {
      const title = stringValue((profile as Record<string, unknown>).title)
      if (title) return `${collaborationText(title)}学情画像已载入`
    }
    return '学情画像已载入'
  }
  if (message.payloadType === 'profile_assessment') {
    const blindSpots = Array.isArray(content.blind_spots) ? content.blind_spots.length : 0
    return `岗前测评完成 · 识别${blindSpots}项知识盲区`
  }
  if (message.payloadType === 'lecture_note') {
    return '依据学员盲区选取知识点，从知识库调取内容并逐句核对引用'
  }
  if (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide') {
    return collaborationText(stringValue(content.question) ?? '实操任务已生成')
  }
  if (message.payloadType === 'sql_result') {
    const failure = verificationFailureCopy(content.event, content)
    if (failure) return failure.learnerMessage
    const question = stringValue(content.question) ?? '数据查询'
    const count = typeof content.row_count === 'number' ? content.row_count : 0
    return `${collaborationText(question)} · 返回${count}行结果`
  }
  if (message.payloadType === 'review_verdict') {
    const labels = message.verdict?.ruleHits.map((hit) => ruleLabel(hit.ruleId)) ?? []
    return labels.length
      ? `${decisionLabel(message.verdict?.decision)} · ${labels.join('、')}`
      : decisionLabel(message.verdict?.decision)
  }
  if (message.payloadType === 'rebuttal_case') return '被驳回方提交补充说明'
  if (message.payloadType === 'learning_path_update') {
    return '培养路径已更新'
  }
  if (content.event === 'student_answer') return '学员提交结论判断'
  if (content.event === 'probe_outcome') return '学员依据验证数据修正结论'
  return `${roleLabel(message.role)}已记录`
}


export function truthBadges(message: TraceMessage): TruthBadge[] {
  const badges: TruthBadge[] = []
  if (message.content.cached === true) {
    badges.push({ label: '历史记录', tone: 'cached' })
  }
  if (
    message.content.generated_by === 'template_fallback'
    || message.content.fallback_action === 'degrade_to_template'
  ) {
    badges.push({ label: '预备内容', tone: 'fallback' })
  }
  return badges
}
