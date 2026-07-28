<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
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
}>(), {
  pollIntervalMs: 1500,
})

const emit = defineEmits<{
  state: [value: InteractiveState]
  agentEvent: [value: AgentActivityEvent]
  reset: []
}>()

const api = props.api ?? createInteractiveApi()
const session = ref<InteractiveState>()
const questions = ref<InteractivePretestQuestion[]>([])
const answers = ref<Record<string, string>>({})
const sqlText = ref('')
const followUpText = ref('')
const followUpClientTurnId = ref<string>()
const followUpSubmittedText = ref('')
const busy = ref(false)
const errorMessage = ref('')
let pollTimer: number | undefined
let closeEventStream: (() => void) | undefined
let eventSessionId = ''

const pretestComplete = computed(() => questions.value.length === 5
  && questions.value.every((question) => answers.value[question.question_id]))

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
    && value.interaction?.kind === 'review_notice'
  ) {
    return learnerText(value.interaction.message)
  }
  return '训练完成'
})

const nextKnowledgePoint = computed(() => {
  const interaction = session.value?.interaction
  if (
    session.value?.awaiting !== 'done'
    || session.value.outcome !== 'completed'
    || interaction?.kind !== 'next_learning_step'
  ) return undefined
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
const followUpInputLength = computed(
  () => followUpText.value.trim().normalize('NFKC').length,
)
const canSubmitFollowUp = computed(
  () => followUpInputLength.value >= 2 && followUpInputLength.value <= 500,
)

function applyState(value: InteractiveState): void {
  session.value = value
  emit('state', value)
}

function connectAgentEvents(sessionId: string): void {
  if (!api.subscribeAgentEvents || eventSessionId === sessionId) return
  closeEventStream?.()
  eventSessionId = sessionId
  closeEventStream = api.subscribeAgentEvents(sessionId, (event) => {
    emit('agentEvent', event)
  })
}

async function selectProfile(profileId: string): Promise<void> {
  if (busy.value) return
  busy.value = true
  errorMessage.value = ''
  try {
    const created = await api.createSession(profileId)
    sessionStorage.setItem('ref-interactive-session', created.session_id)
    connectAgentEvents(created.session_id)
    applyState(created)
    questions.value = await api.getPretest(created.session_id)
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
  busy.value = true
  errorMessage.value = ''
  try {
    applyState(await api.advance(session.value.session_id))
  } catch (error) {
    errorMessage.value = publicRequestError(error, '当前步骤暂时无法继续。')
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
    sessionStorage.setItem('ref-interactive-session', continued.session_id)
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
    errorMessage.value = publicRequestError(error, '本轮判断暂时无法提交。')
  } finally {
    busy.value = false
  }
}

async function pollState(sessionId: string): Promise<void> {
  try {
    const value = await api.getState(sessionId)
    applyState(value)
    if (value.awaiting === 'pretest' && !questions.value.length) {
      questions.value = await api.getPretest(sessionId)
    }
  } catch (error) {
    sessionStorage.removeItem('ref-interactive-session')
    session.value = undefined
    errorMessage.value = publicRequestError(error, '未能恢复上次训练。')
  }
}

function resetSession(): void {
  closeEventStream?.()
  closeEventStream = undefined
  eventSessionId = ''
  sessionStorage.removeItem('ref-interactive-session')
  session.value = undefined
  questions.value = []
  answers.value = {}
  sqlText.value = ''
  followUpText.value = ''
  followUpSubmittedText.value = ''
  followUpClientTurnId.value = undefined
  busy.value = false
  errorMessage.value = ''
  emit('reset')
}

onMounted(async () => {
  const storedSession = sessionStorage.getItem('ref-interactive-session')
  if (storedSession) {
    connectAgentEvents(storedSession)
    await pollState(storedSession)
  }
  if (props.pollIntervalMs > 0) {
    pollTimer = window.setInterval(() => {
      const value = session.value
      if (!busy.value && value && value.awaiting !== 'done') {
        void pollState(value.session_id)
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
  <section class="live-practice panel" aria-label="岗位实操通道">
    <header class="live-practice-heading">
      <h2>实操</h2>
      <button
        v-if="session"
        type="button"
        class="restart-training"
        aria-label="重新开始训练"
        :disabled="busy"
        @click="resetSession"
      >重新开始</button>
    </header>

    <div v-if="!session" class="profile-choice-grid">
      <button
        v-for="profile in profiles"
        :key="profile.id"
        type="button"
        class="profile-choice"
        :aria-label="`选择${learnerText(profile.title)}`"
        :disabled="busy"
        @click="selectProfile(profile.id)"
      >
        <strong>{{ learnerText(profile.title) }}</strong>
        <small>{{ learnerText(profile.background) }}</small>
      </button>
    </div>

    <form
      v-else-if="session.awaiting === 'pretest'"
      class="pretest-form"
      @submit.prevent="submitPretest"
    >
      <header>
        <span>岗前测评</span>
        <strong>{{ learnerText(session.profile.title) }}</strong>
        <p>逐题选择你认为正确的答案，完成后生成个人训练重点。</p>
      </header>
      <fieldset
        v-for="(question, index) in questions"
        :key="question.question_id"
        class="pretest-question"
      >
        <legend><span>{{ index + 1 }}</span>{{ learnerText(question.stem) }}</legend>
        <label v-for="(label, option) in question.options" :key="option">
          <input
            v-model="answers[question.question_id]"
            type="radio"
            :name="question.question_id"
            :value="option"
          />
          <span class="option-letter">{{ option }}</span>
          <span class="option-copy">{{ learnerText(label) }}</span>
        </label>
      </fieldset>
      <button class="primary-action" type="submit" :disabled="busy || !pretestComplete">
        提交岗前测评
      </button>
    </form>

    <div v-else-if="advanceAction" class="live-action-block">
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
          <strong>
            第 {{ session.interaction.round }} / 最多
            {{ session.interaction.max_rounds }} 轮
          </strong>
        </div>
        <p>结合刚才的数据，用自己的话说明判断依据。</p>
      </header>

      <ol
        v-if="session.interaction.turns.length"
        class="follow-up-history"
        aria-label="已完成的理解核对"
      >
        <li
          v-for="turn in session.interaction.turns"
          :key="turn.round"
        >
          <span>第 {{ turn.round }} 轮</span>
          <p class="follow-up-question">
            <b>导师提问</b>
            {{ learnerText(turn.question) }}
          </p>
          <p class="follow-up-answer">
            <b>你的回答</b>
            {{ learnerText(turn.answer) }}
          </p>
        </li>
      </ol>

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
          placeholder="例如：我会以实际发生的数据判断完成情况，因为……"
          :disabled="busy"
          @input="updateFollowUpDraft"
          @keydown.ctrl.enter.prevent="submitFollowUp"
          @keydown.meta.enter.prevent="submitFollowUp"
        ></textarea>
      </label>
      <footer class="follow-up-actions">
        <small>{{ followUpInputLength }} / 500 字</small>
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
      <template v-if="nextKnowledgePoint">
        <p>本知识点已经达标，下一知识点：{{ learnerText(nextKnowledgePoint) }}</p>
        <button
          type="button"
          class="primary-action live-next-action"
          aria-label="开始下一知识点"
          :disabled="busy"
          @click="continueLearning"
        >{{ busy ? '正在衔接…' : '开始下一知识点' }}</button>
      </template>
    </div>

    <p v-if="errorMessage" class="live-error" role="alert">{{ errorMessage }}</p>
  </section>
</template>
