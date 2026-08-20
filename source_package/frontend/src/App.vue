<script setup lang="ts">
import { CircleAlert, LoaderCircle } from '@lucide/vue'
import { computed, onMounted, ref, watch } from 'vue'

import knowledgeCatalog from 'virtual:knowledge-catalog'
import CollaborationWorkspace from './components/CollaborationWorkspace.vue'
import LearningPath from './components/LearningPath.vue'
import DataCollisionMoment from './components/DataCollisionMoment.vue'
import DebugWorkspace from './components/DebugWorkspace.vue'
import FloatingAgentAssistant from './components/FloatingAgentAssistant.vue'
import LivePractice from './components/LivePractice.vue'
import LearningRecords from './components/LearningRecords.vue'
import ProfileComparison from './components/ProfileComparison.vue'
import ProfilePanel from './components/ProfilePanel.vue'
import ReplayToolbar from './components/ReplayToolbar.vue'
import AuthGate from './components/AuthGate.vue'
import UserMenu from './components/UserMenu.vue'
import ResourcePanel from './components/ResourcePanel.vue'
import { useAgentEventPlayback } from './composables/useAgentEventPlayback'
import { useReplay, type ReplaySpeed } from './composables/useReplay'
import { buildTraceView, listKeyframes } from './lib/traceModel'
import { parseTraceJsonl } from './lib/traceParser'
import { parseImportedTrace, serializeTraceJsonl } from './lib/traceTransfer'
import type { InteractiveState } from './lib/interactiveApi'
import { AUTH_TOKEN_KEY, createAuthApi, type AuthRole, type AuthSession } from './lib/authApi'
import type { DataCollision, TraceDocument, TraceManifestEntry, TraceMessage } from './types/trace'


const documents = ref<TraceDocument[]>([])
const isDebugWorkspace = new URLSearchParams(window.location.search).get('view') === 'debug'

// ---- 登录态（身份层；与 ref-interactive-session 训练会话互不干扰） ----
const authApi = createAuthApi()
const authUser = ref<AuthSession | null>(null)
const authRestoring = ref(Boolean(sessionStorage.getItem(AUTH_TOKEN_KEY)))
async function restoreAuth(): Promise<void> {
  const token = sessionStorage.getItem(AUTH_TOKEN_KEY)
  if (!token) {
    authRestoring.value = false
    return
  }
  try {
    const profile = await authApi.me(token)
    authUser.value = {
      user_id: profile.user_id,
      username: profile.username,
      role: profile.role,
      token,
    }
  } catch {
    sessionStorage.removeItem(AUTH_TOKEN_KEY)
  } finally {
    authRestoring.value = false
  }
}
function onAuthenticated(session: AuthSession): void {
  authUser.value = session
  // 定稿：登录/注册进入系统一律先落在欢迎页——清掉上一账号遗留的
  // 训练会话恢复标记并复位面板（否则 LivePractice 会直接续训而非欢迎页）。
  sessionStorage.removeItem('ref-interactive-session')
  resetLiveState()
}
function onAuthLogout(): void {
  authUser.value = null
  // 登录态清除不影响正在进行的训练会话（ref-interactive-session 保留）。
}
const userRole = computed<AuthRole | null>(() => authUser.value?.role ?? null)
// 0819 bug7：跳过登录的游客模式（不再弹登录门）
const authSkipped = ref(false)
function onAuthSkip(): void {
  authSkipped.value = true
  // 定稿：跳过（游客）进入同样先落欢迎页
  sessionStorage.removeItem('ref-interactive-session')
  resetLiveState()
}
void restoreAuth()
const selectedFile = ref('')
const comparison = ref(false)
const entryMode = ref<'replay' | 'live'>(
  sessionStorage.getItem('ref-interactive-session') ? 'live' : 'replay',
)
// 优化20：非 admin（学员+游客跳过）一律固定实操通道——游客曾见管理入口。
// 登录态恢复期间（authRestoring）不强制：admin 恢复后保持默认回放入口。
watch(userRole, (role) => {
  if (authRestoring.value) return
  if (role !== 'admin' && entryMode.value !== 'live') entryMode.value = 'live'
}, { immediate: true })
// 0818 需求 5：学习记录页（头像下拉进入）
const recordsOpen = ref(false)
const viewMode = ref<'student' | 'collaboration'>('student')
const liveDocument = ref<TraceDocument>()
const liveState = ref<InteractiveState>()
const livePracticeRef = ref<InstanceType<typeof LivePractice>>()
type LiveTrainingLayout = 'assessment' | 'transition' | 'lesson' | 'practice' | 'report'
type LessonPageState = {
  index: number
  total: number
  kind?: 'metrics' | 'section' | 'evidence' | 'task'
  isLast: boolean
}
const liveLessonPage = ref<LessonPageState>({ index: 0, total: 0, isLast: false })
// 需求⑨：提问环节查阅讲义开关——追问态临时切回双栏展示完整微课
const lecturePeek = ref(false)
const autoChainActive = ref(false)
const practiceChainActive = ref(false)
const {
  events: liveAgentEvents,
  receive: receiveAgentEvent,
  reset: resetAgentEventPlayback,
} = useAgentEventPlayback()
const replayCollision = ref<DataCollision>()
const replayCollisionKey = ref('')
const dismissedReplayCollision = ref('')
const liveCollision = ref<DataCollision>()
const liveCollisionKey = ref('')
const dismissedLiveCollision = ref('')
const loading = ref(true)
const errorMessage = ref('')
const transferMessage = ref('')

