<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import profiles from 'virtual:profile-catalog'
import diagnosticExperienceTags from 'virtual:diagnostic-experience-tags'

import {
  createInteractiveApi,
  type AgentActivityEvent,
  InteractiveApiError,
  type InteractiveApi,
  type InteractiveDiagnosticProbe,
  type InteractiveOutcome,
  type InteractivePretestQuestion,
  type InteractiveState,
} from '../lib/interactiveApi'
import type { TraceMessage } from '../types/trace'
import SqlResultTable from './SqlResultTable.vue'
import WaveText from './WaveText.vue'
import {
  isLearnerSafeText,
  learnerText,
  learnerTextOr,
  misconceptionLabel,
  verificationFailureCopy,
  type VerificationFailureCopy,
  type VerificationFailureEvent,
} from '../lib/tracePresentation'


const props = withDefaults(defineProps<{
  api?: InteractiveApi
  pollIntervalMs?: number
  sqlResult?: TraceMessage
  operationOnly?: boolean
  /** 需求⑨：讲义查阅面板是否展开（由 App 层控制，双栏切换）。 */
  lecturePeekOpen?: boolean
}>(), {
  pollIntervalMs: 1500,
  operationOnly: false,
  lecturePeekOpen: false,
})

const emit = defineEmits<{
  state: [value: InteractiveState]
  agentEvent: [value: AgentActivityEvent]
  reset: []
  lecturePeek: [value: boolean]
  autoChain: [value: boolean]
  practiceChain: [value: boolean]
}>()

const api = props.api ?? createInteractiveApi()
const sessionStorageKey = 'ref-interactive-session'
const session = ref<InteractiveState>()
const questions = ref<InteractivePretestQuestion[]>([])
const answers = ref<Record<string, string>>({})
const diagnosticProbes = ref<InteractiveDiagnosticProbe[]>([])
const selectedExperienceTag = ref('')
const diagnosticAnswers = ref<Record<string, string>>({})
const pretestPage = ref(0)
const sqlText = ref('')
const followUpText = ref('')
const followUpClientTurnId = ref<string>()
const followUpSubmittedText = ref('')
// 需求③：记录默认展开；用户手动收起后保持，不自动重开
const followUpHistoryExpanded = ref(false)
const sqlHintLevel = ref(0)
// 岗位入口拆成两屏：欢迎页（讲清流程）→ 选择页（三张岗位卡）。
const profileStep = ref<'welcome' | 'select'>('welcome')
// 闭环一：两步式选岗——先选岗位，再按该岗位学习领域（knowledge_scope）
// 过滤"训练关注点"下拉，与蓝图 3.1 的顺序（1 选画像 → 2 选关注点）一致。
const chosenProfileId = ref<string | null>(null)
const chosenProfile = computed(() => profiles.find((profile) => profile.id === chosenProfileId.value))
const scopedExperienceTags = computed(() => {
  const scope = chosenProfile.value?.knowledgeScope ?? []
  return diagnosticExperienceTags.filter((tag) => scope.includes(tag.knowledge_point))
})

function chooseProfile(profileId: string): void {
  if (busy.value) return
  chosenProfileId.value = profileId
  // 先前选过的关注点若不在该岗位领域内，清空回退"自动诊断"。
  if (selectedExperienceTag.value) {
    const inScope = scopedExperienceTags.value
      .some((tag) => tag.tag_id === selectedExperienceTag.value)
    if (!inScope) selectedExperienceTag.value = ''
  }
}
const sqlResultVisible = ref(true)
// 需求②：提交查询后停留在 SQL 页（按钮转"正在进行查询"），链完直跳提问界面
const sqlChainRunning = ref(false)
// 需求②③：前测后直达链全程保持 true——跨 advance 间隙不断档，pending 不闪烁
const autoChainRunning = ref(false)
// ②：练习链（进入练习按钮触发）——不显示 pending，直接跳转
const practiceChainRunning = ref(false)
const hasEnteredPractice = ref(false)

// autoChainRunning 靠 :has(.generation-pending) CSS 联动布局（不再 emit）
// 两链分别 emit：pretest 链锁 transition（中央 pending）；practice 链锁 lesson（保持讲义页）
watch(autoChainRunning, (value) => emit('autoChain', value))
watch(practiceChainRunning, (value) => emit('practiceChain', value))

function isSqlResultArtifact(artifact: unknown): boolean {
  if (typeof artifact !== 'object' || artifact === null) return false
  const payload = (artifact as { payload?: { type?: unknown } }).payload
  return typeof payload === 'object' && payload !== null
    && (payload as { type?: unknown }).type === 'sql_result'
}

// During awaiting='advance' the previous query result stays valid only while
// the session artifact IS the approved sql_result message.  T17 remediation
// re-points the artifact to a control message and the claim gate to the
// lecture — both truthy but not query results — so "artifact exists" alone
// must not keep the stale table rendered.
const sqlResultStale = computed(
  () => session.value?.awaiting === 'advance'
    && !isSqlResultArtifact(session.value?.artifact),
)
const busy = ref(false)
const errorMessage = ref('')
let pollTimer: number | undefined
let pollingSessionId = ''
let closeEventStream: (() => void) | undefined
let eventSessionId = ''

const pretestComplete = computed(() => questions.value.length > 0
  && questions.value.every((question) => answers.value[question.question_id]))
const currentPretestQuestion = computed(() => questions.value[pretestPage.value])
const answeredPretestCount = computed(() => questions.value.filter(
  (question) => Boolean(answers.value[question.question_id]),
).length)
const currentPretestAnswered = computed(() => {
  const question = currentPretestQuestion.value
  return Boolean(question && answers.value[question.question_id])
})
const isLastPretestPage = computed(() => pretestPage.value >= questions.value.length - 1)
const diagnosticComplete = computed(() => diagnosticProbes.value.length > 0
  && diagnosticProbes.value.every(
    (probe) => Boolean(diagnosticAnswers.value[probe.probe_id]?.trim()),
  ))
// 优化5：探针界面已精简——道数进度/题号不再展示，序号与总数字段随之移除。
const stationTitle = computed(() => {
  if (session.value?.awaiting === 'pretest') return '岗前测评'
  if (session.value?.awaiting === 'diagnostic_probe') return '补充诊断'
  if (session.value?.awaiting === 'done') return '训练报告'
  if (session.value?.state === 'S2_KNOWLEDGE') return '微课准备'
  return '实操'
})

/** ⑧：当前是否存在真实讲义（延期标记的讲义没有可翻阅内容）。 */
const hasRealLecture = computed(() => {
  const messages = session.value?.messages ?? []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index] as
      | { agent?: string; payload?: { type?: string; content?: { lecture_deferred?: boolean } } }
      | undefined
    if (message?.agent === 'knowledge' && message.payload?.type === 'lecture_note') {
      return message.payload.content?.lecture_deferred !== true
    }
  }
  return false
})

/** ⑦：直达链空闲推进位（S2/S3/S9）不显示手动按钮——自动链负责，出错时以错误提示兜底。 */
const directPathIdle = computed(() => {
  const value = session.value
  if (!value) return false
  // 四次未过停驻：按钮一律隐藏（提示句常显，自动推进）
  if (stepDownNoticeActive.value) return true
  if (autoChainRunning.value || practiceChainRunning.value) return true
  // S3/S9 待推进：按钮永不出（不论前测结果——"领取实操任务/判断查询结论"彻底消灭）
  // 必须在 firstPointPretestCorrect 之前，否则画像三（false）先返回 false 按钮漏出
  // S7+advance 仅存在于画像三 data_present 延时代执行窗口（任务已下发、系统即将代执行）——同样不出手动按钮
  if (
    value.state === 'S3_TASK'
    || value.state === 'S9_PATH_UPDATE'
    || value.state === 'S7_STUDENT'
  ) return true
  if (!firstPointPretestCorrect.value) return false
  if (errorMessage.value) return false
  return value.state === 'S2_KNOWLEDGE'
})

/** 需求④⑤：四次未过换证重练态——无论是否在推进，都常显中央双行提示。
 * 双通道判定：next_learning_step 交互，或 S2 待推进且消息含"四次"（推进后 kind 会变）。 */
const stepDownNoticeActive = computed(() => {
  const value = session.value
  if (!value || value.awaiting !== 'advance') return false
  const interaction = value.interaction
  const message = (interaction as { message?: string } | undefined)?.message
  if (interaction?.kind === 'next_learning_step') {
    return String(message ?? '').includes('四次')
  }
  return Boolean(
    value.state === 'S2_KNOWLEDGE'
    && String(message ?? '').includes('四次'),
  )
})

/** 生成等待提示句：四次未过换讲义 / 进入链全程同句 / 画像三代执行窗口 / S9 追问生成 / 常规讲义。 */
const pendingPromptText = computed(() => {
  const value = session.value
  if (stepDownNoticeActive.value) {
    return '四次理解核对未达成掌握目标，' + '\n' + '系统将更换证据与讲解角度后再练习一次。' + '\n' + '即将进入学习，请稍候…'
  }
  // 进入链（前测直达）中途不换文案：S9 结论生成阶段原会切成"正在为你准备下一问"，
  // 同一条等待被感知为多出一个中间页（0818 实录）——整链保持同一句，直到提问界面。
  if (autoChainRunning.value) {
    return firstPointPretestCorrect.value
      ? '正在为你准备练习题，请稍候…'
      : '正在为你准备专属讲义，请稍候…'
  }
  if (value?.state === 'S7_STUDENT' && hasEnteredPractice.value) {
    return '已按你的岗位模式查询数据，正在准备提问，请稍候…'
  }
  if (value?.state === 'S9_PATH_UPDATE') {
    return '正在为你准备下一问，请稍候…'
  }
  return firstPointPretestCorrect.value
    ? '正在为你准备练习题，请稍候…'
    : '正在为你准备专属讲义，请稍候…'
})

