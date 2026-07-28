<script setup lang="ts">
import { Check, CircleAlert, Pause, Play, Radio, Sparkles } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import type {
  AgentActivityEvent,
  AgentActivityId,
  AgentActivityStatus,
  InteractiveLearningContract,
} from '../lib/interactiveApi'
import { agentLabel, agentPurpose } from '../lib/tracePresentation'
import type { StateId, TraceView } from '../types/trace'
import AgentTeacherAvatar from './AgentTeacherAvatar.vue'


const props = withDefaults(defineProps<{
  contract?: InteractiveLearningContract
  events?: AgentActivityEvent[]
  view: TraceView
}>(), {
  contract: undefined,
  events: () => [],
})

const motionEnabled = ref(true)
const motionProgress = ref(0)
const flowPercent = computed(() => String(Math.round(motionProgress.value * 100)).padStart(2, '0'))
const reducedMotion = ref(false)
let motionTimer: number | undefined
let previousTick = 0

type PrimaryAgentActivityId = Exclude<AgentActivityId, 'evidence_review' | 'pedagogy_review'>

type AgentCard = {
  id: PrimaryAgentActivityId
  status: AgentActivityStatus
  label: string
  peers: AgentActivityId[]
}

const agentRoutes = [
  { id: 'diagnosis', path: 'M129 103 Q235 120 330 185', code: 'DIAG', sx: 17, sy: 24 },
  { id: 'knowledge', path: 'M380 56 Q380 105 380 150', code: 'KNOW', sx: 50, sy: 13 },
  { id: 'review', path: 'M631 103 Q525 120 430 185', code: 'AUDIT', sx: 83, sy: 24 },
  { id: 'verification', path: 'M570 335 Q500 292 430 250', code: 'VERIFY', sx: 75, sy: 78 },
  { id: 'task', path: 'M190 335 Q260 292 330 250', code: 'TASK', sx: 25, sy: 78 },
] as const satisfies readonly { id: AgentActivityId; path: string; code: string; sx: number; sy: number }[]

const agentOrder = [
  'diagnosis',
  'knowledge',
  'review',
  'verification',
  'task',
] as const satisfies readonly PrimaryAgentActivityId[]

const stateOwners: Partial<Record<StateId, PrimaryAgentActivityId>> = {
  S1_DIAGNOSIS: 'diagnosis',
  S2_KNOWLEDGE: 'knowledge',
  S3_TASK: 'task',
  S4_VERIFY: 'verification',
  S5_REVIEW: 'review',
  S6_DEBATE: 'review',
  S8_PROBE: 'task',
}

const latestEvents = computed(() => {
  const latest = new Map<AgentActivityId, AgentActivityEvent>()
  for (const event of props.events) latest.set(event.agent, event)
  return latest
})

const fallbackActive = computed<PrimaryAgentActivityId | undefined>(() => {
  if (['S10_DONE', 'S_FAIL'].includes(props.view.currentState)) return undefined
  const latestMessage = [...props.view.visibleMessages]
    .reverse()
    .find((message) => message.agent !== 'system')
  if (latestMessage && latestMessage.agent !== 'system') return latestMessage.agent
  return stateOwners[props.view.currentState as StateId]
})

const agents = computed<AgentCard[]>(() => agentOrder.map((id) => {
  const event = latestEvents.value.get(id)
  if (event) return { id, status: event.status, label: event.label, peers: event.peers }
  const seen = props.view.visibleMessages.some((message) => message.agent === id)
  return {
    id,
    status: fallbackActive.value === id ? 'working' : seen ? 'done' : 'idle',
    label: fallbackActive.value === id ? '正在执行当前阶段' : seen ? '已完成接力' : '等待调度',
    peers: [],
  }
}))

const latestEvent = computed(() => props.events.at(-1))
const headline = computed(() => latestEvent.value?.label ?? (
  fallbackActive.value
    ? `${agentLabel(fallbackActive.value)}正在执行当前阶段`
    : '等待工作流启动'
))
const activeCount = computed(() => agents.value.filter(
  (agent) => ['working', 'collaborating', 'reviewing', 'debating'].includes(agent.status),
).length)
const approvedCount = computed(() => agents.value.filter(
  (agent) => ['approved', 'done'].includes(agent.status),
).length)
const reviewFanOut = computed(() => {
  const event = [...props.events].reverse().find(
    (item) => item.activity === 'parallel_quality_review'
      && typeof item.details?.fan_out === 'number',
  )
  return typeof event?.details?.fan_out === 'number'
    ? event.details.fan_out
    : undefined
})

type ReviewBranchProof = {
  agentId: 'evidence_review' | 'pedagogy_review'
  ruleId: 'R-02' | 'R-03'
  axisLabel: string
  label: string
  status: 'running' | 'succeeded' | 'failed'
  elapsedMs?: number
}

type ParallelReviewProof = {
  artifactId?: string
  contractId?: string
  correlationId?: string
  elapsedMs?: number
  isComplete: boolean
  branches: ReviewBranchProof[]
  savedMs?: number
}

const branchLabels: Record<ReviewBranchProof['ruleId'], string> = {
  'R-02': '事实与证据核验',
  'R-03': '难度与岗位适配',
}

