<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import profiles from 'virtual:profile-catalog'

import {
  createInteractiveApi,
  type AgentActivityEvent,
  InteractiveApiError,
  type InteractiveApi,
  type InteractiveOutcome,
  type InteractivePretestQuestion,
  type InteractiveState,
} from '../lib/interactiveApi'
import type { TraceMessage } from '../types/trace'
import SqlResultTable from './SqlResultTable.vue'
import {
  isLearnerSafeText,
  learnerText,
  learnerTextOr,
  verificationFailureCopy,
  type VerificationFailureCopy,
  type VerificationFailureEvent,
} from '../lib/tracePresentation'


const props = withDefaults(defineProps<{
  api?: InteractiveApi
  pollIntervalMs?: number
  sqlResult?: TraceMessage
  operationOnly?: boolean
}>(), {
  pollIntervalMs: 1500,
  operationOnly: false,
})

const emit = defineEmits<{
  state: [value: InteractiveState]
  agentEvent: [value: AgentActivityEvent]
  reset: []
}>()

const api = props.api ?? createInteractiveApi()
const sessionStorageKey = 'ref-interactive-session'
const session = ref<InteractiveState>()
const questions = ref<InteractivePretestQuestion[]>([])
const answers = ref<Record<string, string>>({})
const pretestPage = ref(0)
const sqlText = ref('')
const followUpText = ref('')
const followUpClientTurnId = ref<string>()
const followUpSubmittedText = ref('')
const followUpHistoryExpanded = ref(false)
const sqlHintLevel = ref(0)
const busy = ref(false)
const errorMessage = ref('')
let pollTimer: number | undefined
let pollingSessionId = ''
let closeEventStream: (() => void) | undefined
let eventSessionId = ''

