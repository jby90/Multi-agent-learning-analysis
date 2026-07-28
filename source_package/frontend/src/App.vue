<script setup lang="ts">
import { CircleAlert, LoaderCircle } from '@lucide/vue'
import { computed, onMounted, ref, watch } from 'vue'

import knowledgeCatalog from 'virtual:knowledge-catalog'
import AgentStage from './components/AgentStage.vue'
import LearningPath from './components/LearningPath.vue'
import DataCollisionMoment from './components/DataCollisionMoment.vue'
import LivePractice from './components/LivePractice.vue'
import LiveStepGuide from './components/LiveStepGuide.vue'
import ProfileComparison from './components/ProfileComparison.vue'
import ProfilePanel from './components/ProfilePanel.vue'
import ReplayToolbar from './components/ReplayToolbar.vue'
import ResourcePanel from './components/ResourcePanel.vue'
import TracePanel from './components/TracePanel.vue'
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
const {
  events: liveAgentEvents,
  receive: receiveAgentEvent,
  reset: resetAgentEventPlayback,
} = useAgentEventPlayback()
const replayCollision = ref<DataCollision>()
const replayCollisionKey = ref('')
const dismissedReplayCollision = ref('')
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
const activeCollision = computed<DataCollision | undefined>(() => {
  if (entryMode.value === 'replay') return replayCollision.value
  const interaction = liveState.value?.interaction
  if (interaction?.kind !== 'data_collision') return undefined
  return {
    misconception: interaction.misconception,
    wrongLabel: interaction.wrong_label,
    wrongValue: interaction.wrong_value,
    correctLabel: interaction.correct_label,
    correctValue: interaction.correct_value,
    step: liveState.value?.messages.length ?? 0,
  }
})
const collisionKey = computed(() => entryMode.value === 'live'
  ? `${liveState.value?.trace_id ?? 'live'}-${activeCollision.value?.step ?? 0}`
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
  if (entryMode.value !== 'replay') return
  dismissedReplayCollision.value = replayCollisionKey.value
  replayCollision.value = undefined
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
  <div class="app-shell">
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
        <div v-if="viewMode === 'collaboration'" class="collaboration-observatory">
          <AgentStage :view="view" />
          <TracePanel :view="view" />
        </div>
      </template>
      <LearningPath v-if="!comparison" :view="view" :catalog="knowledgeCatalog" />
    </div>

    <div
      v-else
      class="workspace-grid live-workspace"
      :class="{
        'is-student-view': viewMode === 'student',
        'is-collaboration-view': viewMode === 'collaboration',
        'is-live-empty': !liveView,
      }"
    >
      <ProfilePanel v-if="liveView" :view="liveView" :catalog="knowledgeCatalog" />
      <div
        class="live-training-stage"
        :class="{
          'is-empty': !liveView,
          'has-step-guide': Boolean(liveView && !liveHasResource),
        }"
      >
        <LiveStepGuide
          v-if="liveView && !liveHasResource"
          :state="liveState"
        />
        <ResourcePanel
          v-if="liveView && liveHasResource"
          :view="liveView"
        />
        <LivePractice
          @state="updateLiveState"
          @agent-event="receiveAgentEvent"
          @reset="resetLiveState"
        />
      </div>
      <div
        v-if="viewMode === 'collaboration' && liveView"
        class="collaboration-observatory"
      >
        <AgentStage
          :view="liveView"
          :events="liveAgentEvents"
          :contract="liveState?.learning_contract ?? undefined"
          :evidence-bundle="liveState?.evidence_bundle ?? undefined"
        />
        <TracePanel :view="liveView" />
      </div>
      <LearningPath v-if="liveView" :view="liveView" :catalog="knowledgeCatalog" />
    </div>
  </div>
</template>