const specialistDefinitions = {
  evidence_review: {
    ruleId: 'R-02',
    label: '证据审核 Agent',
  },
  pedagogy_review: {
    ruleId: 'R-03',
    label: '教学适配 Agent',
  },
} as const

function specialistAgentId(value: unknown): ReviewBranchProof['agentId'] | undefined {
  if (value === 'evidence_review' || value === 'R-02') return 'evidence_review'
  if (value === 'pedagogy_review' || value === 'R-03') return 'pedagogy_review'
  return undefined
}

function eventAgentLabel(agent: AgentActivityId): string {
  if (agent === 'evidence_review') return specialistDefinitions.evidence_review.label
  if (agent === 'pedagogy_review') return specialistDefinitions.pedagogy_review.label
  return agentLabel(agent)
}

function detailString(details: Record<string, unknown> | undefined, key: string): string | undefined {
  const value = details?.[key]
  return typeof value === 'string' && value.trim() ? value : undefined
}

function detailNumber(details: Record<string, unknown> | undefined, key: string): number | undefined {
  const value = details?.[key]
  return typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : undefined
}

function shortId(value: string | undefined): string {
  if (!value) return '等待生成'
  return value.length > 18 ? `${value.slice(0, 10)}…${value.slice(-6)}` : value
}

const parallelReviewProof = computed<ParallelReviewProof | undefined>(() => {
  const events = props.events.filter((item) => item.activity === 'parallel_quality_review')
  const latest = events.at(-1)
  if (!latest) return undefined
  const details = latest.details
  const aggregation = detailString(details, 'aggregation')
  const isComplete = aggregation === 'deterministic'
  const rawBranches = Array.isArray(details?.branches) ? details.branches : []
  const completedBranches = new Map<ReviewBranchProof['agentId'], { status: 'succeeded' | 'failed'; elapsedMs?: number }>()
  for (const raw of rawBranches) {
    if (!raw || typeof raw !== 'object') continue
    const branch = raw as Record<string, unknown>
    const branchId = specialistAgentId(branch.branch_id)
    const status = branch.status
    if (
      branchId
      && (status === 'succeeded' || status === 'failed')
    ) {
      completedBranches.set(branchId, {
        status,
        elapsedMs: typeof branch.elapsed_ms === 'number' ? branch.elapsed_ms : undefined,
      })
    }
  }
  const started = [...events].reverse().find((item) => (
    detailString(item.details, 'aggregation') === 'pending'
    && detailString(item.details, 'artifact_id') === detailString(details, 'artifact_id')
  ))
  const elapsedMs = isComplete
    ? detailNumber(details, 'parallel_elapsed_ms')
    : undefined
  const branches = (['evidence_review', 'pedagogy_review'] as const).map((agentId): ReviewBranchProof => {
    const completed = completedBranches.get(agentId)
    const specialistEvent = latestEvents.value.get(agentId)
    const liveStatus = specialistEvent?.status === 'blocked'
      ? 'failed'
      : specialistEvent?.status === 'done' ? 'succeeded' : 'running'
    const definition = specialistDefinitions[agentId]
    return {
      agentId,
      ruleId: definition.ruleId,
      label: definition.label,
      axisLabel: branchLabels[definition.ruleId],
      status: completed?.status ?? liveStatus,
      elapsedMs: completed?.elapsedMs,
    }
  })
  const branchTotal = branches.reduce((total, branch) => total + (branch.elapsedMs ?? 0), 0)
  return {
    artifactId: detailString(details, 'artifact_id') ?? detailString(started?.details, 'artifact_id'),
    contractId: detailString(details, 'contract_id')
      ?? detailString(started?.details, 'contract_id')
      ?? props.contract?.contract_id,
    correlationId: detailString(details, 'correlation_id'),
    elapsedMs,
    isComplete,
    branches,
    savedMs: isComplete && elapsedMs !== undefined && branchTotal > elapsedMs
      ? branchTotal - elapsedMs
      : undefined,
  }
})

function elapsedLabel(value: number | undefined): string {
  if (value === undefined) return '执行中'
  if (value < 1000) return `${Math.round(value)} ms`
  return `${(value / 1000).toFixed(2)} s`
}
const recentEvents = computed(() => props.events.slice(-3).reverse())

function advanceMotion(): void {
  const now = Date.now()
  if (!previousTick) previousTick = now
  const elapsed = Math.min(now - previousTick, 120)
  previousTick = now
  if (motionEnabled.value) motionProgress.value = (motionProgress.value + elapsed / 2600) % 1
}

onMounted(() => {
  reducedMotion.value = typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches
  if (import.meta.env.MODE !== 'test') {
    previousTick = Date.now()
    motionTimer = window.setInterval(advanceMotion, 50)
  }
})

onBeforeUnmount(() => {
  if (motionTimer !== undefined) window.clearInterval(motionTimer)
})

function cycle(offset = 0): number {
  return (motionProgress.value + offset) % 1
}

function movingOpacity(progress: number): number {
  if (progress < .1) return progress / .1
  if (progress > .82) return (1 - progress) / .18
  return 1
}

function routeMotionStyle(route: typeof agentRoutes[number], offset = 0) {
  const progress = cycle(offset)
  return {
    animation: 'none',
    left: `${route.sx + (50 - route.sx) * progress}%`,
    top: `${route.sy + (50 - route.sy) * progress}%`,
    opacity: movingOpacity(progress),
    transform: `translate(-50%,-50%) scale(${.72 + Math.sin(progress * Math.PI) * .38})`,
  }
}