const pretestComplete = computed(() => questions.value.length === 5
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
const stationTitle = computed(() => {
  if (session.value?.awaiting === 'pretest') return '岗前评测'
  if (session.value?.awaiting === 'done') return '训练报告'
  if (session.value?.state === 'S2_KNOWLEDGE') return '微课准备'
  return '实操'
})

const advanceAction = computed(() => {
  const value = session.value
  if (!value || value.awaiting !== 'advance') return undefined
  if (value.state === 'S2_KNOWLEDGE') return '打开岗位微课'
  if (value.state === 'S3_TASK') return '领取实操任务'
  if (value.state === 'S9_PATH_UPDATE'
    && (value.interaction?.kind === 'data_collision'
      || value.interaction?.kind === 'next_learning_step')) {
    return '查看下一步训练'
  }
  if (value.state === 'S9_PATH_UPDATE' && value.interaction?.kind === 'learning_notice') {
    return '完成本次训练'
  }
  if (value.state === 'S9_PATH_UPDATE') return '判断查询结论'
  return '继续训练'
})

const completionMessage = computed(() => {
  const value = session.value
  if (
    value?.awaiting === 'done'
    && value.interaction
    && 'message' in value.interaction
  ) {
    return learnerText(value.interaction.message)
  }
  return '训练完成'
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

function stringList(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string' && Boolean(item.trim()))
    : []
}

function fieldLabel(value: string): string {
  const labels: Record<string, string> = {
    ship_no: '船号', process_code: '工序', period_date: '月份',
    plan_qty: '计划量', actual_qty: '实际完成量', completion_rate: '完成率',
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
  return [
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
})

watch(() => activeTaskPrompt.value, () => {
  sqlHintLevel.value = 0
})
const followUpInputLength = computed(
  () => followUpText.value.trim().normalize('NFKC').length,
)
const followUpIsVacuous = computed(() => {
  const compact = followUpText.value
    .trim()
    .normalize('NFKC')
    .replace(/\s+/gu, '')
    .replace(/[。！!？?]+$/gu, '')
  if ([
    '是', '是的', '否', '不是', '不是的', '对', '对的', '不对',
    '正确', '错误', '同意', '不同意', '知道', '不知道', '不会',
    '不清楚', '不知道怎么回答', '不知道如何回答', '没学过', '无法判断',
    '不晓得', '不懂',
  ].includes(compact)) return true
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
const latestFollowUpTurn = computed(() => {
  const turns = followUpTurns.value
  return turns.length ? turns[turns.length - 1] : undefined
})
const followUpTaskPrompt = computed(() => {
  const interaction = session.value?.interaction
  if (interaction?.kind === 'free_text_follow_up' && interaction.task_prompt?.trim()) {
    return learnerText(interaction.task_prompt)
  }
  return activeTaskPrompt.value
})
const followUpFocus = computed(() => {
  const interaction = session.value?.interaction
  return interaction?.kind === 'free_text_follow_up' && interaction.focus?.trim()
    ? learnerText(interaction.focus)
    : undefined
})

watch(
  () => {
    const interaction = session.value?.interaction
    return interaction?.kind === 'free_text_follow_up'
      ? `${session.value?.session_id}:${interaction.round}`
      : ''
  },
  () => {
    followUpHistoryExpanded.value = false
  },
)

function applyState(value: InteractiveState): void {
  session.value = value
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
    const created = await api.createSession(profileId)
    sessionStorage.setItem(sessionStorageKey, created.session_id)
    connectAgentEvents(created.session_id)
    applyState(created)
    questions.value = await api.getPretest(created.session_id)
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
  try {
    applyState(await api.submitPretest(session.value.session_id, answers.value))
  } catch (error) {
    errorMessage.value = publicRequestError(error, '岗前测评暂时无法提交。')
  } finally {
    busy.value = false
  }
}

async function advance(): Promise<void> {
  if (busy.value || !session.value) return
  const previous = session.value
  busy.value = true
  errorMessage.value = ''
  try {
    applyState(await api.advance(previous.session_id))
  } catch (error) {
    if (!(await reconcileLateAdvance(previous))) {
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
  try {
    const value = await api.submitSql(session.value.session_id, sqlText.value.trim())
    applyState(value)
    if (!sqlFeedbackFrom(value)) sqlText.value = ''
  } catch (error) {
    errorMessage.value = publicRequestError(error, '查询暂时无法执行。')
  } finally {
    busy.value = false
  }
}

function newClientTurnId(): string {
  const randomUuid = globalThis.crypto?.randomUUID?.()
  if (randomUuid) return randomUuid
  return `learner-${Date.now()}-${Math.random().toString(36).slice(2)}`
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
  questions.value = []
  pretestPage.value = 0
  answers.value = {}
  sqlText.value = ''
  followUpText.value = ''
  followUpSubmittedText.value = ''
  followUpClientTurnId.value = undefined
  busy.value = false
  errorMessage.value = ''
  emit('reset')
}

defineExpose({ resetSession })

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
      'has-inline-result': Boolean(session && session.awaiting !== 'done' && sqlResult),
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
        aria-label="重新开始训练"
        :disabled="busy"
        @click="resetSession"
      >重新开始</button>
    </header>

    <SqlResultTable
      v-if="session && session.awaiting !== 'done' && sqlResult"
      class="task-inline-result"
      title="拖动右下角可调整查询结果高度"
      :message="sqlResult"
    />

    <section v-if="!session" class="profile-picker-hero" aria-labelledby="profile-picker-title">
      <div class="profile-picker-copy">
        <span class="profile-picker-eyebrow">个性化岗位训练</span>
        <h1 id="profile-picker-title">从你的岗位出发，建立真正用得上的数字化能力</h1>
        <p>选择与你当前经历最接近的岗位。系统会据此调整讲解重点、任务难度和练习顺序。</p>
      </div>
      <ol class="profile-picker-flow" aria-label="岗位训练流程">
        <li><span>01</span><strong>选择岗位</strong><small>确定学习起点</small></li>
        <li><span>02</span><strong>岗前测评</strong><small>识别知识盲区</small></li>
        <li><span>03</span><strong>微课与实操</strong><small>按能力动态适配</small></li>
        <li><span>04</span><strong>理解核对</strong><small>形成可验证结果</small></li>
      </ol>
    </section>

    <header v-if="!session" class="profile-picker-heading">
      <div>
        <span class="section-kicker">选择训练路径</span>
        <h2>哪一种经历最接近你？</h2>
      </div>
      <p>三个岗位共享相同培养目标，但学习内容与难度会因人而异。</p>
    </header>

    <div v-if="!session" class="profile-choice-grid">
      <button
        v-for="(profile, index) in profiles"
        :key="profile.id"
        type="button"
        class="profile-choice"
        :aria-label="`选择${learnerText(profile.title)}`"
        :disabled="busy"
        @click="selectProfile(profile.id)"
      >
        <span class="profile-choice-index">岗位 {{ String(index + 1).padStart(2, '0') }}</span>
        <span class="profile-choice-copy">
          <strong>{{ learnerText(profile.title) }}</strong>
          <small>{{ learnerText(profile.background) }}</small>
          <span v-if="profile.strengths.length" class="profile-choice-strengths">
            <span v-for="strength in profile.strengths" :key="strength">
              {{ learnerText(strength) }}
            </span>
          </span>
        </span>
        <span class="profile-choice-action">选择岗位 <span aria-hidden="true">→</span></span>
      </button>
    </div>

    <form
      v-else-if="session.awaiting === 'pretest'"
      class="pretest-form"
      @submit.prevent="submitPretest"
    >
      <header class="pretest-heading">
        <div>
          <span>岗前测评</span>
          <strong>{{ learnerText(session.profile.title) }}</strong>
          <p>一次只处理一道题，完成后生成个人训练重点。</p>
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

    <div v-else-if="advanceAction" class="live-action-block">
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
        v-if="session.interaction?.kind === 'learning_notice'"
        class="learning-notice"
        data-testid="learning-notice"
      >{{ learnerText(session.interaction.message) }}</p>
      <button
        type="button"
        class="primary-action"
        :aria-label="advanceAction"
        :disabled="busy"
        @click="advance"
      >{{ advanceAction }}</button>
    </div>

    <div
      v-else-if="session.awaiting === 'sql'"
      class="sql-workbench"
    >
      <header>
        <span>数据实操</span>
      </header>
      <article v-if="!operationOnly && activeTaskPrompt" class="sql-task-brief">
        <span>本题任务</span>
        <h3>{{ activeTaskPrompt }}</h3>
        <small v-if="activeTaskKnowledgePoint || activeTaskDifficulty">
          {{ [activeTaskKnowledgePoint, activeTaskDifficulty && `${activeTaskDifficulty}难度`].filter(Boolean).join(' · ') }}
        </small>
      </article>
      <section v-if="!operationOnly" class="sql-teacher-hint" aria-label="实操老师提示">
        <div>
          <strong>实操老师提示</strong>
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
      <p v-if="!operationOnly" class="sql-safety-note">
        查询输错时会留在本题并提示修改；系统只会读取数据，修改数据或越权语句会在进入数据库前被拦截。
      </p>
      <p
        v-if="session.interaction?.kind === 'learning_notice'"
        class="learning-notice"
        data-testid="learning-notice"
      >{{ learnerText(session.interaction.message) }}</p>
      <aside
        v-if="sqlFeedback"
        :class="sqlFeedback.event === 'sandbox_rejected' ? 'sandbox-rejection' : 'query-feedback'"
        :data-testid="sqlFeedback.event === 'sandbox_rejected' ? 'sandbox-rejection' : 'query-feedback'"
        role="alert"
      >
        <strong>{{ sqlFeedback.title }}</strong>
        <p>{{ sqlFeedback.learnerMessage }}</p>
      </aside>
      <aside
        v-if="session.sql_support"
        class="sql-progressive-support"
        :data-level="session.sql_support.level"
        aria-live="polite"
      >
        <strong>实操老师 · 第 {{ session.sql_support.attempt }} 次提示</strong>
        <p>{{ learnerText(session.sql_support.hint) }}</p>
      </aside>
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
        :disabled="busy || !sqlText.trim()"
        @click="submitSql"
      >运行查询</button>
    </div>

    <section
      v-else-if="
        session.awaiting === 'follow_up'
          && session.interaction?.kind === 'free_text_follow_up'
      "
      class="follow-up-workbench"
      aria-label="理解核对"
    >
      <header class="follow-up-heading">
        <div>
          <span>理解核对</span>
          <strong class="follow-up-round-label">
            <span>第 {{ session.interaction.round }} / 最多 {{ session.interaction.max_rounds }} 轮</span>
          </strong>
        </div>
        <p>结合刚才的数据，用自己的话说明判断依据。</p>
      </header>

      <article v-if="followUpTaskPrompt" class="follow-up-task-anchor">
        <div>
          <span>原始实操任务</span>
          <h3>{{ followUpTaskPrompt }}</h3>
        </div>
        <small v-if="followUpFocus">
          当前核对目标：{{ followUpFocus }}
        </small>
      </article>

      <section
        v-if="followUpTurns.length"
        class="follow-up-history-panel"
        :class="{ 'is-expanded': followUpHistoryExpanded }"
        aria-label="已完成的理解核对"
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
        <ol v-if="followUpHistoryExpanded" class="follow-up-history">
          <li v-for="turn in followUpTurns" :key="turn.round">
            <span>第 {{ turn.round }} 轮</span>
            <p class="follow-up-question">
              <b>导师提问</b>
              {{ learnerText(turn.question) }}
            </p>
            <p class="follow-up-answer">
              <b>你的回答</b>
              {{ learnerText(turn.answer) }}
            </p>
            <p v-if="turn.feedback" class="follow-up-feedback">
              <b>老师评价</b>
              {{ learnerText(turn.feedback) }}
            </p>
          </li>
        </ol>
        <article v-else-if="latestFollowUpTurn" class="follow-up-latest-summary">
          <p>
            <b>你的回答</b>
            {{ learnerText(latestFollowUpTurn.answer) }}
          </p>
          <p v-if="!operationOnly && (session.interaction.feedback || latestFollowUpTurn.feedback)">
            <b>老师评价</b>
            {{ learnerText(session.interaction.feedback || latestFollowUpTurn.feedback || '') }}
          </p>
          <small v-if="!operationOnly && session.interaction.next_step_reason">
            继续本知识点：{{ learnerText(session.interaction.next_step_reason) }}
          </small>
        </article>
      </section>

      <section
        v-else-if="!operationOnly && (session.interaction.feedback || session.interaction.next_step_reason)"
        class="answer-feedback-card is-compact"
        aria-live="polite"
      >
        <strong>老师评价</strong>
        <p v-if="session.interaction.feedback">{{ learnerText(session.interaction.feedback) }}</p>
        <small v-if="session.interaction.next_step_reason">
          继续本知识点：{{ learnerText(session.interaction.next_step_reason) }}
        </small>
      </section>

      <article class="follow-up-current">
        <span>这一轮</span>
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
        <small :class="{ 'needs-evidence': followUpIsVacuous }">
          {{ followUpIsVacuous ? '请用自己的话引用字段和值作答，不能只回答“是/否”，也不要回答“不会”或复述题目' : `${followUpInputLength} / 500 字` }}
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
      >正在根据你的回答准备并检查下一步提示…</p>
    </section>

    <div v-else-if="session.awaiting === 'done'" class="live-complete">
      <strong>{{ completionMessage }}</strong>
      <section v-if="session.training_report" class="training-report" aria-label="本轮训练报告">
        <header>
          <span>本轮训练报告</span>
          <h3>{{ learnerText(session.training_report.knowledge_point) }}</h3>
        </header>
        <div class="training-report-metrics">
          <article v-if="session.training_report.pretest_score">
            <span>岗前评测正确</span>
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
      </section>
      <section v-if="nextKnowledgePoint" class="training-report-next">
        <p>本知识点已经达标，下一知识点：{{ learnerText(nextKnowledgePoint) }}</p>
        <button
          type="button"
          class="primary-action live-next-action"
          aria-label="开始下一知识点"
          :disabled="busy"
          @click="continueLearning"
        >{{ busy ? '正在衔接…' : '开始下一知识点' }}</button>
      </section>
    </div>

    <p v-if="errorMessage" class="live-error" role="alert">{{ errorMessage }}</p>
  </section>
</template>
