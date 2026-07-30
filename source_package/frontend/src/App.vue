<script setup lang="ts">
import { CircleAlert, LoaderCircle } from '@lucide/vue'
import { computed, onMounted, ref, watch } from 'vue'

import knowledgeCatalog from 'virtual:knowledge-catalog'
import CollaborationWorkspace from './components/CollaborationWorkspace.vue'
import LearningPath from './components/LearningPath.vue'
import DataCollisionMoment from './components/DataCollisionMoment.vue'
import FloatingAgentAssistant from './components/FloatingAgentAssistant.vue'
import LivePractice from './components/LivePractice.vue'
import ProfileComparison from './components/ProfileComparison.vue'
import ProfilePanel from './components/ProfilePanel.vue'
import ReplayToolbar from './components/ReplayToolbar.vue'
import ResourcePanel from './components/ResourcePanel.vue'
import ResourceBundleStrip from './components/ResourceBundleStrip.vue'
import { useAgentEventPlayback } from './composables/useAgentEventPlayback'
import { useReplay, type ReplaySpeed } from './composables/useReplay'
import { buildTraceView, listKeyframes } from './lib/traceModel'
import { parseTraceJsonl } from './lib/traceParser'
import { parseImportedTrace, serializeTraceJsonl } from './lib/traceTransfer'
import type { InteractiveState } from './lib/interactiveApi'
import type { DataCollision, TraceDocument, TraceManifestEntry } from './types/trace'


const documents = ref<TraceDocument[]>([])
const selectedFile = ref('')
const comparison = ref(false)
const entryMode = ref<'replay' | 'live'>(
  sessionStorage.getItem('ref-interactive-session') ? 'live' : 'replay',
)
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
const liveView = computed(() => liveDocument.value
  ? buildTraceView(liveDocument.value, liveDocument.value.messages.length)
  : undefined)
const liveHasResource = computed(() => Boolean(
  liveView.value?.lecture || liveView.value?.task || liveView.value?.sqlResult,
))
const liveTrainingLayout = computed<LiveTrainingLayout>(() => {
  const state = liveState.value
  if (!state || state.awaiting === 'pretest') return 'assessment'
  if (state.awaiting === 'done') return 'report'
  if (state.awaiting === 'sql' || state.awaiting === 'follow_up') return 'practice'
  if (state.state === 'S2_KNOWLEDGE') return 'transition'
  if (liveView.value?.task && liveLessonPage.value.kind === 'task') return 'practice'
  if (state.state === 'S3_TASK' && state.awaiting === 'advance' && liveLessonPage.value.isLast) {
    return 'practice'
  }
  if (liveView.value?.lecture) return 'lesson'
  return 'transition'
})
const liveWorkbenchTitle = computed(() => ({
  assessment: '岗前评测',
  transition: '训练准备',
  lesson: '岗位微课',
  practice: '实操工作台',
  report: '本轮训练报告',
})[liveTrainingLayout.value])
const liveWorkbenchStatus = computed(() => {
  if (liveTrainingLayout.value === 'lesson') {
    const page = liveLessonPage.value
    return page.total ? `学习进度 ${page.index + 1}/${page.total}` : '微课已就绪'
  }
  if (liveTrainingLayout.value === 'practice') return '指南与操作同步'
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
  const suffix = full.debateGroups.length ? '辩论复审' : '完整会话'
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
    if (!profileId || seen.has(profileId) || full.debateGroups.length) return []
    seen.add(profileId)
    return [{ document, view: full }]
  })
})

const canCompare = computed(() => comparisonEntries.value.length >= 3)
const exportDocument = computed(() => entryMode.value === 'live'
  ? liveDocument.value
  : selectedDocument.value)

function selectTrace(fileName: string): void {
  selectedFile.value = fileName
  comparison.value = false
  const document = documents.value.find((item) => item.fileName === fileName)
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
  replay.pause()
  comparison.value = false
  entryMode.value = value
}

function changeViewMode(value: 'student' | 'collaboration'): void {
  viewMode.value = value
}

function updateLiveState(state: InteractiveState): void {
  liveState.value = state
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
  liveState.value = undefined
  liveDocument.value = undefined
  liveCollision.value = undefined
  liveCollisionKey.value = ''
  dismissedLiveCollision.value = ''
  liveLessonPage.value = { index: 0, total: 0, isLast: false }
  resetAgentEventPlayback()
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

onMounted(loadTraces)
</script>

<template>
  <div
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
      :can-export="Boolean(exportDocument)"
      :has-session="Boolean(liveState)"
      @select="selectTrace"
      @play="replay.play"
      @pause="replay.pause"
      @step="replay.step"
      @speed="changeSpeed"
      @jump="replay.jumpTo"
      @comparison="changeComparison"
      @entry="changeEntryMode"
      @view="changeViewMode"
      @import="importTrace"
      @export="exportTrace"
    />

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
        <ProfilePanel :view="view" :catalog="knowledgeCatalog" />
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
        },
      ]"
      :aria-label="liveState ? '岗位训练工作台' : '选择岗位训练路径'"
    >
      <ProfilePanel v-if="liveView" :view="liveView" :catalog="knowledgeCatalog" />
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
            <span class="training-workbench-status">
              {{ liveWorkbenchStatus }}
            </span>
            <a
              v-if="viewMode === 'collaboration'"
              class="return-to-topology"
              href="#collaboration-topology-workspace"
            >返回拓扑 ↑</a>
            <button
              type="button"
              class="restart-training"
              aria-label="重新开始训练"
              @click="livePracticeRef?.resetSession()"
            >重新开始</button>
          </div>
        </header>
        <div
          class="training-workbench-body"
          :class="{
            'has-learning-resource': liveHasResource,
            'is-profile-selection': !liveState,
            [`is-${liveTrainingLayout}-layout`]: Boolean(liveState),
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
            @state="updateLiveState"
            @agent-event="receiveAgentEvent"
            @reset="resetLiveState"
          />
          <aside
            v-if="liveState"
            v-show="liveTrainingLayout === 'lesson' || liveTrainingLayout === 'practice'"
            class="training-lesson-station"
            aria-label="学习与实操指南"
          >
            <ResourceBundleStrip
              v-if="liveState.resource_bundle"
              :bundle="liveState.resource_bundle"
            />
            <ResourcePanel
              v-if="liveView && liveHasResource"
              :view="liveView"
              lesson-pager
              :guidance-feedback="liveFeedback"
              :guidance-next-step-reason="liveNextStepReason"
              @page-state="liveLessonPage = $event"
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
      <LearningPath v-if="liveView" :view="liveView" :catalog="knowledgeCatalog" />
      <FloatingAgentAssistant
        v-if="viewMode === 'student' && liveView"
        :view="liveView"
        :events="liveAgentEvents"
      />
    </main>
  </div>
</template>