function activeAvatarStyle(agent: AgentCard) {
  if (!['working', 'collaborating', 'reviewing', 'debating'].includes(agent.status)) return undefined
  const lift = Math.sin(motionProgress.value * Math.PI * 2) * 5
  return {
    animation: 'none',
    transform: `translateY(${lift.toFixed(1)}px) scale(${(1.06 + Math.abs(lift) / 70).toFixed(3)})`,
  }
}

function orbitRingStyle(direction: 1 | -1) {
  return {
    animation: 'none',
    strokeDashoffset: String(motionProgress.value * 120 * direction),
  }
}

function routeDashStyle(active: boolean) {
  return {
    animation: 'none',
    strokeDashoffset: String(-motionProgress.value * (active ? 84 : 30)),
  }
}

function isApproaching(agent: AgentCard): boolean {
  return ['collaborating', 'reviewing', 'debating'].includes(agent.status)
    || agent.peers.length > 0
}

function routeIsActive(agent: AgentActivityId): boolean {
  const status = agents.value.find((item) => item.id === agent)?.status
  return Boolean(status && ['working', 'collaborating', 'reviewing', 'debating'].includes(status))
}

function statusLabel(status: AgentActivityStatus): string {
  const labels: Record<AgentActivityStatus, string> = {
    idle: '待机', queued: '排队', working: '工作中', waiting: '等待',
    collaborating: '协同', reviewing: '审查中', debating: '复核中',
    approved: '已通过', blocked: '已阻断', done: '已完成',
  }
  return labels[status]
}
</script>