const selectedDocument = computed(() => documents.value.find(
  (document) => document.fileName === selectedFile.value,
))
const total = computed(() => selectedDocument.value?.messages.length ?? 0)
const replay = useReplay(total)
const view = computed(() => selectedDocument.value
  ? buildTraceView(selectedDocument.value, replay.cursor.value)
  : undefined)
// 顶栏数据范围：从当前通道（live 实操 / 回放）的最新查询结果动态提取。

const liveView = computed(() => liveDocument.value
  ? buildTraceView(liveDocument.value, liveDocument.value.messages.length)
  : undefined)

function currentApprovedTaskArtifact(state: InteractiveState): TraceMessage | undefined {
  // The interactive service owns `artifact`: while awaiting SQL it points at
  // the approved task that the learner must execute.  Agent events and the
  // append-only trace can arrive one poll later, so the learner view needs a
  // narrow, read-only fallback instead of showing an empty/stale guide.
  if (state.awaiting !== 'sql') return undefined
  const raw = state.artifact
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return undefined
  const record = raw as Record<string, unknown>
  const payload = record.payload
  if (typeof payload !== 'object' || payload === null || Array.isArray(payload)) return undefined
  const payloadRecord = payload as Record<string, unknown>
  const content = payloadRecord.content
  if (typeof content !== 'object' || content === null || Array.isArray(content)) return undefined
  const contentRecord = content as Record<string, unknown>
  const payloadType = payloadRecord.type
  const prompt = contentRecord.question
    ?? contentRecord.contextualized_stem
    ?? contentRecord.standard_stem
  if (
    record.agent !== 'task'
    || record.role !== 'produce'
    || !['quiz_set', 'practice_guide'].includes(String(payloadType))
    || contentRecord.event !== 'product_ready'
    || typeof prompt !== 'string'
    || !prompt.trim()
    || typeof record.msg_id !== 'string'
    || typeof record.trace_id !== 'string'
    || record.trace_id !== state.trace_id
  ) return undefined

  return {
    msgId: record.msg_id,
    traceId: record.trace_id,
    step: typeof record.step === 'number' ? record.step : state.messages.length + 1,
    agent: 'task',
    role: 'produce',
    payloadType: String(payloadType),
    content: contentRecord,
    evidence: [],
    claims: [],
    timestamp: typeof record.timestamp === 'string' ? record.timestamp : '',
    rejectedByBus: false,
    busErrors: [],
    raw: record,
  }
}
watch(
  () => liveView.value?.lecture?.msgId,
  () => { liveLessonPage.value = { index: 0, total: 0, isLast: false } },
)
const liveLearnerView = computed(() => {
  const current = liveView.value
  const state = liveState.value
  if (!current || !state) return current

  const artifactTask = currentApprovedTaskArtifact(state)
  const currentWithArtifact = artifactTask
    ? {
        ...current,
        visibleMessages: current.visibleMessages.some(
          (message) => message.msgId === artifactTask.msgId,
        )
          ? current.visibleMessages
          : [...current.visibleMessages, artifactTask],
        task: artifactTask,
      }
    : current

  const approvedTaskIds = new Set(currentWithArtifact.visibleMessages.flatMap((message) => {
    const reviewedId = message.content.reviewed_msg_id
    const decision = message.verdict?.decision
    return message.payloadType === 'review_verdict'
      && typeof reviewedId === 'string'
      && (decision === 'approve' || decision === 'approve_with_fix')
      ? [reviewedId]
      : []
  }))
  const latestReviewedTask = [...currentWithArtifact.visibleMessages].reverse().find((message) => (
    (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
    && approvedTaskIds.has(message.msgId)
  ))

  if (
    state.awaiting !== 'follow_up'
    || state.interaction?.kind !== 'free_text_follow_up'
  ) {
    return state.awaiting === 'advance' && latestReviewedTask
      ? { ...currentWithArtifact, task: latestReviewedTask }
      : currentWithArtifact
  }

  const artifactId = typeof state.artifact?.msg_id === 'string'
    ? state.artifact.msg_id
    : undefined
  const prompt = state.interaction.prompt
  const approvedTask = [...currentWithArtifact.visibleMessages].reverse().find((message) => (
    (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
    && (!artifactId || message.msgId === artifactId)
    && message.content.question === prompt
  ))

  // The collaboration trace intentionally keeps rejected drafts for auditability.
  // Learners may only see the artifact approved for the current interaction.
  return { ...currentWithArtifact, task: approvedTask }
})
const liveHasResource = computed(() => Boolean(
  liveView.value?.lecture || liveView.value?.task || liveView.value?.sqlResult,
))
const liveLearnerHasResource = computed(() => Boolean(
  liveLearnerView.value?.lecture
  || liveLearnerView.value?.task
  || liveLearnerView.value?.sqlResult,
))
// T17 remediation and the claim gate keep the artifact pointer populated
// (control / lecture messages) while the previous sql_result message stays
// in the append-only trace.  During awaiting='advance' the result is valid
// only while the artifact IS the approved sql_result message.
const liveSqlResultStale = computed(() => {
  const state = liveState.value
  if (!state || state.awaiting !== 'advance') return false
  const artifact = state.artifact as { payload?: { type?: string } } | null | undefined
  return artifact?.payload?.type !== 'sql_result'
})
const liveTrainingLayout = computed<LiveTrainingLayout>(() => {
  const state = liveState.value
  // ②③：前测链锁 transition（中央 pending）；练习链锁 lesson（保持讲义页+正在进入按钮）
  if (autoChainActive.value && state && state.awaiting !== 'pretest' && state.awaiting !== 'done') {
    return 'transition'
  }
  if (practiceChainActive.value && state && state.awaiting !== 'pretest' && state.awaiting !== 'done') {
    return 'lesson'
  }
  if (!state || state.awaiting === 'pretest') return 'assessment'
  if (state.awaiting === 'done') return 'report'
  if (
    state.awaiting === 'sql'
    && liveLearnerView.value?.lecture
    && !liveLessonPage.value.isLast
  ) return 'lesson'
  if (state.awaiting === 'sql' || state.awaiting === 'follow_up') return 'practice'
  if (state.state === 'S2_KNOWLEDGE') return 'transition'
  if (
    state.state === 'S9_PATH_UPDATE'
    && liveView.value?.sqlResult
    && state.awaiting === 'advance'
  ) return 'practice'
  if (liveView.value?.task && liveLessonPage.value.kind === 'task') return 'practice'
  if (state.state === 'S3_TASK' && state.awaiting === 'advance' && liveLessonPage.value.isLast) {
    return 'practice'
  }
  if (liveView.value?.lecture) return 'lesson'
  return 'transition'
})
const liveWorkbenchTitle = computed(() => ({
  assessment: '岗前测评',
  transition: '训练准备',
  lesson: '岗位微课',
  practice: '实操工作台',
  report: '本轮训练报告',
})[liveTrainingLayout.value])
const liveWorkbenchStatus = computed(() => {
  // 需求③：微课阶段不再展示"学习进度 n/m"字样
  if (liveTrainingLayout.value === 'lesson') return ''
  // 需求③：实操阶段不再展示"指南与操作同步"状态字样
  if (liveTrainingLayout.value === 'practice') return ''
  if (liveTrainingLayout.value === 'assessment') return '专注完成诊断'
  if (liveTrainingLayout.value === 'report') return '训练已完成'
  return liveHasResource.value ? '内容已就绪' : '正在准备'
})
const liveFeedback = computed(() => liveState.value?.interaction?.feedback)
const liveNextStepReason = computed(() => liveState.value?.interaction?.next_step_reason)
const activeCollision = computed<DataCollision | undefined>(() => {
  if (entryMode.value === 'replay') return replayCollision.value
  return liveCollision.value
})
const collisionKey = computed(() => entryMode.value === 'live'
  ? liveCollisionKey.value
  : replayCollisionKey.value)

const traceOptions = computed(() => documents.value.map((document) => {
  const full = buildTraceView(document, document.messages.length)
  const title = full.profile?.title ?? '岗位培养会话'
  const suffix = isDebateReviewDocument(document, full) ? '辩论复审' : '完整会话'
  return { fileName: document.fileName, label: `${title} · ${suffix}` }
}))

const keyframes = computed(() => selectedDocument.value
  ? listKeyframes(selectedDocument.value)
  : [])

const comparisonEntries = computed(() => {
  const seen = new Set<string>()
  return documents.value.flatMap((document) => {
    const full = buildTraceView(document, document.messages.length)
    const profileId = document.profileId ?? ''
    if (!profileId || seen.has(profileId) || isDebateReviewDocument(document, full)) return []
    seen.add(profileId)
    // 当前会话卡跟随回放光标（展示连续学习过程），其余岗位定格各自会话
    // 终态作对照组 —— 三条会话只有一条在回放，其余无光标可同步。
    const isCurrent = document === selectedDocument.value
    return [{
      document,
      view: isCurrent ? buildTraceView(document, replay.cursor.value) : full,
      isCurrent,
    }]
  })
})

const canCompare = computed(() => comparisonEntries.value.length >= 3)
const exportDocument = computed(() => entryMode.value === 'live'
  ? liveDocument.value
  : selectedDocument.value)

/**
 * 是否为"辩论复审"专题会话（整段会话以复审为主体）。
 * 优化7：真实完整会话也可能包含少量辩论复审轮次（讲义/任务评审触发），
 * 只有辩论消息占比过半的专题会话才排除出三画像对比集。
 */
function isDebateReviewDocument(
  document: TraceDocument,
  full: ReturnType<typeof buildTraceView>,
): boolean {
  if (!full.debateGroups.length) return false
  const debateMessages = document.messages.filter((message) => (
    message.payloadType === 'rebuttal_case'
  )).length
  return debateMessages * 2 >= document.messages.length
}

/** 会话是否属于三画像对比集（带画像的基础岗会话；辩论复审专题/导入的不算）。 */
function participatesInComparison(document: TraceDocument | undefined): boolean {
  if (!document?.profileId) return false
  const full = buildTraceView(document, document.messages.length)
  return !isDebateReviewDocument(document, full)
}

function selectTrace(fileName: string): void {
  selectedFile.value = fileName
  const document = documents.value.find((item) => item.fileName === fileName)
  // 三画像下切换岗位会话：新会话仍在对比集内则停留在三画像（"正在回放"高亮
  // 卡随之切换到对应岗位）；切到对比集之外的会话（辩论复审/导入）才退回单画像。
  if (!participatesInComparison(document)) comparison.value = false
  replay.jumpTo(document?.messages.length ?? 0)
}

function changeComparison(value: boolean): void {
  if (value && !canCompare.value) return
  replay.pause()
  comparison.value = value
}

function changeSpeed(value: ReplaySpeed): void {
  replay.setSpeed(value)
}

function changeEntryMode(value: 'replay' | 'live'): void {
  // 优化20：回放仅 admin 可进（学员+游客跳过均固定实操通道）
  if (value === 'replay' && userRole.value !== 'admin') return
  replay.pause()
  comparison.value = false
  entryMode.value = value
}

function changeViewMode(value: 'student' | 'collaboration'): void {
  viewMode.value = value
}


async function autoAdvanceToPractice(): Promise<void> {
  const ref = livePracticeRef.value as { startPracticeChain?: () => Promise<void> } | null
  if (!ref?.startPracticeChain) return
  // ①③：整链由 LivePractice startPracticeChain 包裹 autoChainRunning——不闪中间界面
  await ref.startPracticeChain()
}

watch(() => liveState.value?.awaiting, () => {
  lecturePeek.value = false
})

function updateLiveState(state: InteractiveState): void {
  liveState.value = state
  sessionStorage.setItem('ref-interactive-trace', state.trace_id)
  const interaction = state.interaction
  if (interaction?.kind === 'data_collision') {
    const key = `${state.trace_id}-${state.messages.length}`
    if (key !== dismissedLiveCollision.value) {
      liveCollisionKey.value = key
      liveCollision.value = {
        misconception: interaction.misconception,
        wrongLabel: interaction.wrong_label,
        wrongValue: interaction.wrong_value,
        correctLabel: interaction.correct_label,
        correctValue: interaction.correct_value,
        step: state.messages.length,
      }
    }
  }
  if (!state.messages.length) return
  try {
    liveDocument.value = parseTraceJsonl(
      state.messages.map((message) => JSON.stringify(message)).join('\n'),
      `${state.trace_id}.jsonl`,
    )
  } catch {
    liveDocument.value = undefined
  }
}

function resetLiveState(): void {
  sessionStorage.removeItem('ref-interactive-trace')
  liveState.value = undefined
  liveDocument.value = undefined
  liveCollision.value = undefined
  liveCollisionKey.value = ''
  dismissedLiveCollision.value = ''
  liveLessonPage.value = { index: 0, total: 0, isLast: false }
  resetAgentEventPlayback()
}

function openDebugWorkspace(): void {
  if (!liveState.value) return
  sessionStorage.setItem('ref-interactive-session', liveState.value.session_id)
  sessionStorage.setItem('ref-interactive-trace', liveState.value.trace_id)
  const url = new URL(window.location.href)
  url.searchParams.set('view', 'debug')
  window.location.assign(`${url.pathname}${url.search}${url.hash}`)
}

async function importTrace(file: File): Promise<void> {
  transferMessage.value = ''
  try {
    const imported = await parseImportedTrace(file)
    documents.value = [
      ...documents.value.filter((document) => document.fileName !== imported.fileName),
      imported,
    ]
    entryMode.value = 'replay'
    selectTrace(imported.fileName)
    transferMessage.value = '会话记录已导入'
  } catch {
    transferMessage.value = '未能导入该会话记录。'
  }
}

function exportTrace(): void {
  const current = exportDocument.value
  if (!current) return
  transferMessage.value = ''
  try {
    const blob = new Blob([serializeTraceJsonl(current)], { type: 'application/x-ndjson' })
    const url = URL.createObjectURL(blob)
    const link = document.createElement('a')
    link.href = url
    link.download = current.fileName.toLowerCase().endsWith('.jsonl')
      ? current.fileName
      : `${current.traceId}.jsonl`
    document.body.appendChild(link)
    link.click()
    link.remove()
    URL.revokeObjectURL(url)
  } catch {
    transferMessage.value = '未能导出当前会话记录。'
  }
}

function completeCollision(): void {
  if (entryMode.value === 'replay') {
    dismissedReplayCollision.value = replayCollisionKey.value
    replayCollision.value = undefined
    return
  }
  dismissedLiveCollision.value = liveCollisionKey.value
  liveCollision.value = undefined
}

watch(
  () => ({
    fileName: selectedFile.value,
    cursor: replay.cursor.value,
    collision: view.value?.dataCollision,
  }),
  ({ fileName, collision }) => {
    if (!collision) {
      replayCollision.value = undefined
      replayCollisionKey.value = ''
      dismissedReplayCollision.value = ''
      return
    }
    const key = `${fileName}-${collision.step}`
    if (key === dismissedReplayCollision.value) return
    replay.pause()
    replayCollisionKey.value = key
    replayCollision.value = collision
  },
)

async function loadTraces(): Promise<void> {
  loading.value = true
  errorMessage.value = ''
  try {
    const manifestResponse = await fetch('/traces/manifest.json')
    if (!manifestResponse.ok) throw new Error('manifest')
    const manifestValue: unknown = await manifestResponse.json()
    if (!Array.isArray(manifestValue)) throw new Error('manifest')
    const manifest = manifestValue.filter(
      (entry): entry is TraceManifestEntry => typeof entry === 'object'
        && entry !== null
        && typeof (entry as TraceManifestEntry).fileName === 'string'
        && typeof (entry as TraceManifestEntry).bytes === 'number',
    )
    const loaded = await Promise.all(manifest.map(async (entry) => {
      const response = await fetch(`/traces/${encodeURIComponent(entry.fileName)}`)
      if (!response.ok) throw new Error('trace')
      return parseTraceJsonl(await response.text(), entry.fileName)
    }))
    if (!loaded.length) throw new Error('empty')
    documents.value = loaded
    selectTrace(loaded[0]?.fileName ?? '')
  } catch {
    errorMessage.value = '未能载入岗位培养记录，请确认本地记录完整后重试。'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  if (!isDebugWorkspace) void loadTraces()
})
</script>

<template>
  <div v-if="authRestoring" class="auth-restore-veil" aria-label="正在恢复登录状态">
    <LoaderCircle class="auth-restore-spin" :size="26" aria-hidden="true" />
  </div>
  <AuthGate v-else-if="!authUser && !authSkipped" @authenticated="onAuthenticated" @skip="onAuthSkip" />
  <DebugWorkspace v-else-if="isDebugWorkspace" />
  <!-- 优化27：学习记录=独立完整页面（不带顶栏），返回按钮回到训练 -->
  <main v-else-if="recordsOpen" class="records-standalone">
    <LearningRecords :role="userRole" @close="recordsOpen = false" />
  </main>
  <div
    v-else
    class="app-shell"
    :class="[
      `is-${entryMode}-entry`,
      `is-${viewMode}-mode`,
      { 'has-empty-live-session': entryMode === 'live' && !liveState },
      { 'has-live-session': entryMode === 'live' && Boolean(liveState) },
    ]"
  >
    <ReplayToolbar
      :traces="traceOptions"
      :auth-role="userRole"
      :selected-file="selectedFile"
      :playing="replay.playing.value"
      :cursor="replay.cursor.value"
      :total="total"
      :speed="replay.speed.value"
      :keyframes="keyframes"
      :comparison="comparison"
      :can-compare="canCompare"
      :entry-mode="entryMode"
      :view-mode="viewMode"
      :has-session="Boolean(liveState)"
      :view-disabled="entryMode === 'live' && liveState?.awaiting === 'pretest'"
      @select="selectTrace"
      @play="replay.play"
      @pause="replay.pause"
      @step="replay.step"
      @back="replay.stepBack"
      @restart="replay.restart"
      @speed="changeSpeed"
      @jump="replay.jumpTo"
      @comparison="changeComparison"
      @entry="changeEntryMode"
      @view="changeViewMode"
      @debug="openDebugWorkspace"
    >
      <template #user-menu>
        <UserMenu
          v-if="authUser"
          :username="authUser.username"
          :role="authUser.role"
          :can-export="Boolean(exportDocument)"
          @import-trace="importTrace"
          @export-trace="exportTrace"
          @open-records="recordsOpen = true"
          @logout="onAuthLogout"
        />
        <!-- 优化26：游客（跳过登录）在头像位显示"登录/注册"，一键回到登录门 -->
        <button
          v-else-if="authSkipped"
          type="button"
          class="guest-login-btn"
          aria-label="登录或注册"
          @click="authSkipped = false"
        >登录/注册</button>
      </template>
    </ReplayToolbar>

    <p v-if="transferMessage" class="trace-transfer-message" role="status">
      {{ transferMessage }}
    </p>

    <DataCollisionMoment
      v-if="activeCollision"
      :key="collisionKey"
      :misconception="activeCollision.misconception"
      :wrong-label="activeCollision.wrongLabel"
      :wrong-value="activeCollision.wrongValue"
      :correct-label="activeCollision.correctLabel"
      :correct-value="activeCollision.correctValue"
      @complete="completeCollision"
    />

    <section v-if="entryMode === 'replay' && loading" class="load-state" aria-live="polite">
      <LoaderCircle class="loading-icon" :size="24" aria-hidden="true" />
      <strong>正在载入</strong>
    </section>

    <section v-else-if="entryMode === 'replay' && errorMessage" class="load-state is-error" role="alert">
      <CircleAlert :size="24" aria-hidden="true" />
      <strong>回放记录暂不可用</strong>
      <span>{{ errorMessage }}</span>
    </section>

    <div
      v-else-if="entryMode === 'replay' && view"
      class="workspace-grid"
      :class="{
        'is-comparison': comparison,
        'is-student-view': viewMode === 'student',
        'is-collaboration-view': viewMode === 'collaboration',
      }"
    >
      <ProfileComparison
        v-if="comparison"
        class="comparison-stage"
        :entries="comparisonEntries"
        :catalog="knowledgeCatalog"
      />
      <template v-else>
        <ProfilePanel :view="view" :catalog="knowledgeCatalog" :profile-id="selectedDocument?.profileId" />
        <ResourcePanel :view="view" />
        <CollaborationWorkspace
          v-if="viewMode === 'collaboration'"
          :view="view"
        />
      </template>
      <LearningPath v-if="!comparison" :view="view" :catalog="knowledgeCatalog" />
    </div>

    <main
      v-else
      class="live-page-shell"
      :class="[
        liveState ? 'workspace-grid live-workspace' : 'role-selection-page',
        {
          'is-student-view': viewMode === 'student',
          'is-collaboration-view': viewMode === 'collaboration',
          'is-live-booting': Boolean(liveState && !liveView),
          'is-pretest-focus': Boolean(
            liveState
              && (liveState.awaiting === 'pretest'
                || liveState.awaiting === 'diagnostic_probe'),
          ),
        },
      ]"
      :aria-label="liveState ? '岗位训练工作台' : '选择岗位训练路径'"
    >
        <ProfilePanel
          v-if="liveView && liveState?.awaiting !== 'pretest' && liveState?.awaiting !== 'diagnostic_probe'"
          :view="liveView"
          :catalog="knowledgeCatalog"
          :current-difficulty="liveState?.current_difficulty"
          :profile-id="liveState?.profile?.profile_id"
        />
      <div
        id="collaboration-learner-workspace"
        class="live-training-stage"
        :class="{
          'is-empty': !liveView,
        }"
      >
        <header v-if="liveState" class="training-workbench-heading">
          <div>
            <span class="section-kicker">
              {{ viewMode === 'collaboration' ? '协同操作区 · 当前学习阶段' : '当前学习阶段' }}
            </span>
            <strong>{{ liveWorkbenchTitle }}</strong>
            <small v-if="viewMode === 'collaboration'" class="collaboration-operation-note">
              与上方拓扑使用同一会话，本区操作会实时触发 Agent
            </small>
          </div>
          <div class="training-workbench-actions">
            <span
              v-if="liveState && liveWorkbenchStatus && liveState.awaiting !== 'pretest' && liveState.awaiting !== 'diagnostic_probe'"
              class="training-workbench-status"
            >
              {{ liveWorkbenchStatus }}
            </span>
            <a
              v-if="viewMode === 'collaboration'"
              class="return-to-topology"
              href="#collaboration-topology-workspace"
            >返回拓扑 ↑</a>
            <button
              v-if="liveState && (liveState.awaiting === 'pretest' || liveState.awaiting === 'diagnostic_probe')"
              type="button"
              class="restart-training"
              aria-label="返回选择训练关注点"
              @click="livePracticeRef?.backToFocusSelection()"
            >返回</button>
            <button
              v-else
              type="button"
              class="restart-training"
              aria-label="重新选择岗位"
              @click="livePracticeRef?.resetSession()"
            >重新选择岗位</button>
          </div>
        </header>
        <div
          class="training-workbench-body"
          :class="{
            'has-learning-resource': liveHasResource,
            'is-profile-selection': !liveState,
            [`is-${liveTrainingLayout}-layout`]: Boolean(liveState),
            'is-followup-focus': Boolean(liveState && liveState.awaiting === 'follow_up' && !lecturePeek),
            }"
        >
          <LivePractice
            ref="livePracticeRef"
            :class="{ 'training-task-station': Boolean(liveState) }"
            :style="{
              display: liveState && liveTrainingLayout === 'lesson' ? 'none' : 'grid',
            }"
            :sql-result="liveView?.sqlResult"
            :operation-only="Boolean(liveState)"
            :lecture-peek-open="lecturePeek"
            @state="updateLiveState"
            @lecture-peek="lecturePeek = $event"
            @auto-chain="autoChainActive = $event"
            @practice-chain="practiceChainActive = $event"
            @agent-event="receiveAgentEvent"
            @reset="resetLiveState"
          />
          <aside
            v-if="liveState"
            v-show="liveTrainingLayout === 'lesson' || liveTrainingLayout === 'practice' || lecturePeek"
            class="training-lesson-station"
            aria-label="学习与实操指南"
          >
            <!-- 需求③：资源就绪条（实操与测验已准备 3/3）板块已删除 -->
            <ResourcePanel
              v-if="liveLearnerView && liveLearnerHasResource"
              :view="liveLearnerView"
              lesson-pager
              live-operation
              :auto-jump-to-task="false"
              :task-claimed="!(liveState?.state === 'S3_TASK' && liveState?.awaiting === 'advance')"
              :peek-lecture="lecturePeek"
              :chain-running="autoChainActive || practiceChainActive"
              :practice-chain-active="practiceChainActive"
              :data-present-mode="liveState?.profile?.practice_mode === 'data_present'"
              :guidance-feedback="liveFeedback"
              :guidance-next-step-reason="liveNextStepReason"
              :sql-result-stale="liveSqlResultStale"
              @page-state="liveLessonPage = $event"
              @start-practice="autoAdvanceToPractice"
            />
            <section v-else class="lesson-station-placeholder">
              <span>微课</span>
              <strong>完成岗前诊断后生成</strong>
              <p>微课会常驻在这里，并按知识卡片分页展示。</p>
            </section>
          </aside>
        </div>
      </div>
      <CollaborationWorkspace
        v-if="viewMode === 'collaboration' && liveView"
        id="collaboration-topology-workspace"
        :view="liveView"
        :events="liveAgentEvents"
        :contract="liveState?.learning_contract ?? undefined"
        :evidence-bundle="liveState?.evidence_bundle ?? undefined"
        :resource-bundle="liveState?.resource_bundle ?? undefined"
        :coordination-evidence="liveState?.coordination_evidence"
        learner-workspace-target="#collaboration-learner-workspace"
      />
      <FloatingAgentAssistant
        v-if="entryMode === 'live' && viewMode === 'student' && liveView"
        :view="liveView"
        :events="liveAgentEvents"
      />
    </main>
  </div>
</template>