/** 首个知识点是否前测做对（v4 计划首条目 correct 档 → 微课跳过直入练习）。
 * 双信号：诊断 artifact 的学习计划首条目；或最新讲义消息带延期标记
 * （S3 领取任务推进期间 artifact 已换，靠消息兜底保证提示句不分错流）。 */
const firstPointPretestCorrect = computed(() => {
  const value = session.value
  if (!value) return false
  const artifact = value.artifact as
    | { payload?: { content?: { knowledge_point_plan?: Array<{ tier?: string }> } } }
    | null
    | undefined
  const plan = artifact?.payload?.content?.knowledge_point_plan
  const first = Array.isArray(plan) ? plan[0] : undefined
  if (first?.tier === 'correct') return true
  const messages = value.messages ?? []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index] as
      | { agent?: string; payload?: { type?: string; content?: { lecture_deferred?: boolean } } }
      | undefined
    if (message?.agent === 'knowledge' && message.payload?.type === 'lecture_note') {
      return message.payload.content?.lecture_deferred === true
    }
  }
  return false
})

const advanceAction = computed(() => {
  const value = session.value
  if (!value || value.awaiting !== 'advance') return undefined
  if (value.state === 'S2_KNOWLEDGE') {
    // 闭环四名实一致：当前知识点前测已验证（v4 计划首条目 correct 档）→ 微课
    // 懒生成，按钮按真实动作表述“直入实操”，不再说“打开岗位微课”。
    if (firstPointPretestCorrect.value) return '直入实操（前测已验证）'
    return '打开岗位微课'
  }
  if (value.state === 'S3_TASK') {
    return '领取实操任务'
  }
  if (value.state === 'S9_PATH_UPDATE'
    && (value.interaction?.kind === 'data_collision'
      || value.interaction?.kind === 'next_learning_step')) {
    return '查看下一步训练'
  }
  if (value.state === 'S9_PATH_UPDATE' && value.interaction?.kind === 'learning_notice') {
    return '判断查询结论'
  }
  if (value.state === 'S9_PATH_UPDATE') {
    return '判断查询结论'
  }
  return '继续训练'
})

// ---- 0818 需求 3：报告增强展示 + HTML 下载 ----
const REPORT_TIER_LABELS = ['未学', '基础档', '应用档', '进阶档']

function reportTierLabel(tier: number | undefined): string {
  return REPORT_TIER_LABELS[Math.min(3, Math.max(0, Number(tier ?? 0)))]
}

const hasCommonMistakes = computed(() => {
  const mistakes = session.value?.training_report?.common_mistakes
  if (!mistakes) return false
  return Boolean(
    (mistakes.misconception_counts && Object.keys(mistakes.misconception_counts).length)
      || mistakes.wrong_answer_rounds
      || mistakes.sql_failure_count,
  )
})

const reportDurationText = computed(() => {
  const report = session.value?.training_report
  if (!report?.started_at || !report.finished_at) return ''
  const start = Date.parse(report.started_at)
  const finish = Date.parse(report.finished_at)
  if (!Number.isFinite(start) || !Number.isFinite(finish) || finish <= start) return ''
  const minutes = Math.round((finish - start) / 60000)
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
})

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