<template>
  <section
    class="agent-stage panel"
    :class="{ 'is-motion-paused': !motionEnabled }"
    aria-label="Agent 实时协同舞台"
  >
    <header class="agent-stage-heading">
      <div>
        <span class="stage-kicker"><Radio :size="13" aria-hidden="true" /> LIVE ORCHESTRATION</span>
        <h2>智能体协同舱</h2>
      </div>
      <div class="stage-controls">
        <button
          class="motion-toggle"
          type="button"
          :aria-pressed="motionEnabled"
          :aria-label="motionEnabled ? '暂停 Agent 动效' : '播放 Agent 动效'"
          @click="motionEnabled = !motionEnabled"
        >
          <Pause v-if="motionEnabled" :size="12" aria-hidden="true" />
          <Play v-else :size="12" aria-hidden="true" />
          {{ motionEnabled ? `FLOW ${flowPercent}%` : `已暂停 ${flowPercent}%` }}
          <small v-if="reducedMotion">RM兼容</small>
        </button>
        <div class="stage-metrics" aria-label="协同状态摘要">
          <span v-if="reviewFanOut"><b>{{ reviewFanOut }}</b> 路并发审核</span>
          <span><b>{{ activeCount }}</b> 活跃</span>
          <span><b>{{ approvedCount }}</b> 已接力</span>
        </div>
      </div>
    </header>

    <div
      class="agent-stage-canvas"
      :data-stage="view.currentState"
    >
      <svg class="agent-links" viewBox="0 0 760 430" preserveAspectRatio="none" aria-hidden="true">
        <circle cx="380" cy="215" r="118" :style="orbitRingStyle(1)" />
        <circle cx="380" cy="215" r="82" :style="orbitRingStyle(-1)" />
        <g v-for="route in agentRoutes" :key="route.id" :class="{ 'is-live': routeIsActive(route.id) }">
          <path
            class="agent-route"
            :d="route.path"
            :style="routeDashStyle(routeIsActive(route.id))"
          />
        </g>
      </svg>

      <div class="agent-particle-layer" aria-hidden="true">
        <template v-for="route in agentRoutes" :key="`particle-${route.id}`">
          <span
            v-if="routeIsActive(route.id)"
            class="agent-packet"
            :class="`route-${route.id}`"
            :style="routeMotionStyle(route)"
          ></span>
          <span
            v-if="routeIsActive(route.id)"
            class="agent-packet is-lagging"
            :class="`route-${route.id}`"
            :style="routeMotionStyle(route, .5)"
          ></span>
          <span
            v-if="routeIsActive(route.id)"
            class="data-capsule"
            :class="`route-${route.id}`"
            :style="routeMotionStyle(route, .23)"
          >
            <b>{{ route.code }}</b>
            <small>→ CORE</small>
          </span>
        </template>
      </div>

      <div
        class="stage-core"
        :class="{ 'is-processing': activeCount > 0 }"
      >
        <i
          v-if="activeCount > 0"
          class="core-radar"
          :style="{ animation: 'none', transform: `rotate(${motionProgress * 360}deg)` }"
          aria-hidden="true"
        ></i>
        <Sparkles :size="18" aria-hidden="true" />
        <span>当前协作动作</span>
        <strong>{{ headline }}</strong>
        <small>{{ view.currentState }}</small>
      </div>

      <ul class="agent-stage-roster">
        <li
          v-for="(agent, index) in agents"
          :key="agent.id"
          :class="[
            `agent-slot-${index + 1}`,
            `is-${agent.status}`,
            { 'is-approaching': isApproaching(agent) },
          ]"
          :data-agent="agent.id"
          :aria-label="`${agentLabel(agent.id)}，${agentPurpose(agent.id)}，${statusLabel(agent.status)}`"
        >
          <span class="agent-signal" aria-hidden="true"></span>
          <span class="agent-avatar" :style="activeAvatarStyle(agent)">
            <AgentTeacherAvatar :agent="agent.id" :status="agent.status" :size="58" />
            <span class="agent-motion-bars" aria-hidden="true"><i></i><i></i><i></i></span>
          </span>
          <span class="agent-copy">
            <b>{{ agentLabel(agent.id) }}</b>
            <small>{{ agent.label }}</small>
          </span>
          <span class="agent-status">
            <CircleAlert v-if="agent.status === 'blocked'" :size="12" aria-hidden="true" />
            <Check v-else-if="agent.status === 'approved' || agent.status === 'done'" :size="12" aria-hidden="true" />
            {{ statusLabel(agent.status) }}
          </span>
        </li>
      </ul>
    </div>

    <Transition name="proof">
      <section
        v-if="parallelReviewProof"
        class="parallel-proof"
        :class="{ 'is-running': !parallelReviewProof.isComplete, 'is-complete': parallelReviewProof.isComplete }"
        aria-label="本轮并行审核执行证据"
      >
        <header class="parallel-proof-heading">
          <div>
            <span>PARALLEL EXECUTION PROOF</span>
            <strong>本轮并行审核证据</strong>
          </div>
          <b class="proof-state">
            <i aria-hidden="true"></i>
            {{ parallelReviewProof.isComplete ? '已汇聚 · 确定性裁决' : '双路并行执行中' }}
          </b>
        </header>

        <div class="parallel-proof-flow">
          <article
            v-for="branch in parallelReviewProof.branches"
            :key="branch.agentId"
            class="proof-branch"
            :class="`is-${branch.status}`"
            :data-agent="branch.agentId"
          >
            <div class="proof-branch-title">
              <code>{{ branch.ruleId }}</code>
              <span>{{ branch.status === 'running' ? 'RUNNING' : branch.status.toUpperCase() }}</span>
            </div>
            <strong>{{ branch.label }}</strong>
            <small class="proof-axis">{{ branch.axisLabel }}</small>
            <div class="proof-progress" aria-hidden="true"><i></i></div>
            <time>{{ elapsedLabel(branch.elapsedMs) }}</time>
          </article>

          <div class="proof-merge" :class="{ 'is-complete': parallelReviewProof.isComplete }">
            <span><Check v-if="parallelReviewProof.isComplete" :size="18" aria-hidden="true" /><i v-else></i></span>
            <b>{{ parallelReviewProof.isComplete ? 'JOINED' : 'JOIN' }}</b>
            <small>固定顺序汇聚</small>
          </div>
        </div>

        <footer class="parallel-proof-meta">
          <span>
            <small>LEARNING CONTRACT</small>
            <code :title="parallelReviewProof.contractId">{{ shortId(parallelReviewProof.contractId) }}</code>
          </span>
          <span>
            <small>ARTIFACT</small>
            <code :title="parallelReviewProof.artifactId">{{ shortId(parallelReviewProof.artifactId) }}</code>
          </span>
          <span>
            <small>并行墙钟</small>
            <b>{{ elapsedLabel(parallelReviewProof.elapsedMs) }}</b>
          </span>
          <span v-if="parallelReviewProof.savedMs !== undefined" class="proof-saving">
            <small>并行节省</small>
            <b>≈ {{ elapsedLabel(parallelReviewProof.savedMs) }}</b>
          </span>
        </footer>
      </section>
    </Transition>

    <TransitionGroup
      v-if="recentEvents.length"
      name="activity"
      tag="ol"
      class="stage-activity-feed"
      aria-label="最近 Agent 动作"
    >
      <li v-for="event in recentEvents" :key="event.sequence">
        <span :data-agent="event.agent"></span>
        <b>{{ eventAgentLabel(event.agent) }}</b>
        <p>{{ event.label }}</p>
        <small>#{{ event.sequence }}</small>
      </li>
    </TransitionGroup>
  </section>
</template>

