<script setup lang="ts">
import { Activity, ChevronLeft, ChevronRight } from '@lucide/vue'
import { computed, ref, watch } from 'vue'

import type { AgentActivityStatus } from '../lib/interactiveApi'
import { agentLabel, agentPurpose } from '../lib/tracePresentation'
import type { AgentId, DebateGroup, StateId, TraceMessage, TraceView } from '../types/trace'
import AgentTeacherAvatar from './AgentTeacherAvatar.vue'
import DebateGroupComponent from './DebateGroup.vue'
import StateRail from './StateRail.vue'
import TraceCard from './TraceCard.vue'


const props = defineProps<{ view: TraceView }>()
const recordPage = ref(0)

type TimelineItem =
  | { kind: 'message'; id: string; message: TraceMessage }
  | { kind: 'debate'; id: string; group: DebateGroup }

type CollaborationAgent = Exclude<AgentId, 'system'>
type AgentRosterState = 'active' | 'seen' | 'pending'

const agentOrder = [
  'diagnosis',
  'knowledge',
  'task',
  'verification',
  'review',
] as const satisfies readonly CollaborationAgent[]

const stateOwners: Partial<Record<StateId, CollaborationAgent>> = {
  S1_DIAGNOSIS: 'diagnosis',
  S2_KNOWLEDGE: 'knowledge',
  S3_TASK: 'task',
  S4_VERIFY: 'verification',
  S5_REVIEW: 'review',
  S6_DEBATE: 'knowledge',
  S8_PROBE: 'task',
}

function isCollaborationAgent(agent: AgentId): agent is CollaborationAgent {
  return agent !== 'system'
}

const acceptedMessages = computed(() => props.view.visibleMessages.filter(
  (message) => !message.rejectedByBus,
))

const seenAgents = computed(() => {
  const agents = new Set<CollaborationAgent>()
  for (const message of acceptedMessages.value) {
    if (isCollaborationAgent(message.agent)) agents.add(message.agent)
  }
  return agents
})

const activeAgent = computed<CollaborationAgent | undefined>(() => {
  const transitionStep = [...acceptedMessages.value].reverse().find(
    (message) => message.content.action === 'state_transition',
  )?.step ?? 0
  const latestSpeaker = [...acceptedMessages.value].reverse().find(
    (message) => message.step > transitionStep && isCollaborationAgent(message.agent),
  )
  return latestSpeaker && isCollaborationAgent(latestSpeaker.agent)
    ? latestSpeaker.agent
    : stateOwners[props.view.currentState as StateId]
})

function rosterState(agent: CollaborationAgent): AgentRosterState {
  if (activeAgent.value === agent) return 'active'
  return seenAgents.value.has(agent) ? 'seen' : 'pending'
}

function rosterStatus(agent: CollaborationAgent): string {
  const labels: Record<AgentRosterState, string> = {
    active: '当前执行',
    seen: '已参与',
    pending: '待接力',
  }
  return labels[rosterState(agent)]
}

function teacherStatus(agent: CollaborationAgent): AgentActivityStatus {
  const state = rosterState(agent)
  if (state === 'active') return 'working'
  if (state === 'seen') return 'done'
  return 'idle'
}

const systemMessages = computed(() => props.view.visibleMessages.filter(
  (message) => message.agent === 'system',
))

const timeline = computed<TimelineItem[]>(() => {
  const groupedMessageIds = new Set<string>()
  const groupsByFirstMessage = new Map<string, DebateGroup>()
  for (const group of props.view.debateGroups) {
    group.messages.forEach((message) => groupedMessageIds.add(message.msgId))
    const first = group.messages[0]
    if (first) groupsByFirstMessage.set(first.msgId, group)
  }

  const items: TimelineItem[] = []
  for (const message of props.view.visibleMessages) {
    if (message.agent === 'system') continue
    const group = groupsByFirstMessage.get(message.msgId)
    if (group) {
      items.push({ kind: 'debate', id: group.id, group })
      continue
    }
    if (!groupedMessageIds.has(message.msgId)) {
      items.push({ kind: 'message', id: message.msgId, message })
    }
  }
  return items
})

watch(
  () => timeline.value.length,
  (length, previousLength) => {
    if (!length) {
      recordPage.value = 0
      return
    }
    if (length > (previousLength ?? 0)) {
      recordPage.value = length - 1
      return
    }
    recordPage.value = Math.min(recordPage.value, length - 1)
  },
  { immediate: true },
)

function previousRecord(): void {
  recordPage.value = Math.max(0, recordPage.value - 1)
}

function nextRecord(): void {
  recordPage.value = Math.min(timeline.value.length - 1, recordPage.value + 1)
}
</script>

<template>
  <aside class="panel trace-panel">
    <header class="panel-heading trace-heading">
      <div>
        <h2>协作记录</h2>
        <span v-if="view.debateGroups.length" class="debate-summary">
          发生过驳回复审 · {{ view.debateGroups.length }}次
        </span>
      </div>
      <span class="message-count"><Activity :size="14" aria-hidden="true" />{{ view.visibleMessages.length }} 条</span>
    </header>

    <ul class="agent-collaboration-map" aria-label="五位智能体老师协作状态">
      <li
        v-for="agent in agentOrder"
        :key="agent"
        :class="[`agent-${agent}`, `is-${rosterState(agent)}`]"
        :data-agent="agent"
        :aria-current="activeAgent === agent ? 'step' : undefined"
        :aria-label="`${agentLabel(agent)}，${agentPurpose(agent)}，${rosterStatus(agent)}`"
      >
        <span class="agent-teacher-shell" aria-hidden="true">
          <AgentTeacherAvatar :agent="agent" :status="teacherStatus(agent)" :size="46" />
          <span class="agent-status-light"></span>
        </span>
        <span class="agent-role-copy">
          <strong>{{ agentLabel(agent) }}</strong>
          <span class="agent-role-purpose">{{ agentPurpose(agent) }}</span>
          <span class="agent-role-status">{{ rosterStatus(agent) }}</span>
        </span>
      </li>
    </ul>

    <StateRail :messages="view.visibleMessages" :current-state="view.currentState" />

    <details v-if="systemMessages.length" class="system-records">
      <summary>流程记录 · {{ systemMessages.length }}条</summary>
      <div class="system-record-list">
        <TraceCard
          v-for="message in systemMessages"
          :key="message.msgId"
          :message="message"
        />
      </div>
    </details>

    <div class="trace-record-page" aria-live="polite">
      <div
        v-for="(item, index) in timeline"
        v-show="recordPage === index"
        :key="item.id"
        class="trace-record-slide"
      >
        <DebateGroupComponent v-if="item.kind === 'debate'" :group="item.group" />
        <TraceCard v-else :message="item.message" />
      </div>
      <p v-if="!timeline.length" class="trace-record-empty">协作开始后，本轮记录会在这里逐页出现。</p>
    </div>

    <footer v-if="timeline.length" class="trace-record-pager">
      <button
        type="button"
        aria-label="上一条协作记录"
        :disabled="recordPage === 0"
        @click="previousRecord"
      ><ChevronLeft :size="16" aria-hidden="true" />上一条</button>
      <span><b>{{ recordPage + 1 }}</b> / {{ timeline.length }}</span>
      <button
        type="button"
        aria-label="下一条协作记录"
        :disabled="recordPage >= timeline.length - 1"
        @click="nextRecord"
      >下一条<ChevronRight :size="16" aria-hidden="true" /></button>
    </footer>
  </aside>
</template>