function downloadTrainingReport(): void {
  const state = session.value
  const report = state?.training_report
  if (!state || !report) return
  const profileTitle = String(state.profile?.title ?? '')
  const rows = (report.mastery_plan ?? [])
    .map((item) => (
      `<tr><td>${escapeHtml(learnerText(item.knowledge_point))}</td>`
      + `<td>${reportTierLabel(item.tier)}</td></tr>`
    ))
    .join('')
  const misconceptionRows = Object.entries(
    report.common_mistakes?.misconception_counts ?? {},
  )
    .map(([misconception, count]) => (
      `<li>${escapeHtml(misconceptionLabel(misconception))} ×${count}</li>`
    ))
    .join('')
  const exampleRows = (report.common_mistakes?.examples ?? [])
    .map((item) => (
      '<li>'
      + `<b>第${item.round ?? '—'}问</b> ${escapeHtml(learnerText(String(item.question ?? '')))}<br/>`
      + `你的回答：${escapeHtml(learnerText(String(item.answer ?? '')))}<br/>`
      + `老师评价：${escapeHtml(learnerText(String(item.feedback ?? '')))}`
      + '</li>'
    ))
    .join('')
  const html = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<title>学习报告 · ${escapeHtml(learnerText(report.knowledge_point))}</title>
<style>
  body { font-family: "PingFang SC", "Microsoft YaHei", sans-serif; color: #1f2937; margin: 0; background: #f5f6f8; }
  .page { max-width: 720px; margin: 24px auto; background: #fff; border-radius: 12px; padding: 32px 36px; box-shadow: 0 4px 16px rgba(15, 23, 42, .08); }
  h1 { font-size: 22px; margin: 0 0 4px; }
  .meta { color: #6b7280; font-size: 13px; margin-bottom: 20px; }
  h2 { font-size: 15px; margin: 24px 0 8px; color: #111827; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #eef0f3; }
  th { color: #6b7280; font-weight: 600; background: #f9fafb; }
  ul { padding-left: 18px; margin: 6px 0; font-size: 14px; line-height: 1.8; }
  .metrics { display: flex; gap: 12px; flex-wrap: wrap; margin: 12px 0; }
  .metric { flex: 1; min-width: 120px; background: #f9fafb; border-radius: 8px; padding: 10px 12px; }
  .metric span { display: block; color: #6b7280; font-size: 12px; }
  .metric b { font-size: 16px; }
  footer { margin-top: 28px; color: #9ca3af; font-size: 12px; text-align: center; }
  @media print { body { background: #fff; } .page { box-shadow: none; margin: 0; } }
</style>
</head>
<body>
<div class="page">
  <h1>学习报告 · ${escapeHtml(learnerText(report.knowledge_point))}</h1>
  <p class="meta">${escapeHtml(profileTitle)} · ${escapeHtml((report.finished_at ?? '').slice(0, 10))}${reportDurationText.value ? ` · 用时 ${reportDurationText.value}` : ''}</p>
  <div class="metrics">
    <div class="metric"><span>岗前测评正确</span><b>${report.pretest_score ? `${report.pretest_score.correct}/${report.pretest_score.total}` : '—'}</b></div>
    <div class="metric"><span>数据查询</span><b>${report.query_count} 次</b></div>
    <div class="metric"><span>理解核对</span><b>${report.follow_up_rounds} 轮</b></div>
    <div class="metric"><span>最终难度</span><b>${escapeHtml(difficultyLabel(report.final_difficulty))}档</b></div>
  </div>
  <h2>知识点掌握情况</h2>
  <table><thead><tr><th>知识点</th><th>掌握档位</th></tr></thead><tbody>${rows || '<tr><td colspan="2">—</td></tr>'}</tbody></table>
  <h2>常犯错误</h2>
  ${misconceptionRows ? `<ul>${misconceptionRows}</ul>` : '<p style="color:#6b7280;font-size:14px">本轮没有明显误区命中。</p>'}
  ${report.common_mistakes?.wrong_answer_rounds ? `<p style="font-size:14px">理解核对答错 ${report.common_mistakes.wrong_answer_rounds} 轮；SQL 查询失误 ${report.common_mistakes.sql_failure_count ?? 0} 次。</p>` : ''}
  ${exampleRows ? `<h2>典型问答回顾</h2><ul>${exampleRows}</ul>` : ''}
  <h2>学习小结</h2>
  <p style="font-size:14px;line-height:1.8">${escapeHtml(learnerText(report.achievement))}</p>
  <footer>船厂数字化岗位培训 · 本报告由系统在完成本轮知识点训练后自动生成</footer>
</div>
</body>
</html>`
  const blob = new Blob([html], { type: 'text/html;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  const date = (report.finished_at || '').slice(0, 10) || 'report'
  anchor.href = url
  anchor.download = `学习报告-${report.knowledge_point}-${date}.html`
  document.body.appendChild(anchor)
  anchor.click()
  anchor.remove()
  URL.revokeObjectURL(url)
}

const completionMessage = computed(() => {
  const value = session.value
  if (
    value?.awaiting === 'done'
    && value.interaction
    && 'message' in value.interaction
  ) {
    return learnerText(String(value.interaction.message))
  }
  return '训练完成'
})
const terminationReason = computed(() => {
  const reason = session.value?.termination?.reason_code
  if (!reason) return undefined
  return {
    model_unavailable: '内容生成服务暂时不可用',
    evidence_insufficient: '证据不足：当前内容缺少足够证据支持',
    review_exhausted: '内容在限定次数内未通过质量审核',
  }[reason]
})

const nextKnowledgePoint = computed(() => {
  const value = session.value
  const interaction = value?.interaction
  if (
    value?.awaiting !== 'done'
    || value.outcome !== 'completed'
  ) return undefined
  const reportNext = value.training_report?.next_knowledge_point
  if (typeof reportNext === 'string' && reportNext.trim()) return reportNext.trim()
  if (interaction?.kind !== 'next_learning_step') return undefined
  return interaction.knowledge_point
    || interaction.message.replace(/^下一知识点[：:]\s*/u, '').trim()
})

type SqlFeedback = Omit<VerificationFailureCopy, 'event'> & {
  event?: VerificationFailureEvent
}

const outcomeCopy: Partial<Record<InteractiveOutcome, Pick<SqlFeedback, 'title' | 'learnerMessage'>>> = {
  safe_rejected: {
    title: '查询内容需要调整',
    learnerMessage: '本题的查询未通过数据安全检查，请调整后重试。',
  },
  external_unavailable: {
    title: '查询服务暂时不可用',
    learnerMessage: '服务暂时不可用，请稍后再试。',
  },
  system_error: {
    title: '当前步骤暂时无法继续',
    learnerMessage: '当前步骤暂时无法继续，请稍后再试。',
  },
}

function publicRequestError(error: unknown, fallback: string): string {
  if (!(error instanceof InteractiveApiError)) return fallback
  const outcomeMessage = error.outcome
    ? outcomeCopy[error.outcome]?.learnerMessage
    : undefined
  return outcomeMessage ?? learnerTextOr(error.message, fallback)
}

function sqlFeedbackFrom(value: InteractiveState | undefined): SqlFeedback | undefined {
  if (value?.awaiting === 'advance') return undefined
  const payload = value?.artifact?.payload
  if (typeof payload === 'object' && payload !== null && !Array.isArray(payload)) {
    const content = (payload as Record<string, unknown>).content
    if (typeof content === 'object' && content !== null && !Array.isArray(content)) {
      const record = content as Record<string, unknown>
      const feedback = verificationFailureCopy(record.event, record)
      if (feedback) return feedback
    }
  }
  const fallback = value?.outcome ? outcomeCopy[value.outcome] : undefined
  return fallback ? { ...fallback } : undefined
}

const sqlFeedback = computed(() => sqlFeedbackFrom(session.value))

/** 定稿：五连错代执行后不自动进追问——停在 SQL 实操页，
 * 右半边用"标准答案与解析"覆盖原输入框/按钮位，学员点"继续"再进提问。 */
const scaffoldHold = ref(false)
const scaffoldProceeding = ref(false)

function scaffoldInResponse(value: InteractiveState): boolean {
  const payload = (value.artifact as { payload?: { content?: { scaffold?: unknown } } } | null)?.payload
  return Boolean(payload?.content?.scaffold)
}

async function proceedAfterScaffold(): Promise<void> {
  if (scaffoldProceeding.value || busy.value) return
  scaffoldProceeding.value = true
  try {
    for (let step = 0; step < 2; step += 1) {
      const before = session.value
      if (!before || before.awaiting !== 'advance') break
      await advance({ silent: true })
      const after = session.value
      if (!after || after.awaiting !== 'advance' || after.state === before.state) break
    }
  } finally {
    scaffoldProceeding.value = false
    scaffoldHold.value = false
  }
}

/** 0819：五连错脚手架直通——结果携带的标答与解析（仅 _scaffold 路径有值） */
const sqlScaffold = computed(() => {
  const content = props.sqlResult?.content as
    | { scaffold?: { standard_sql?: string; analysis?: string } }
    | undefined
  return content?.scaffold ?? null
})

const activeTaskContent = computed<Record<string, unknown> | undefined>(() => {
  const messages = session.value?.messages ?? []
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (!message) continue
    if (message.agent !== 'task') continue
    const payload = message.payload
    if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) continue
    const content = (payload as Record<string, unknown>).content
    if (typeof content !== 'object' || content === null || Array.isArray(content)) continue
    const contentRecord = content as Record<string, unknown>
    if (message.role === 'probe' || contentRecord.event === 'follow_up_question_ready') continue
    return contentRecord
  }
  return undefined
})
const activeTaskPrompt = computed(() => {
  const record = activeTaskContent.value
  if (!record) return undefined
  for (const field of ['contextualized_stem', 'question', 'standard_stem']) {
    const value = record[field]
    if (typeof value === 'string' && value.trim()) return learnerText(value)
  }
  return undefined
})
const activeTaskKnowledgePoint = computed(() => {
  const value = activeTaskContent.value?.knowledge_point
  return typeof value === 'string' && value.trim() ? learnerText(value) : undefined
})
function difficultyLabel(value: unknown): string {
  const labels: Record<string, string> = { basic: '基础', applied: '应用', advanced: '进阶' }
  return typeof value === 'string' ? (labels[value] ?? learnerText(value)) : '—'
}
const activeTaskDifficulty = computed(() => {
  const value = activeTaskContent.value?.difficulty
  return typeof value === 'string' ? difficultyLabel(value) : undefined
})
const activeTaskIdentity = computed(() => {
  const record = activeTaskContent.value
  if (!record) return ''
  return [
    session.value?.session_id ?? '',
    record.knowledge_point ?? '',
    record.template_id ?? '',
    record.difficulty ?? '',
    record.contextualized_stem ?? record.question ?? record.standard_stem ?? '',
  ].map(String).join('|')
})

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : []
}

function fieldLabel(value: string): string {
  const labels: Record<string, string> = {
    ship_no: '船号', process_code: '工序', period_date: '月份',
    plan_qty: '计划量', actual_qty: '实际完成量', completion_rate: '完成率',
    complete_rate: '完成率', month_label: '月份', workshop_code: '责任单元',
  }
  return labels[value] ?? value
}

const sqlHints = computed(() => {
  const authority = activeTaskContent.value?.query_authority
  const record = typeof authority === 'object' && authority !== null && !Array.isArray(authority)
    ? authority as Record<string, unknown>
    : undefined
  const outputs = stringList(record?.output_columns).map(fieldLabel)
  const filters = stringList(record?.filter_columns).map(fieldLabel)
  const groups = stringList(record?.group_by_columns).map(fieldLabel)
  const times = stringList(record?.time_values)
  const hints = [
    '先确定题目要求返回什么，再写一条 SELECT 查询；输入错误不会修改数据库，也不会丢失当前训练进度。',
    outputs.length
      ? `结果列应能回答题目，重点检查：${outputs.join('、')}。`
      : '检查 SELECT 后的结果列是否能直接回答题目。',
    [
      filters.length ? `筛选条件涉及${filters.join('、')}` : '按题目对象设置筛选条件',
      times.length ? `时间范围包含${times.join('、')}` : '',
      groups.length ? `需要按${groups.join('、')}分组比较` : '',
    ].filter(Boolean).join('；') + '。',
  ]
  if (filters.includes('工序') && times.length > 1) {
    hints.push('题目把多个工序与月份一一对应时，筛选条件也要保留这种对应关系，不能只查询整个总时间范围。')
  }
  return hints
})

watch(activeTaskIdentity, (current, previous) => {
  sqlHintLevel.value = 0
  if (previous && current !== previous) {
    sqlText.value = ''
    if (session.value?.awaiting !== 'follow_up') {
      sqlResultVisible.value = false
    }
    errorMessage.value = ''
  }
})
watch(
  () => session.value?.awaiting,
  (current, previous) => {
    if (previous !== current && current === 'advance') {
      errorMessage.value = ''
      sqlHintLevel.value = 0
      if (!isSqlResultArtifact(session.value?.artifact)) {
        sqlResultVisible.value = false
      }
    }
  },
)
const followUpInputLength = computed(
  () => followUpText.value.trim().normalize('NFKC').length,
)
const FOLLOW_UP_YES_NO_ANSWERS = [
  '是', '是的', '否', '不是', '不是的', '对', '对的', '不对',
  '正确', '错误', '同意', '不同意', '知道',
]
const FOLLOW_UP_DONT_KNOW_ANSWERS = [
  '不知道', '不知道啊', '不知道呀', '不会', '不会啊', '不清楚',
  '不知道怎么回答', '不知道如何回答', '没学过', '没学过这个',
  '无法判断', '不晓得', '不懂',
]
const FOLLOW_UP_UNRELATED_ANSWERS = [
  '今天天气不错', '随便写写', '和题目无关', '不相关', '测试一下', 'asdfgh', 'abcdef',
]
const followUpIsDontKnow = computed(() => {
  const compact = followUpText.value
    .trim()
    .normalize('NFKC')
    .replace(/\s+/gu, '')
    .replace(/[。！!？?]+$/gu, '')
  return FOLLOW_UP_DONT_KNOW_ANSWERS.includes(compact)
})
const _compactAnswer = (value: string) => value
  .trim()
  .normalize('NFKC')
  .replace(/\s+/gu, '')
  .replace(/[。！!？?]+$/gu, '')

/** 无关作答（如闲聊"咖喱饭/天气怎么样"）→ 红字提示并拦截；"不知道"族放行走引导。 */
const followUpIsUnrelated = computed(() => {
  const compact = _compactAnswer(followUpText.value)
  if (!compact || followUpIsDontKnow.value) return false
  if (FOLLOW_UP_YES_NO_ANSWERS.includes(compact)) return false
  const key = (value: string) => value
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, '')
  if (FOLLOW_UP_UNRELATED_ANSWERS.some((item) => key(item) === key(compact))) return true
  // 启发式：短句、无数字无字母、且与当前问题零汉字重叠 → 判无关
  if (compact.length > 14 || /\d/.test(compact)) return false
  // 纯拉丁字母短串（如 bzd/tmd）且非业务代码 → 无关
  if (/^[A-Za-z]{1,6}$/.test(compact) && !['ycl', 'zztp', 'aztp', 'sql'].includes(compact.toLowerCase())) return true
  // 含汉字或较长含字母的回答走 bigram 重叠检测
  if (/[A-Za-z]/.test(compact) && !/[一-鿿]/.test(compact)) return false
  const interaction = session.value?.interaction
  if (interaction?.kind !== 'free_text_follow_up') return false
  const bigrams = (value: string) => {
    const chars = [...value]
    const set = new Set<string>()
    for (let i = 0; i < chars.length - 1; i += 1) set.add(chars[i]! + chars[i + 1]!)
    return set
  }
  const question = bigrams(interaction.prompt ?? '')
  const answer = bigrams(compact)
  if (!answer.size) return false
  return ![...answer].some((gram) => question.has(gram))
})
const followUpIsVacuous = computed(() => {
  const compact = followUpText.value
    .trim()
    .normalize('NFKC')
    .replace(/\s+/gu, '')
    .replace(/[。！!？?]+$/gu, '')
  if (FOLLOW_UP_YES_NO_ANSWERS.includes(compact)) return true
  if (followUpIsUnrelated.value) return true
  const interaction = session.value?.interaction
  if (interaction?.kind !== 'free_text_follow_up') return false
  const matchKey = (value: string) => value
    .normalize('NFKC')
    .toLocaleLowerCase()
    .replace(/[^\p{L}\p{N}]/gu, '')
  return Boolean(compact) && matchKey(compact) === matchKey(interaction.prompt)
})
const canSubmitFollowUp = computed(
  () => followUpInputLength.value >= 2
    && followUpInputLength.value <= 500
    && !followUpIsVacuous.value,
)
const followUpTurns = computed(() => {
  const interaction = session.value?.interaction
  return interaction?.kind === 'free_text_follow_up' ? interaction.turns : []
})
// 需求③：历史记录翻页式浏览（默认停在最新一条）
const followUpHistoryPage = ref(0)
const historyTurn = computed(() => (
  followUpTurns.value[followUpHistoryPage.value] ?? followUpTurns.value.at(-1)
))
// 仅在新一轮产生时跳到最新；轮询刷新不重置用户翻页位置
watch(() => followUpTurns.value.length, (length, previous) => {
  if (length !== previous) followUpHistoryPage.value = Math.max(0, length - 1)
}, { immediate: true })
const latestFollowUpTurn = computed(() => {
  const turns = followUpTurns.value
  return turns.length ? turns[turns.length - 1] : undefined
})
// 0818 用户定稿口径：答题记录默认收起，展开/收起只随学员手点变化——
// 不随答题/换题/轮询自动收起或展开（此前"换题自动收起"也属状态随答题变化，已撤）。
function applyState(value: InteractiveState): void {
  // 资源生成中点击"重新选择岗位"：resetSession 已移除会话标记——迟到的
  // 生成响应（讲义/任务/追问）一律丢弃，不得把已放弃的训练内容强塞回界面。
  const activeSessionId = sessionStorage.getItem(sessionStorageKey)
  if (!activeSessionId || activeSessionId !== value.session_id) return
  session.value = value
  // 需求⑤⑧：进入提问环节时查询结果须保持可见（数据上、提问下的整板块）
  if (value.awaiting === 'follow_up') sqlResultVisible.value = true
  emit('state', value)
}

function progressFingerprint(value: InteractiveState): string {
  return [
    value.state,
    value.awaiting,
    value.messages.length,
    value.artifact ? JSON.stringify(value.artifact) : '',
  ].join('|')
}

function followUpProgressed(
  previous: InteractiveState,
  current: InteractiveState,
): boolean {
  if (current.awaiting !== 'follow_up') return true
  if (current.state !== previous.state) return true
  const previousInteraction = previous.interaction
  const currentInteraction = current.interaction
  if (
    previousInteraction?.kind !== 'free_text_follow_up'
    || currentInteraction?.kind !== 'free_text_follow_up'
  ) return false
  if (currentInteraction.retry_required) return true
  if (currentInteraction.feedback !== previousInteraction.feedback) return true
  if (currentInteraction.next_step_reason !== previousInteraction.next_step_reason) return true
  return currentInteraction.round > previousInteraction.round
    || currentInteraction.turns.length > previousInteraction.turns.length
}

async function reconcileLateAdvance(previous: InteractiveState): Promise<boolean> {
  try {
    const current = await api.getState(previous.session_id)
    if (progressFingerprint(current) === progressFingerprint(previous)) return false
    applyState(current)
    return true
  } catch {
    return false
  }
}

function connectAgentEvents(sessionId: string): void {
  if (!api.subscribeAgentEvents || eventSessionId === sessionId) return
  closeEventStream?.()
  eventSessionId = sessionId
  closeEventStream = api.subscribeAgentEvents(sessionId, (event) => {
    emit('agentEvent', event)
  })
}

function previousPretestPage(): void {
  pretestPage.value = Math.max(0, pretestPage.value - 1)
}

function nextPretestPage(): void {
  if (!currentPretestAnswered.value) return
  pretestPage.value = Math.min(questions.value.length - 1, pretestPage.value + 1)
}

async function selectProfile(profileId: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const created = await api.createSession(
      profileId,
      selectedExperienceTag.value ? [selectedExperienceTag.value] : [],
      // 0818 需求 5/6：登录学员建会话携带 token——学习记录归属该账号
      sessionStorage.getItem('ref-auth-token'),
    )
    sessionStorage.setItem(sessionStorageKey, created.session_id)
    connectAgentEvents(created.session_id)
    // 需求①：先取回前测题再切换界面，避免"开始训练"后闪现空工作台
    const fetched = await api.getPretest(created.session_id)
    applyState(created)
    questions.value = fetched
    pretestPage.value = 0
  } catch (error) {
    errorMessage.value = publicRequestError(error, '实操通道暂时不可用。')
  } finally {
    busy.value = false
  }
}

async function submitPretest(): Promise<void> {
  if (busy.value || !session.value || !pretestComplete.value) return
  busy.value = true
  errorMessage.value = ''
  let autoAdvance = false
  try {
    const value = await api.submitPretest(session.value.session_id, answers.value)
    applyState(value)
    if (value.awaiting === 'diagnostic_probe') {
      diagnosticProbes.value = await api.getDiagnosticProbes(value.session_id)
      diagnosticAnswers.value = {}
    } else if (value.awaiting === 'advance') {
      autoAdvance = true
    }
  } catch (error) {
    errorMessage.value = publicRequestError(error, '岗前测评暂时无法提交。')
  } finally {
    busy.value = false
  }
  // 需求④⑤：提交后自动推进直至练习/追问就位——直达链一口气走完，不出现中间按钮页
  if (autoAdvance) {
    autoChainRunning.value = true
    try {
    // 第一步（S2 生成）两种路径都需要；其后仅直达路径（讲义延期）继续自动推进，
    // 做错路径停在讲义阅读，由学员点击"进入练习"。链式推进为尽力而为：
    // 撞推进锁（409）等失败静默退出，交由轮询/学员继续，不弹红字。
    // S7+advance=画像三 data_present 直达路径的任务下发停驻（系统代执行窗口）——
    // 须与 S2/S3/S9 一并链内推进，否则链在此断裂、布局锁释放，闪现空白工作台
    // 中间页（0818 视频实录）；画像一/二直达任务下发落 S7+sql，awaiting 变化自然停。
    for (let step = 0; step < 5; step += 1) {
      const current = session.value
      if (!current || current.awaiting !== 'advance') break
      if (step > 0 && !firstPointPretestCorrect.value) break
      if (
        current.state === 'S2_KNOWLEDGE'
        || current.state === 'S3_TASK'
        || current.state === 'S7_STUDENT'
        || current.state === 'S9_PATH_UPDATE'
      ) {
        await advance({ silent: true })
        const next = session.value
        if (next && next.state === current.state && next.awaiting === current.awaiting) break
        continue
      }
      break
    }
    } finally {
      autoChainRunning.value = false
    }
  }
}

async function advance(options: { silent?: boolean } = {}): Promise<void> {
  if (busy.value || !session.value) return
  const previous = session.value
  busy.value = true
  if (!options.silent) errorMessage.value = ''
  try {
    applyState(await api.advance(previous.session_id))
  } catch (error) {
    const reconciled = await reconcileLateAdvance(previous)
    if (!reconciled) {
      // ③ 静默推进撞锁（409）时等 800ms 重试一次（后端 advance_lock 竞争窗口）
      if (options.silent) {
        // 静默失败直接返回——反应式 watcher 1s 后自动重试（不在此 setTimeout 阻塞）
        return
      }
      errorMessage.value = publicRequestError(error, '当前步骤暂时无法继续。')
    }
  } finally {
    busy.value = false
  }
}

async function submitSql(): Promise<void> {
  if (busy.value || !session.value || !sqlText.value.trim()) return
  busy.value = true
  errorMessage.value = ''
  let autoAdvance = false
  try {
    const value = await api.submitSql(session.value.session_id, sqlText.value.trim())
    applyState(value)
    sqlResultVisible.value = value.awaiting !== 'advance' || Boolean(value.artifact)
    // 需求②：执行查询过程中不清空输入框（学员可对照修改）
    if (value.awaiting === 'advance' && scaffoldInResponse(value)) {
      // 定稿：五连错代执行——停在 SQL 实操页展示标答（不自动跳提问页）
      scaffoldHold.value = true
      sqlResultVisible.value = true
      return
    }
    if (value.awaiting === 'advance') autoAdvance = true
  } catch (error) {
    errorMessage.value = publicRequestError(error, '查询暂时无法执行。')
  } finally {
    busy.value = false
  }
  // 需求⑤②：查询通过后自动直达提问环节；期间停留在 SQL 页（按钮反馈），链完直跳
  if (autoAdvance) {
    sqlChainRunning.value = true
    try {
      for (let step = 0; step < 2; step++) {
        const before = session.value
        if (!before || before.awaiting !== 'advance') break
        await advance({ silent: true })
        const after = session.value
        if (!after || after.awaiting !== 'advance' || after.state === before.state) break
      }
    } finally {
      sqlChainRunning.value = false
    }
  }
}

function newClientTurnId(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.()
  if (randomUuid) return randomUuid
  return `learner-${Date.now()}-${Math.random().toString(36).slice(2)}`
}

async function submitDiagnosticProbes(): Promise<void> {
  if (busy.value || !session.value || !diagnosticComplete.value) return
  busy.value = true
  errorMessage.value = ''
  let autoAdvance = false
  try {
    const value = await api.submitDiagnosticProbes(
      session.value.session_id,
      diagnosticAnswers.value,
    )
    applyState(value)
    if (value.awaiting === 'diagnostic_probe') {
      diagnosticProbes.value = await api.getDiagnosticProbes(value.session_id)
      diagnosticAnswers.value = {}
    } else if (value.awaiting === 'advance') {
      autoAdvance = true
    }
  } catch (error) {
    errorMessage.value = publicRequestError(error, '补充诊断暂时无法提交。')
  } finally {
    busy.value = false
  }
  // 需求④：补充诊断提交后同样自动生成微课，不再停在"打开岗位微课"
  if (autoAdvance) await advance()
}

async function continueLearning(): Promise<void> {
  const value = session.value
  if (busy.value || !value || !nextKnowledgePoint.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const continued = await api.continueLearning(value.session_id)
    sessionStorage.setItem(sessionStorageKey, continued.session_id)
    connectAgentEvents(continued.session_id)
    applyState(continued)
  } catch (error) {
    errorMessage.value = publicRequestError(error, '下一知识点暂时无法开始。')
  } finally {
    busy.value = false
  }
}

function updateFollowUpDraft(): void {
  if (followUpText.value.trim() !== followUpSubmittedText.value) {
    followUpClientTurnId.value = undefined
  }
}

async function submitFollowUp(): Promise<void> {
  const value = session.value
  const interaction = value?.interaction
  if (
    busy.value || !value
    || value.awaiting !== 'follow_up'
    || interaction?.kind !== 'free_text_follow_up'
    || !canSubmitFollowUp.value
  ) return
  const text = followUpText.value.trim()
  if (!isLearnerSafeText(text.normalize('NFKC'))) {
    errorMessage.value = '请用业务或学习语言描述你的判断。'
    return
  }
  const clientTurnId = followUpClientTurnId.value ?? newClientTurnId()
  followUpClientTurnId.value = clientTurnId
  followUpSubmittedText.value = text
  busy.value = true
  errorMessage.value = ''
  try {
    applyState(await api.submitFollowUp(value.session_id, text, clientTurnId))
    followUpText.value = ''
    followUpSubmittedText.value = ''
    followUpClientTurnId.value = undefined
  } catch (error) {
    let reconciled = false
    try {
      const current = await api.getState(value.session_id)
      reconciled = followUpProgressed(value, current)
      if (reconciled) applyState(current)
    } catch {
      reconciled = false
    }
    if (reconciled) {
      followUpText.value = ''
      followUpSubmittedText.value = ''
      followUpClientTurnId.value = undefined
    } else {
      errorMessage.value = publicRequestError(error, '本轮判断暂时无法提交。')
    }
  } finally {
    busy.value = false
  }
  // 需求⑧③：四次未过自动推进换证重练——S9→S2 后继续推进 S2 生成基础档讲义，
  // 不停在"直入实操"按钮页（生成期间中央显示双行提示句）。
  const after = session.value
  const afterMessage = (after?.interaction as { message?: string } | undefined)?.message
  if (
    after
    && after.awaiting === 'advance'
    && after.interaction?.kind === 'next_learning_step'
    && String(afterMessage ?? '').includes('四次')
  ) {
    for (let step = 0; step < 3; step += 1) {
      const current = session.value
      if (!current || current.awaiting !== 'advance') break
      if (current.state !== 'S9_PATH_UPDATE' && current.state !== 'S2_KNOWLEDGE') break
      await advance({ silent: true })
      const next = session.value
      if (next && next.state === current.state && next.awaiting === current.awaiting) break
    }
  }
}

async function pollState(sessionId: string): Promise<void> {
  if (pollingSessionId) return
  pollingSessionId = sessionId
  try {
    const value = await api.getState(sessionId)
    if (sessionStorage.getItem(sessionStorageKey) !== sessionId) return
    applyState(value)
    if (value.awaiting === 'pretest' && !questions.value.length) {
      questions.value = await api.getPretest(sessionId)
      pretestPage.value = 0
    }
    if (value.awaiting === 'diagnostic_probe' && !diagnosticProbes.value.length) {
      diagnosticProbes.value = await api.getDiagnosticProbes(sessionId)
      diagnosticAnswers.value = {}
    }
    errorMessage.value = ''
  } catch (error) {
    if (sessionStorage.getItem(sessionStorageKey) !== sessionId) return
    if (error instanceof InteractiveApiError && error.status === 404) {
      closeEventStream?.()
      closeEventStream = undefined
      eventSessionId = ''
      sessionStorage.removeItem(sessionStorageKey)
      session.value = undefined
      errorMessage.value = '上次训练已失效，请重新开始。'
      return
    }
    errorMessage.value = publicRequestError(error, '未能恢复上次训练，请稍后重试。')
  } finally {
    if (pollingSessionId === sessionId) pollingSessionId = ''
  }
}

function resetSession(): void {
  closeEventStream?.()
  closeEventStream = undefined
  eventSessionId = ''
  sessionStorage.removeItem(sessionStorageKey)
  session.value = undefined
  // 重新开始后回到岗位选择页（欢迎页只在首次进入时展示）；
  // 需求⑤：同时清空已选画像，确保落在"选择画像卡片"页而非训练关注点页。
  profileStep.value = 'select'
  chosenProfileId.value = null
  selectedExperienceTag.value = ''
  questions.value = []
  pretestPage.value = 0
  answers.value = {}
  sqlText.value = ''
  followUpText.value = ''
  followUpSubmittedText.value = ''
  followUpClientTurnId.value = undefined
  busy.value = false
  errorMessage.value = ''
  // 闭环五：重开训练必须重新阅读讲义并点击"进入练习"——
  // 上一轮已进入练习的标志若不清，反应式 watcher 会在新讲义
  // 落定（S3+advance）后 1s 自动推进，跳过讲义阅读直达追问。
  hasEnteredPractice.value = false
  // 资源生成中重选岗位：终止一切自动链（迟到响应由 applyState 守卫丢弃）
  clearTimeout(autoAdvanceTimer)
  autoChainRunning.value = false
  practiceChainRunning.value = false
  sqlChainRunning.value = false
  scaffoldHold.value = false
  scaffoldProceeding.value = false
  emit('reset')
}

function backToFocusSelection(): void {
  // 需求①：前测界面"返回"——回到所选岗位的训练关注点页（保留岗位选择）
  const previousProfileId = session.value?.profile?.profile_id
  resetSession()
  if (previousProfileId) chosenProfileId.value = previousProfileId
}

defineExpose({ resetSession, backToFocusSelection, advance, startPracticeChain })

/** ①③：进入练习链——S3→（画像三 data_present：S7+advance 系统代执行）→S9→追问
 * 整链 practiceChainRunning 包裹（不闪中间界面）。通用状态机循环：
 * awaiting=advance 且 state∈{S3,S7,S9} 就继续推进；awaiting 离开 advance 自然停
 * （画像1/2 一次 advance 落 S7+sql 即停，语义与旧链一致）。 */
async function startPracticeChain(): Promise<void> {
  hasEnteredPractice.value = true
  practiceChainRunning.value = true
  try {
    for (let i = 0; i < 4; i += 1) {
      const state = session.value
      if (!state || state.awaiting !== 'advance') break
      if (
        state.state !== 'S3_TASK'
        && state.state !== 'S7_STUDENT'
        && state.state !== 'S9_PATH_UPDATE'
      ) break
      await advance({ silent: true })
      const now = session.value
      // 同态即停（撞锁/恒定响应防死循环）；sql/follow_up 停驻点由 awaiting 变化兜住
      if (!now || now.state === state.state) break
      if (now.awaiting !== 'advance') break
    }
  } finally {
    practiceChainRunning.value = false
  }
}

// ②根治：反应式自动推进——S3/S9 待推进时延时自动 advance
// （watcher 替代链式调用：链断在哪个状态，watcher 就从哪个状态续推，不怕 409）
// 新讲义（下一知识点 continue / 降档补学换证重讲）落定后必须重新阅读并点击
// "进入练习"——hasEnteredPractice 随讲义消息 id 变化重置，否则 watcher 会在
// 新讲义的 S3+advance 自动推进，跳过讲义阅读（视频 0818 实录根因）。
const lectureMsgId = computed(() => {
  const messages = (session.value?.messages ?? []) as Array<{
    agent?: string
    msg_id?: string
    payload?: { type?: string }
  }>
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index]
    if (message?.agent === 'knowledge' && message.payload?.type === 'lecture_note') {
      return String(message.msg_id ?? '')
    }
  }
  return ''
})
watch(lectureMsgId, (current, previous) => {
  if (current && current !== previous) {
    hasEnteredPractice.value = false
  }
})
let autoAdvanceTimer: ReturnType<typeof setTimeout> | undefined
watch(
  () => [session.value?.state, session.value?.awaiting, busy.value] as const,
  ([state, awaiting, isBusy]) => {
    clearTimeout(autoAdvanceTimer)
    if (!state || isBusy || stepDownNoticeActive.value) return
    if (awaiting !== 'advance') return
    // S7+advance 仅画像三 data_present 代执行窗口会出现——与 S3/S9 一并自动续推
    if (
      state !== 'S3_TASK'
      && state !== 'S9_PATH_UPDATE'
      && state !== 'S7_STUDENT'
    ) return
    // 守卫：链运行中、直达路径（前测全对）、或已进入练习（chainRunning 结束后
    // practiceEntered 已被 ResourcePanel watcher 设 true）时自动推进；
    // 仅排除初始讲义阅读（从未点击"进入练习"、非直达路径）
    if (
      !autoChainRunning.value
      && !practiceChainRunning.value
      && !firstPointPretestCorrect.value
      && !hasEnteredPractice.value
    ) return
    if (scaffoldHold.value) return
    autoAdvanceTimer = setTimeout(async () => {
      const current = session.value
      if (
        !current || busy.value || stepDownNoticeActive.value
        || current.awaiting !== 'advance'
        || (
          current.state !== 'S3_TASK'
          && current.state !== 'S9_PATH_UPDATE'
          && current.state !== 'S7_STUDENT'
        )
      ) return
      if (
        !autoChainRunning.value
        && !practiceChainRunning.value
        && !firstPointPretestCorrect.value
        && !hasEnteredPractice.value
      ) return
      if (scaffoldHold.value) return
      await advance({ silent: true })
    }, 1000)
  },
  { immediate: true },
)

// 需求③：四次未过后 S2 停留兜底——链断了时延时自动重推
let stepDownRetryTimer: ReturnType<typeof setTimeout> | undefined
watch(stepDownNoticeActive, (active) => {
  clearTimeout(stepDownRetryTimer)
  if (!active) return
  stepDownRetryTimer = setTimeout(async () => {
    if (stepDownNoticeActive.value && !busy.value) {
      await advance({ silent: true })
    }
  }, 3000)
})

onMounted(async () => {
  const storedSession = sessionStorage.getItem(sessionStorageKey)
  if (storedSession) {
    connectAgentEvents(storedSession)
    await pollState(storedSession)
  }
  if (props.pollIntervalMs > 0) {
    pollTimer = window.setInterval(() => {
      const value = session.value
      const sessionId = value?.session_id ?? sessionStorage.getItem(sessionStorageKey)
      if (!busy.value && sessionId && value?.awaiting !== 'done') {
        void pollState(sessionId)
      }
    }, props.pollIntervalMs)
  }
})

onBeforeUnmount(() => {
  closeEventStream?.()
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <section
    class="live-practice panel"
    :class="{
      'is-profile-picker': !session,
      'is-profile-welcome': !session && profileStep === 'welcome',
      'is-profile-select': !session && profileStep === 'select',
      'has-inline-result': Boolean(session && session.awaiting !== 'done' && sqlResult) && !sqlResultStale,
    }"
    aria-label="岗位实操通道"
  >
    <header v-if="session && !operationOnly" class="live-practice-heading">
      <div>
        <span class="section-kicker">当前任务</span>
        <h2>{{ stationTitle }}</h2>
      </div>
      <button
        type="button"
        class="restart-training"
        aria-label="重新选择岗位"
        :disabled="busy"
        @click="resetSession"
      >重新选择岗位</button>
    </header>

    <!-- ①②③：独立全屏 pending——组件根级 fixed，不依赖 live-action-block -->
    <!-- S7+advance 且已进入练习=画像三代执行窗口（含链断后 watcher 续推前空窗）也常显 pending -->
    <div
      v-if="session && !sqlChainRunning && !practiceChainRunning && !scaffoldHold && (autoChainRunning || stepDownNoticeActive || (busy && session.awaiting === 'advance') || (hasEnteredPractice && session.state === 'S7_STUDENT' && session.awaiting === 'advance'))"
      class="generation-pending-overlay"
      role="status"
      aria-live="polite"
      data-testid="generation-pending"
    >
      <!-- 优化15：等待提示逐字波浪跳动（含省略号），表达运行中而非卡死 -->
      <p class="generation-pending"><WaveText :text="pendingPromptText" /></p>
    </div>

    <!-- 优化：查询结果与"标准答案与解析"同组展示——标答属于查询结果的一部分，
         不再作为独立卡片夹在提问页面中间 -->
    <div
      v-if="session && session.awaiting !== 'done' && sqlResult && sqlResultVisible && !sqlResultStale && !sqlChainRunning && !sqlFeedback && !scaffoldHold"
      class="sql-result-group"
    >
      <SqlResultTable
        class="task-inline-result"
        title="拖动右下角可调整查询结果高度"
        :message="sqlResult"
        live-operation
      />
    </div>

    <section
      v-if="!session && profileStep === 'welcome'"
      class="profile-picker-hero"
      aria-labelledby="profile-picker-title"
    >
      <div class="profile-picker-copy">
        <span class="profile-picker-eyebrow">个性化岗位训练</span>
        <!-- 0819 bug2：主标题更聚焦"岗位任务+数据能力"；副句小字按用户要求删除 -->
        <h1 id="profile-picker-title">从岗位任务出发，练出上手就能用的数据能力</h1>
      </div>
      <ol class="profile-picker-flow" aria-label="岗位训练流程">
        <li><span>01</span><strong>选择岗位</strong><small>确定学习起点</small></li>
        <li><span>02</span><strong>岗前测评</strong><small>识别知识盲区</small></li>
        <li><span>03</span><strong>岗位微课</strong><small>讲透口径与误区</small></li>
        <li><span>04</span><strong>数据实操</strong><small>查询数据核对结论</small></li>
      </ol>
      <button
        type="button"
        class="profile-picker-cta"
        :disabled="busy"
        @click="profileStep = 'select'"
      >开始选择岗位 <span aria-hidden="true">→</span></button>
    </section>

    <header v-if="!session && profileStep === 'select'" class="profile-picker-heading">
      <button
        v-if="chosenProfileId"
        type="button"
        class="profile-picker-back"
        :disabled="busy"
        @click="chosenProfileId = null"
      ><span aria-hidden="true">←</span> 重新选择岗位</button>
      <button
        v-else
        type="button"
        class="profile-picker-back"
        :disabled="busy"
        @click="profileStep = 'welcome'"
      ><span aria-hidden="true">←</span> 返回欢迎页</button>
      <div v-if="!chosenProfileId">
        <span class="section-kicker">选择训练路径</span>
        <h2>哪一种经历最接近你？</h2>
      </div>
    </header>

    <div v-if="!session && profileStep === 'select' && !chosenProfileId" class="profile-choice-grid">
      <button
        v-for="(profile, index) in profiles"
        :key="profile.id"
        type="button"
        class="profile-choice"
        :aria-label="`选择${learnerText(profile.title)}`"
        :disabled="busy"
        @click="chooseProfile(profile.id)"
      >
        <span class="profile-choice-index">岗位 {{ String(index + 1).padStart(2, '0') }}</span>
        <span class="profile-choice-copy">
          <strong>{{ learnerText(profile.title) }}</strong>
          <small>{{ learnerText(profile.background) }}</small>
          <!-- 0819 bug3：画像卡下方的特长小椭圆按用户要求删除 -->
        </span>
        <span class="profile-choice-action">选择岗位 <span aria-hidden="true">→</span></span>
      </button>
    </div>

    <!-- 闭环一：选定岗位后，训练关注点仅列该岗位学习领域内的知识点 -->
    <section
      v-if="!session && profileStep === 'select' && chosenProfile"
      class="profile-focus-panel"
      aria-label="选择训练关注点"
    >
      <div class="profile-focus-copy">
        <span class="section-kicker">已选岗位</span>
        <h3>{{ learnerText(chosenProfile.title) }}</h3>
        <p>{{ learnerText(chosenProfile.background) }}</p>
        <!-- 0819 bug4：关注点页的特长小椭圆与下拉说明小字按用户要求删除 -->
      </div>
      <div class="profile-route-focus">
        <label for="experience-focus">训练关注点</label>
        <select id="experience-focus" v-model="selectedExperienceTag" :disabled="busy">
          <option value="">由岗前测评自动诊断</option>
          <option
            v-for="tag in scopedExperienceTags"
            :key="tag.tag_id"
            :value="tag.tag_id"
          >{{ learnerText(tag.label) }} · {{ learnerText(tag.knowledge_point) }}</option>
        </select>
      </div>
      <button
        type="button"
        class="profile-picker-cta"
        :disabled="busy"
        @click="selectProfile(chosenProfile.id)"
      >{{ busy ? '正在创建会话…' : '开始训练' }} <span aria-hidden="true">→</span></button>
    </section>

    <div v-if="!session" class="profile-picker-void" aria-hidden="true"></div>

    <form
      v-else-if="session.awaiting === 'pretest'"
      class="pretest-form"
      @submit.prevent="submitPretest"
    >
      <header class="pretest-heading">
        <div>
          <strong>{{ learnerText(session.profile.title) }}</strong>
        </div>
        <div class="pretest-progress-copy" aria-live="polite">
          <b>{{ pretestPage + 1 }}</b><span>/ {{ questions.length }}</span>
          <small>已作答 {{ answeredPretestCount }} 题</small>
        </div>
      </header>
      <ol class="pretest-page-dots" aria-label="测评题目进度">
        <li
          v-for="(question, index) in questions"
          :key="question.question_id"
          :class="{
            'is-current': index === pretestPage,
            'is-complete': Boolean(answers[question.question_id]),
          }"
        >
          <button
            type="button"
            :aria-label="`前往第${index + 1}题`"
            :aria-current="index === pretestPage ? 'step' : undefined"
            @click="pretestPage = index"
          >{{ index + 1 }}</button>
        </li>
      </ol>
      <fieldset
        v-if="currentPretestQuestion"
        :key="currentPretestQuestion.question_id"
        class="pretest-question"
      >
        <legend><span>{{ pretestPage + 1 }}</span>{{ learnerText(currentPretestQuestion.stem) }}</legend>
        <label v-for="(label, option) in currentPretestQuestion.options" :key="option">
          <input
            v-model="answers[currentPretestQuestion.question_id]"
            type="radio"
            :name="currentPretestQuestion.question_id"
            :value="option"
          />
          <span class="option-letter">{{ option }}</span>
          <span class="option-copy">{{ learnerText(label) }}</span>
        </label>
      </fieldset>
      <footer class="pretest-page-actions">
        <button
          type="button"
          class="secondary-action"
          :disabled="busy || pretestPage === 0"
          @click="previousPretestPage"
        >上一题</button>
        <button
          v-if="!isLastPretestPage"
          type="button"
          class="primary-action"
          :disabled="busy || !currentPretestAnswered"
          @click="nextPretestPage"
        >下一题</button>
        <button
          v-else
          class="primary-action"
          type="submit"
          :disabled="busy || !pretestComplete"
        >提交岗前测评</button>
      </footer>
    </form>

    <form
      v-else-if="session.awaiting === 'diagnostic_probe'"
      class="pretest-form diagnostic-probe-form"
      @submit.prevent="submitDiagnosticProbes"
    >
      <!-- 优化5：探针界面精简——删"补充诊断"题头/道数进度/说明小字/小题属性行/题号，
           起点行改口径为"训练关注点" -->
      <header class="pretest-heading">
        <div>
          <strong>确认你的学习起点</strong>
        </div>
      </header>
      <section class="diagnostic-route-preview" v-if="session.interaction?.kind === 'supplemental_diagnosis'">
        <strong>训练关注点：{{ learnerText(session.interaction.provisional_route?.knowledge_point || '待确认') }}</strong>
      </section>
      <fieldset
        v-for="probe in diagnosticProbes"
        :key="probe.probe_id"
        class="pretest-question diagnostic-probe-question"
      >
        <legend>{{ learnerText(probe.stem) }}</legend>
        <textarea
          v-model="diagnosticAnswers[probe.probe_id]"
          rows="3"
          maxlength="500"
          placeholder="请用自己的话回答上面的问题，不确定也可以写下你的理解"
        ></textarea>
      </fieldset>
      <footer class="pretest-page-actions">
        <button
          class="primary-action"
          type="submit"
          :disabled="busy || !diagnosticComplete"
        >提交补充诊断</button>
      </footer>
    </form>

    <div v-else-if="advanceAction && !sqlChainRunning && !scaffoldHold" class="live-action-block">
      <section
        v-if="!operationOnly && (session.interaction?.feedback || session.interaction?.next_step_reason)"
        class="answer-feedback-card"
        aria-live="polite"
      >
        <strong>本轮评价</strong>
        <p v-if="session.interaction.feedback">{{ learnerText(session.interaction.feedback) }}</p>
        <small v-if="session.interaction.next_step_reason">
          进入下一步的理由：{{ learnerText(session.interaction.next_step_reason) }}
        </small>
      </section>
      <p
        v-if="session.interaction?.kind === 'learning_notice' && !busy && !stepDownNoticeActive"
        class="learning-notice"
        data-testid="learning-notice"
      >{{ learnerText(session.interaction.message) }}</p>
      <!-- 闭环四：前测答对点微课懒生成——直入实操提示（蓝图 3.8 UI 口径） -->
      <p
        v-if="session.interaction?.kind === 'lecture_deferred' && !busy && !autoChainRunning"
        class="learning-notice lecture-deferred-notice"
        data-testid="lecture-deferred-notice"
      >{{ learnerText(session.interaction.message || '已由前测验证，直入实操（未通过将自动配发微课）') }}</p>
      <!-- 需求①⑥：画像三代执行通知已删——仅保留中央 pending 提示句 -->
      <button
        v-if="!busy && !directPathIdle"
        type="button"
        class="primary-action"
        :aria-label="advanceAction"
        @click="advance()"
      >{{ advanceAction }}</button>
    </div>

    <div
      v-else-if="session.awaiting === 'sql' || sqlChainRunning || scaffoldHold"
      class="sql-workbench"
    >
      <!-- 定稿：标答停留页删"查询练习"头行，标答置顶 -->
      <header v-if="!scaffoldHold">
        <span>查询练习</span>
      </header>
      <article v-if="!operationOnly && !scaffoldHold && activeTaskPrompt" class="sql-task-brief">
        <span>本题任务</span>
        <h3>{{ activeTaskPrompt }}</h3>
        <small v-if="activeTaskKnowledgePoint || activeTaskDifficulty">
          {{ [activeTaskKnowledgePoint, activeTaskDifficulty && `${activeTaskDifficulty}难度`].filter(Boolean).join(' · ') }}
        </small>
      </article>
      <section v-if="!operationOnly && !scaffoldHold" class="sql-teacher-hint" aria-label="提示">
        <div>
          <strong>提示</strong>
          <button
            type="button"
            class="text-action"
            @click="sqlHintLevel = sqlHintLevel >= sqlHints.length ? 0 : sqlHintLevel + 1"
          >{{ sqlHintLevel >= sqlHints.length ? '收起提示' : `查看提示 ${sqlHintLevel + 1}/${sqlHints.length}` }}</button>
        </div>
        <ol v-if="sqlHintLevel">
          <li v-for="hint in sqlHints.slice(0, sqlHintLevel)" :key="hint">{{ hint }}</li>
        </ol>
      </section>
      <p v-if="!operationOnly && !scaffoldHold" class="sql-safety-note">
        查询输错时会留在本题并提示修改；这里只会查看数据，不会改动任何业务数据。
      </p>
      <p
        v-if="session.interaction?.kind === 'learning_notice' && !busy && !stepDownNoticeActive"
        class="learning-notice"
        data-testid="learning-notice"
      >{{ learnerText(session.interaction.message) }}</p>
      <!-- 定稿：五连错代执行后，右半边用"标准答案与解析"覆盖原输入框/按钮位 -->
      <template v-if="scaffoldHold">
        <section
          v-if="sqlScaffold"
          class="scaffold-reveal is-workbench is-replacing"
          aria-label="标准答案与解析"
        >
          <header>
            <strong>标准答案与解析</strong>
          </header>
          <pre class="scaffold-sql">{{ sqlScaffold.standard_sql }}</pre>
          <p class="scaffold-analysis">{{ learnerText(sqlScaffold.analysis ?? '') }}</p>
        </section>
        <button
          type="button"
          class="primary-action"
          aria-label="继续进入提问"
          :disabled="scaffoldProceeding"
          @click="proceedAfterScaffold"
        >{{ scaffoldProceeding ? '' : '继续进入提问' }}<WaveText v-if="scaffoldProceeding" text="正在进入" /></button>
      </template>
      <template v-else>
      <textarea
        v-model="sqlText"
        aria-label="输入查询语句"
        rows="8"
        spellcheck="false"
        placeholder="SELECT ..."
      ></textarea>
      <button
        type="button"
        class="primary-action"
        aria-label="运行查询"
        :disabled="busy || sqlChainRunning || !sqlText.trim()"
        @click="submitSql"
      >{{ sqlChainRunning ? '' : '运行查询' }}<WaveText v-if="sqlChainRunning" text="正在进行查询" /></button>
      <!-- 修复3：输错红色横幅移至"运行查询"按钮下方（按钮位置固定不变）——
           原"拦截提示"红框与"实操老师"蓝框合并为"第 N 次提示"；外部故障以
           "查询提示"同款横幅；结果区失败占位由 sqlFeedback 联动隐藏 -->
      <aside
        v-if="(session.sql_support || sqlFeedback) && session.awaiting === 'sql'"
        class="sql-hint-banner is-below-button"
        :data-level="session.sql_support?.level"
        role="alert"
      >
        <strong>{{ session.sql_support ? `第 ${session.sql_support.attempt} 次提示` : '查询提示' }}</strong>
        <p>{{ learnerText(session.sql_support?.hint || sqlFeedback?.learnerMessage || '') }}</p>
      </aside>
      </template>
    </div>

    <section
      v-else-if="
        session.awaiting === 'follow_up'
          && session.interaction?.kind === 'free_text_follow_up'
      "
      class="follow-up-workbench"
      aria-label="老师提问"
    >
      <!-- 需求④：画像三追问区重复结果表已删——顶部 task-inline-result 已展示数据 -->

      <header class="follow-up-heading">
        <div>
          <span>提问环节</span>
        </div>
        <!-- 需求⑨：随时查看讲义（App 层切换双栏，左侧完整讲义） -->
        <button
          v-if="hasRealLecture"
          type="button"
          class="lecture-peek-toggle"
          :aria-label="lecturePeekOpen ? '收起讲义' : '查看讲义'"
          @click="emit('lecturePeek', !lecturePeekOpen)"
        >{{ lecturePeekOpen ? '收起讲义' : '查看讲义' }}</button>
      </header>

      <section
        v-if="followUpTurns.length"
        class="follow-up-history-panel"
        :class="{ 'is-expanded': followUpHistoryExpanded }"
        aria-label="已完成的提问记录"
      >
        <button
          type="button"
          class="follow-up-history-toggle"
          :aria-expanded="followUpHistoryExpanded"
          @click="followUpHistoryExpanded = !followUpHistoryExpanded"
        >
          <span>已完成 {{ followUpTurns.length }} 轮</span>
          <strong>{{ followUpHistoryExpanded ? '收起记录' : '查看完整记录' }}</strong>
        </button>
        <div v-if="followUpHistoryExpanded" class="follow-up-history-pager">
          <div v-if="historyTurn" class="follow-up-history" aria-live="polite">
            <p class="follow-up-question">
              <b>问题{{ historyTurn.round }}</b>
              {{ learnerText(historyTurn.question) }}
            </p>
            <p class="follow-up-answer">
              <b>你的回答</b>
              {{ learnerText(historyTurn.answer) }}
            </p>
            <p v-if="historyTurn.feedback" class="follow-up-feedback">
              <b>评价</b>
              {{ learnerText(historyTurn.feedback) }}
            </p>
          </div>
          <footer class="follow-up-history-pager-actions">
            <button
              type="button"
              :disabled="followUpHistoryPage <= 0"
              aria-label="上一条记录"
              @click="followUpHistoryPage -= 1"
            >上一条</button>
            <span>{{ followUpHistoryPage + 1 }} / {{ followUpTurns.length }}</span>
            <button
              type="button"
              :disabled="followUpHistoryPage >= followUpTurns.length - 1"
              aria-label="下一条记录"
              @click="followUpHistoryPage += 1"
            >下一条</button>
          </footer>
        </div>
        <article v-else-if="latestFollowUpTurn" class="follow-up-latest-summary">
          <p>
            <b>你的回答</b>
            {{ learnerText(latestFollowUpTurn.answer) }}
          </p>
          <!-- 0818：live 模式同样内联展示评价——追问态左侧微课栏已让位
               （is-followup-focus）， ResourcePanel 的"本轮小结"不可见，
               若此处再按 !operationOnly 屏蔽，学员将完全看不到评价 -->
          <p v-if="session.interaction.feedback || latestFollowUpTurn.feedback">
            <b>老师评价</b>
            {{ learnerText(session.interaction.feedback || latestFollowUpTurn.feedback || '') }}
          </p>
          <small v-if="session.interaction.next_step_reason">
            继续本知识点：{{ learnerText(session.interaction.next_step_reason) }}
          </small>
        </article>
      </section>

      <section
        v-else-if="session.interaction.feedback || session.interaction.next_step_reason"
        class="answer-feedback-card is-compact"
        aria-live="polite"
      >
        <!-- 0818：质量门保留（如"不知道"触发路由校验失败）时无 turn 记录、
             原题重出——评价兜底卡在 live 模式也必须可见，否则提交后无任何反馈 -->
        <strong>老师评价</strong>
        <p v-if="session.interaction.feedback">{{ learnerText(session.interaction.feedback) }}</p>
        <small v-if="session.interaction.next_step_reason">
          继续本知识点：{{ learnerText(session.interaction.next_step_reason) }}
        </small>
      </section>

      <article class="follow-up-current">
        <span>第 {{ session.interaction.round }} 轮</span>
        <h3>{{ learnerText(session.interaction.prompt) }}</h3>
      </article>

      <label class="follow-up-input">
        <span>你的判断</span>
        <textarea
          v-model="followUpText"
          aria-label="输入你的判断"
          rows="5"
          maxlength="500"
          placeholder="请引用查询结果或业务依据，例如：YCL 为 62.36%，低于另外两道工序，因此……"
          :disabled="busy"
          @input="updateFollowUpDraft"
          @keydown.ctrl.enter.prevent="submitFollowUp"
          @keydown.meta.enter.prevent="submitFollowUp"
        ></textarea>
      </label>
      <footer class="follow-up-actions">
        <!-- 0818：三句引导提示同粗细（600）；"没关系（不知道引导）"用黑色
             （--paper-ink），另两句拦截提示保持琥珀色 needs-evidence -->
        <small :class="{ 'needs-evidence': followUpIsVacuous, 'dont-know-hint': followUpIsDontKnow }">
          {{ followUpIsUnrelated
            ? '请输入与题目相关的回答，并结合上方查询结果说明你的判断。'
            : followUpIsVacuous
              ? '请用自己的话回答，不能只答“是/否”，也不要复述题目。'
              : followUpIsDontKnow
                ? '没关系，直接提交也可以，下一问会给你提示。'
                : `${followUpInputLength} / 500 字` }}
        </small>
        <button
          type="button"
          class="primary-action"
          aria-label="提交本轮判断"
          :disabled="busy || !canSubmitFollowUp"
          @click="submitFollowUp"
        >提交本轮判断</button>
      </footer>
      <p
        v-if="busy"
        class="follow-up-progress"
        role="status"
        aria-live="polite"
      ><WaveText text="正在根据你的回答生成并审核下一步内容…" /></p>

    </section>

    <div v-else-if="session.awaiting === 'done'" class="live-complete">
      <strong>{{ completionMessage }}</strong>
      <section
        v-if="session.termination && terminationReason"
        class="termination-explanation"
        data-testid="termination-explanation"
        aria-label="本轮安全结束说明"
      >
        <span>结束原因</span>
        <h3>{{ terminationReason }}</h3>
        <p>
          已完成 {{ session.termination.review_attempts }}/{{ session.termination.review_limit }} 轮质量审核；
          系统没有向学员交付未通过审核的内容。
        </p>
      </section>
      <section v-if="session.training_report" class="training-report" aria-label="本轮训练报告">
        <header>
          <span>本轮训练报告</span>
          <h3>{{ learnerText(session.training_report.knowledge_point) }}</h3>
        </header>
        <div class="training-report-metrics">
          <article v-if="session.training_report.pretest_score">
            <span>岗前测评正确</span>
            <strong>
              {{ session.training_report.pretest_score.correct }}/{{ session.training_report.pretest_score.total }}
            </strong>
          </article>
          <article>
            <span>数据查询</span>
            <strong>{{ session.training_report.query_count }} 次</strong>
          </article>
          <article>
            <span>最终核对</span>
            <strong>{{ session.training_report.follow_up_rounds }} 轮</strong>
          </article>
          <article>
            <span>结论修正</span>
            <strong>{{ session.training_report.completed_correction ? '已完成' : (session.training_report.deferred_knowledge_points?.length ? '待后续补学' : '无需修正') }}</strong>
          </article>
          <article>
            <span>最终难度</span>
            <strong>{{ difficultyLabel(session.training_report.final_difficulty) }}档</strong>
          </article>
        </div>
        <p>{{ learnerText(session.training_report.achievement) }}</p>
        <p v-if="session.training_report.deferred_knowledge_points?.length" class="training-report-deferred">
          后续补学：{{ session.training_report.deferred_knowledge_points.map(learnerText).join('、') }}
        </p>

        <!-- 0818 需求 3：每知识点掌握档位 + 常犯错误 + 学习时长 + 报告下载 -->
        <div
          v-if="session.training_report.mastery_plan?.length"
          class="training-report-mastery"
          aria-label="各知识点掌握档位"
        >
          <strong>知识点掌握情况</strong>
          <ul>
            <li
              v-for="item in session.training_report.mastery_plan"
              :key="item.knowledge_point"
            >
              <span>{{ learnerText(item.knowledge_point) }}</span>
              <b>{{ reportTierLabel(item.tier) }}</b>
            </li>
          </ul>
        </div>
        <div
          v-if="hasCommonMistakes"
          class="training-report-mistakes"
          aria-label="本轮常犯错误"
        >
          <strong>本轮常犯错误</strong>
          <ul>
            <li
              v-for="(count, misconception) in session.training_report.common_mistakes?.misconception_counts"
              :key="misconception"
            >
              <span>{{ misconceptionLabel(String(misconception)) }} ×{{ count }}</span>
            </li>
            <li v-if="session.training_report.common_mistakes?.wrong_answer_rounds">
              <span>理解核对答错 {{ session.training_report.common_mistakes.wrong_answer_rounds }} 轮</span>
            </li>
            <li v-if="session.training_report.common_mistakes?.sql_failure_count">
              <span>SQL 查询失误 {{ session.training_report.common_mistakes.sql_failure_count }} 次</span>
            </li>
          </ul>
        </div>
        <footer class="training-report-actions">
          <span v-if="reportDurationText">本次学习用时 {{ reportDurationText }}</span>
          <button
            type="button"
            class="report-download"
            aria-label="下载学习报告"
            @click="downloadTrainingReport"
          >下载学习报告</button>
        </footer>
      </section>
      <section v-if="nextKnowledgePoint" class="training-report-next">
        <p>本知识点已经达标，下一知识点：{{ learnerText(nextKnowledgePoint) }}</p>
        <button
          type="button"
          class="primary-action live-next-action"
          aria-label="开始下一知识点"
          :disabled="busy"
          @click="continueLearning"
        >{{ busy ? '' : '开始下一知识点' }}<WaveText v-if="busy" text="正在衔接…" /></button>
      </section>
    </div>

    <p v-if="errorMessage" class="live-error" role="alert">{{ errorMessage }}</p>
  </section>
</template>