<style scoped>
.agent-stage { overflow: hidden; padding: 0; color: #eaf7ff; background: linear-gradient(145deg, #07111f, #0a1c2c 54%, #071725); border-color: rgba(91,211,255,.16); }
.agent-stage-heading { display: flex; align-items: center; justify-content: space-between; padding: 20px 22px 12px; }
.agent-stage-heading h2 { margin: 6px 0 0; font-size: 20px; letter-spacing: .02em; }
.stage-kicker { display: inline-flex; align-items: center; gap: 6px; color: #55d7ff; font: 700 10px/1.2 ui-monospace, monospace; letter-spacing: .14em; }
.stage-kicker svg { filter: drop-shadow(0 0 6px #31ccff); }
.stage-controls { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.motion-toggle { display: inline-flex; align-items: center; gap: 5px; padding: 6px 9px; color: #5be3ff; background: rgba(42,202,245,.08); border: 1px solid rgba(84,218,255,.28); border-radius: 8px; font: 700 10px/1 ui-monospace,monospace; cursor: pointer; }
.motion-toggle::before { width: 5px; height: 5px; content: ''; background: #57e6ff; border-radius: 50%; box-shadow: 0 0 8px #57e6ff; animation: motion-led .8s ease-in-out infinite alternate; }
.motion-toggle:hover { background: rgba(42,202,245,.14); }
.is-motion-paused .motion-toggle { color: #7995a5; background: rgba(255,255,255,.035); border-color: rgba(255,255,255,.09); }
.is-motion-paused .motion-toggle::before { background: #67808e; box-shadow: none; }
.stage-metrics { display: flex; gap: 8px; }
.stage-metrics span { padding: 6px 9px; color: #8eb3c8; background: rgba(255,255,255,.04); border: 1px solid rgba(255,255,255,.07); border-radius: 8px; font-size: 11px; }
.stage-metrics b { color: #f2fbff; }
.agent-stage-canvas { position: relative; min-height: 430px; overflow: hidden; isolation: isolate; }
.agent-stage-canvas::before { position: absolute; inset: 8%; content: ''; background-image: linear-gradient(rgba(60,170,220,.045) 1px, transparent 1px), linear-gradient(90deg, rgba(60,170,220,.045) 1px, transparent 1px); background-size: 28px 28px; mask-image: radial-gradient(circle, black, transparent 70%); }
.agent-links { position: absolute; inset: 0; width: 100%; height: 100%; color: #4bd4ff; }
.agent-links > circle { fill: none; stroke: currentColor; stroke-width: 1; opacity: .12; stroke-dasharray: 3 9; transform-origin: center; animation: orbit 24s linear infinite; }
.agent-links > circle + circle { opacity: .18; animation-duration: 17s; animation-direction: reverse; }
.agent-route { fill: none; stroke: rgba(86,191,225,.13); stroke-width: 1.4; stroke-dasharray: 4 8; vector-effect: non-scaling-stroke; transition: stroke .35s, opacity .35s; }
.agent-links g.is-live .agent-route { stroke: rgba(79,222,255,.72); stroke-width: 2; stroke-dasharray: 10 8; filter: drop-shadow(0 0 4px #3cdbff); animation: route-flow .75s linear infinite; }
.agent-particle-layer { position: absolute; z-index: 4; inset: 0; pointer-events: none; }
.agent-packet,.data-capsule { --sx: 50%; --sy: 13%; position: absolute; left: var(--sx); top: var(--sy); transform: translate(-50%,-50%); }
.agent-packet { width: 14px; height: 14px; background: #e8fbff; border: 3px solid #3bdaff; border-radius: 50%; box-shadow: 0 0 8px #27d4ff,0 0 22px #27d4ff; animation: agent-packet-travel 2.15s linear infinite; }
.agent-packet::after { position: absolute; top: 3px; right: 10px; width: 40px; height: 3px; content: ''; background: linear-gradient(90deg,transparent,rgba(75,220,255,.9)); filter: blur(.2px); }
.agent-packet.is-lagging { width: 9px; height: 9px; opacity: .7; animation-delay: -1.05s; }
.data-capsule { z-index: 2; display: flex; align-items: center; gap: 4px; width: max-content; padding: 4px 7px; color: #dffaff; background: rgba(7,40,58,.96); border: 1px solid #4bdcff; border-radius: 5px; box-shadow: 0 0 13px rgba(60,218,255,.65); font: 700 8px/1 ui-monospace,monospace; letter-spacing: .04em; animation: capsule-travel 2.15s linear infinite; animation-delay: -.52s; }
.data-capsule small { color: #65dfff; font-size: 7px; }
.agent-packet.route-diagnosis,.data-capsule.route-diagnosis { --sx: 17%; --sy: 24%; }
.agent-packet.route-knowledge,.data-capsule.route-knowledge { --sx: 50%; --sy: 13%; }
.agent-packet.route-review,.data-capsule.route-review { --sx: 83%; --sy: 24%; }
.agent-packet.route-verification,.data-capsule.route-verification { --sx: 75%; --sy: 78%; }
.agent-packet.route-task,.data-capsule.route-task { --sx: 25%; --sy: 78%; }
.stage-core { position: absolute; z-index: 2; top: 50%; left: 50%; display: grid; place-items: center; align-content: center; width: 178px; min-height: 126px; padding: 18px; text-align: center; background: radial-gradient(circle at 50% 20%, rgba(49,204,255,.18), rgba(5,23,37,.92) 68%); border: 1px solid rgba(76,210,255,.32); border-radius: 50%; box-shadow: 0 0 0 12px rgba(42,186,232,.025), 0 18px 60px rgba(0,0,0,.35); transform: translate(-50%,-50%); }
.core-radar { position: absolute; z-index: -1; width: 155px; height: 155px; background: conic-gradient(from 0deg,transparent 0 62%,rgba(55,218,255,.28) 76%,transparent 90%); border: 1px solid rgba(76,210,255,.2); border-radius: 50%; animation: core-radar-spin 1.8s linear infinite; }
.stage-core.is-processing { animation: core-pulse 2.4s ease-in-out infinite; }
.stage-core.is-processing::before,.stage-core.is-processing::after { position: absolute; inset: -8px; content: ''; border: 1px solid rgba(71,218,255,.45); border-radius: 50%; animation: core-wave 1.8s ease-out infinite; }
.stage-core.is-processing::after { animation-delay: -.9s; }
.stage-core svg, .stage-core small { color: #4edaff; }
.stage-core span { color: #79aabd; font-size: 10px; letter-spacing: .12em; }
.stage-core strong { max-width: 148px; font-size: 12px; line-height: 1.45; }
.stage-core small { font: 700 10px/1 ui-monospace, monospace; }
.agent-stage-roster { margin: 0; padding: 0; list-style: none; }
.agent-stage-roster li { --dx: 0px; --dy: 0px; position: absolute; z-index: 3; display: grid; grid-template-columns: 60px 1fr; gap: 8px; align-items: center; width: 152px; min-height: 92px; padding: 8px; color: #7f9daf; background: rgba(9,28,42,.9); border: 1px solid rgba(120,174,200,.12); border-radius: 15px; transform: translate(-50%,-50%); transition: transform .7s cubic-bezier(.2,.8,.2,1), border-color .35s, box-shadow .35s, color .35s; }
.agent-slot-1 { top: 24%; left: 17%; --dx: 44px; --dy: 27px; }
.agent-slot-2 { top: 13%; left: 50%; --dx: 0px; --dy: 43px; }
.agent-slot-3 { top: 24%; left: 83%; --dx: -44px; --dy: 27px; }
.agent-slot-4 { top: 78%; left: 75%; --dx: -42px; --dy: -34px; }
.agent-slot-5 { top: 78%; left: 25%; --dx: 42px; --dy: -34px; }
.agent-stage-roster li.is-approaching { transform: translate(calc(-50% + var(--dx)),calc(-50% + var(--dy))); }
.agent-avatar { position: relative; display: grid; place-items: center; width: 60px; height: 60px; overflow: hidden; color: currentColor; background: radial-gradient(circle at 50% 62%,rgba(82,219,255,.1),transparent 66%); border-radius: 13px; }
.agent-avatar::after { position: absolute; right: 4px; left: 4px; top: -8px; height: 2px; content: ''; opacity: 0; background: linear-gradient(90deg, transparent, currentColor, transparent); box-shadow: 0 0 8px currentColor; }
.agent-motion-bars { position: absolute; right: 3px; bottom: 3px; display: none; align-items: end; gap: 2px; height: 12px; padding: 2px; background: rgba(4,19,30,.82); border-radius: 3px; }
.agent-motion-bars i { width: 2px; height: 4px; background: currentColor; box-shadow: 0 0 4px currentColor; animation: work-bars .42s ease-in-out infinite alternate; }
.agent-motion-bars i:nth-child(2) { height: 9px; animation-delay: -.14s; }
.agent-motion-bars i:nth-child(3) { height: 6px; animation-delay: -.28s; }
.agent-copy { display: grid; gap: 3px; min-width: 0; }
.agent-copy b { color: #dcecf4; font-size: 12px; }
.agent-copy small { display: -webkit-box; overflow: hidden; color: #718fa1; font-size: 10px; line-height: 1.35; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }
.agent-status { grid-column: 1/-1; display: inline-flex; align-items: center; gap: 4px; width: max-content; padding: 3px 6px; color: #7390a2; background: rgba(255,255,255,.035); border-radius: 5px; font-size: 9px; }
.agent-signal { position: absolute; top: 10px; right: 10px; width: 6px; height: 6px; background: #385464; border-radius: 50%; }
.is-working,.is-collaborating,.is-reviewing,.is-debating { color: #55dcff !important; border-color: rgba(71,216,255,.45) !important; box-shadow: 0 0 25px rgba(33,190,234,.13); }
.is-working .agent-signal,.is-collaborating .agent-signal,.is-reviewing .agent-signal,.is-debating .agent-signal { background: #54e8ff; box-shadow: 0 0 0 4px rgba(84,232,255,.1),0 0 12px #54e8ff; animation: signal 1.25s ease-in-out infinite; }
.is-working .agent-avatar::after,.is-reviewing .agent-avatar::after { opacity: .8; animation: scanner 1.15s ease-in-out infinite; }
.is-working .agent-motion-bars,.is-collaborating .agent-motion-bars,.is-reviewing .agent-motion-bars,.is-debating .agent-motion-bars { display: flex; }
.is-working .agent-avatar { animation: agent-working-shell .72s ease-in-out infinite alternate; }
.is-collaborating .agent-avatar { animation: docking 1s ease-in-out infinite alternate; }
.is-reviewing { color: #b896ff !important; border-color: rgba(184,150,255,.42) !important; }
.is-debating { color: #ffb45b !important; border-color: rgba(255,180,91,.44) !important; }
.is-approved,.is-done { color: #62d8a2 !important; }
.is-approved .agent-signal,.is-done .agent-signal { background: #48ce91; box-shadow: 0 0 9px rgba(72,206,145,.65); }
.is-blocked { color: #ff7e7e !important; border-color: rgba(255,126,126,.48) !important; }
.parallel-proof { margin: 0 14px 14px; padding: 14px; background: linear-gradient(135deg,rgba(5,25,39,.96),rgba(8,31,47,.92)); border: 1px solid rgba(76,210,255,.22); border-radius: 13px; box-shadow: inset 0 1px rgba(255,255,255,.025),0 12px 30px rgba(0,0,0,.18); }
.parallel-proof-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.parallel-proof-heading > div { display: grid; gap: 3px; }
.parallel-proof-heading div > span { color: #53d8ff; font: 700 8px/1 ui-monospace,monospace; letter-spacing: .14em; }
.parallel-proof-heading div > strong { color: #e7f7ff; font-size: 12px; }
.proof-state { display: inline-flex; align-items: center; gap: 6px; padding: 5px 8px; color: #65e2ff; background: rgba(57,205,245,.075); border: 1px solid rgba(76,210,255,.2); border-radius: 999px; font-size: 9px; font-weight: 600; }
.proof-state i { width: 6px; height: 6px; background: currentColor; border-radius: 50%; box-shadow: 0 0 9px currentColor; }
.parallel-proof.is-running .proof-state i { animation: proof-led .7s ease-in-out infinite alternate; }
.parallel-proof.is-complete .proof-state { color: #5de0a3; background: rgba(62,213,147,.075); border-color: rgba(78,219,157,.22); }
.parallel-proof-flow { position: relative; display: grid; grid-template-columns: minmax(0,1fr) 78px minmax(0,1fr); gap: 10px; align-items: center; }
.parallel-proof-flow::before { position: absolute; z-index: 0; top: 50%; right: 22%; left: 22%; height: 1px; content: ''; background: repeating-linear-gradient(90deg,rgba(73,219,255,.7) 0 8px,transparent 8px 14px); opacity: .5; }
.parallel-proof.is-running .parallel-proof-flow::after { position: absolute; z-index: 1; top: calc(50% - 2px); left: 22%; width: 34px; height: 4px; content: ''; background: linear-gradient(90deg,transparent,#dffbff,transparent); border-radius: 99px; filter: drop-shadow(0 0 6px #4fe0ff); animation: proof-packet 1.35s ease-in-out infinite; }
.proof-branch { position: relative; z-index: 2; display: grid; grid-template-columns: 1fr auto; gap: 6px 10px; min-width: 0; padding: 10px 11px; background: rgba(8,39,56,.96); border: 1px solid rgba(77,211,247,.22); border-radius: 9px; }
.proof-branch:nth-of-type(1) { grid-column: 1; }
.proof-branch:nth-of-type(2) { grid-column: 3; }
.proof-branch-title { grid-column: 1/-1; display: flex; align-items: center; justify-content: space-between; gap: 8px; }
.proof-branch-title code { color: #60ddff; font: 800 9px/1 ui-monospace,monospace; }
.proof-branch-title span { color: #638b9e; font: 700 7px/1 ui-monospace,monospace; letter-spacing: .09em; }
.proof-branch > strong { overflow: hidden; color: #dff5ff; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.proof-axis { grid-column: 1/-1; color: #718fa1; font-size: 8px; line-height: 1.2; }
.proof-branch time { color: #7da8ba; font: 700 8px/1 ui-monospace,monospace; }
.proof-progress { grid-column: 1/-1; height: 3px; overflow: hidden; background: rgba(255,255,255,.055); border-radius: 99px; }
.proof-progress i { display: block; width: 100%; height: 100%; background: linear-gradient(90deg,#2acdf7,#7aeaff,#2acdf7); box-shadow: 0 0 8px rgba(61,218,255,.7); transform-origin: left; }
.proof-branch.is-running .proof-progress i { animation: proof-progress 1.1s ease-in-out infinite; }
.proof-branch.is-succeeded { border-color: rgba(75,218,154,.3); }
.proof-branch.is-succeeded .proof-branch-title code,.proof-branch.is-succeeded .proof-branch-title span { color: #5bd99c; }
.proof-branch.is-succeeded .proof-progress i { background: #54d89a; box-shadow: 0 0 8px rgba(84,216,154,.55); }
.proof-branch.is-failed { border-color: rgba(255,112,112,.45); }
.proof-branch.is-failed .proof-progress i { background: #ff7474; }
.proof-branch[data-agent="pedagogy_review"] { background: rgba(27,25,61,.94); border-color: rgba(177,143,255,.3); }
.proof-branch[data-agent="pedagogy_review"] .proof-branch-title code { color: #c0a0ff; }
.proof-branch[data-agent="pedagogy_review"] .proof-progress i { background: linear-gradient(90deg,#8f70eb,#c4a9ff,#8f70eb); box-shadow: 0 0 8px rgba(174,139,255,.55); }
.proof-merge { position: relative; z-index: 3; grid-column: 2; grid-row: 1; display: grid; place-items: center; gap: 3px; color: #5edfff; text-align: center; }
.proof-merge > span { display: grid; place-items: center; width: 37px; height: 37px; background: #082a3d; border: 1px solid rgba(80,220,255,.52); border-radius: 50%; box-shadow: 0 0 0 5px rgba(56,205,244,.045),0 0 17px rgba(56,205,244,.18); }
.proof-merge > span i { width: 9px; height: 9px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: proof-spin .8s linear infinite; }
.proof-merge b { font: 800 8px/1 ui-monospace,monospace; letter-spacing: .08em; }
.proof-merge small { color: #5e8294; font-size: 7px; white-space: nowrap; }
.proof-merge.is-complete { color: #59dfa0; }
.proof-merge.is-complete > span { background: #082f29; border-color: rgba(87,222,159,.55); box-shadow: 0 0 0 5px rgba(75,213,148,.045),0 0 17px rgba(75,213,148,.18); }
.parallel-proof-meta { display: grid; grid-template-columns: minmax(0,1.35fr) minmax(0,1.35fr) minmax(90px,.65fr) minmax(90px,.65fr); gap: 7px; margin-top: 10px; padding-top: 9px; border-top: 1px solid rgba(112,193,224,.1); }
.parallel-proof-meta > span { display: grid; gap: 3px; min-width: 0; }
.parallel-proof-meta small { color: #557687; font: 700 7px/1 ui-monospace,monospace; letter-spacing: .08em; }
.parallel-proof-meta code,.parallel-proof-meta b { overflow: hidden; color: #a9c8d6; font: 700 8px/1.2 ui-monospace,monospace; text-overflow: ellipsis; white-space: nowrap; }
.parallel-proof-meta .proof-saving b { color: #5bdda0; }
.proof-enter-active,.proof-leave-active { transition: opacity .28s ease,transform .28s ease; }
.proof-enter-from,.proof-leave-to { opacity: 0; transform: translateY(-6px); }
.stage-activity-feed { display: grid; gap: 5px; margin: 0; padding: 10px 14px 14px; list-style: none; background: rgba(2,12,20,.42); border-top: 1px solid rgba(113,187,220,.08); }
.stage-activity-feed li { display: grid; grid-template-columns: 7px auto 1fr auto; gap: 8px; align-items: center; min-height: 26px; padding: 0 8px; color: #8babbc; font-size: 10px; }
.stage-activity-feed li > span { width: 6px; height: 6px; background: #4edaff; border-radius: 50%; box-shadow: 0 0 7px currentColor; }
.stage-activity-feed b { color: #dcecf4; }
.stage-activity-feed p { overflow: hidden; margin: 0; text-overflow: ellipsis; white-space: nowrap; }
.stage-activity-feed small { color: #466476; font-family: ui-monospace, monospace; }
.activity-enter-active,.activity-leave-active { transition: all .25s ease; }
.activity-enter-from,.activity-leave-to { opacity: 0; transform: translateY(-5px); }
@keyframes signal { 50% { opacity: .45; transform: scale(.72); } }
@keyframes motion-led { to { opacity: .35; transform: scale(.7); } }
@keyframes core-pulse { 50% { box-shadow: 0 0 0 18px rgba(42,186,232,.03),0 18px 70px rgba(0,0,0,.42),0 0 30px rgba(42,202,245,.11); } }
@keyframes core-wave { 0% { opacity: .8; transform: scale(.86); } 100% { opacity: 0; transform: scale(1.35); } }
@keyframes core-radar-spin { to { transform: rotate(360deg); } }
@keyframes orbit { to { transform: rotate(360deg); } }
@keyframes route-flow { to { stroke-dashoffset: -18; } }
@keyframes agent-packet-travel { 0% { left: var(--sx); top: var(--sy); opacity: 0; transform: translate(-50%,-50%) scale(.55); } 10% { opacity: 1; } 82% { opacity: 1; } 100% { left: 50%; top: 50%; opacity: 0; transform: translate(-50%,-50%) scale(1.5); } }
@keyframes capsule-travel { 0% { left: var(--sx); top: var(--sy); opacity: 0; transform: translate(-50%,-50%) scale(.82); } 13% { opacity: 1; } 72% { opacity: 1; transform: translate(-50%,-50%) scale(1); } 100% { left: 50%; top: 50%; opacity: 0; transform: translate(-50%,-50%) scale(.8); } }
@keyframes scanner { 0% { top: 3px; } 50% { top: 41px; } 100% { top: 3px; } }
@keyframes agent-working-shell { to { background: rgba(77,218,255,.17); box-shadow: inset 0 0 17px rgba(77,218,255,.22),0 0 12px rgba(77,218,255,.16); transform: translateY(-4px) scale(1.06); } }
@keyframes work-bars { to { height: 11px; opacity: .45; } }
@keyframes robot-work { to { transform: translateY(-3px) rotate(2deg); } }
@keyframes docking { to { background: rgba(77,218,255,.16); box-shadow: inset 0 0 15px rgba(77,218,255,.18); transform: scale(1.08); } }
@keyframes review-tilt { 25% { transform: rotate(-4deg); } 75% { transform: rotate(4deg); } }
@keyframes debate-shake { to { transform: translateX(3px) rotate(2deg); } }
@keyframes proof-led { to { opacity: .28; transform: scale(.7); } }
@keyframes proof-packet { 0% { left: 22%; opacity: 0; } 18%,82% { opacity: 1; } 100% { left: calc(78% - 34px); opacity: 0; } }
@keyframes proof-progress { 0% { transform: translateX(-82%) scaleX(.22); } 55% { transform: translateX(18%) scaleX(.45); } 100% { transform: translateX(100%) scaleX(.18); } }
@keyframes proof-spin { to { transform: rotate(360deg); } }
.agent-stage.is-motion-paused *,.agent-stage.is-motion-paused *::before,.agent-stage.is-motion-paused *::after { animation-play-state: paused !important; }
@media (max-width: 900px) { .agent-stage-heading { align-items: flex-start; } .stage-controls { max-width: 58%; } .agent-stage-roster li { width: 146px; } .agent-copy small { display: none; } .parallel-proof-meta { grid-template-columns: 1fr 1fr; } }
@media (prefers-reduced-motion: reduce) { .motion-toggle { outline: 1px solid rgba(255,255,255,.05); } }
</style>
