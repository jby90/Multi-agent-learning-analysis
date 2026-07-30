<script setup lang="ts">
import { Award, Check, CircleDot, Route } from '@lucide/vue'
import { computed } from 'vue'

import { learningSummary, nextLearningPlan } from '../lib/learningInsights'
import { learnerText, misconceptionLabel } from '../lib/tracePresentation'
import type { KnowledgeCatalogEntry, TraceView } from '../types/trace'


const props = defineProps<{
  view: TraceView
  catalog: KnowledgeCatalogEntry[]
}>()

const message = computed(() => props.view.path)

const completed = computed(() => {
  const value = message.value?.content.completed_nodes
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string').map(learnerText)
    : []
})

const current = computed(() => {
  const value = message.value?.content.current_node
  return typeof value === 'string' ? learnerText(value) : undefined
})

const plan = computed(() => nextLearningPlan(props.view, props.catalog))
const summary = computed(() => learningSummary(props.view, props.catalog))

const levelLabel = {
  basic: '基础档',
  applied: '应用档',
  advanced: '进阶档',
}

type PathNode = {
  key: string
  label: string
  status: 'complete' | 'current' | 'planned'
  isNext?: boolean
}

const traceNodes = computed(() => {
  const values: PathNode[] = completed.value.map((label) => ({
    key: `complete-${label}`,
    label,
    status: 'complete' as const,
  }))
  if (current.value && !completed.value.includes(current.value)) {
    values.push({
      key: `current-${current.value}`,
      label: current.value,
      status: 'current' as const,
    })
  }
  if (plan.value) {
    values.push({
      key: `planned-${plan.value.knowledgePoint}`,
      label: plan.value.knowledgePoint,
      status: 'planned' as const,
    })
  }
  return values
})

const stageLabels = ['岗位与诊断', '个性微课', '数据实操', '理解核对', '培养更新']
const stateStageIndex: Record<string, number> = {
  S0_INIT: 0,
  S1_DIAGNOSIS: 0,
  S2_KNOWLEDGE: 1,
  S3_TASK: 2,
  S4_VERIFY: 2,
  S5_REVIEW: 3,
  S6_DEBATE: 3,
  S7_STUDENT: 3,
  S8_PROBE: 3,
  S9_PATH_UPDATE: 4,
  S10_DONE: 5,
  S_FAIL: 4,
}

const currentStageIndex = computed(() => stateStageIndex[props.view.currentState] ?? 0)
const progressPercent = computed(() => Math.round(
  (Math.min(currentStageIndex.value, stageLabels.length) / stageLabels.length) * 100,
))
const currentStageLabel = computed(() => (
  current.value
    ?? (currentStageIndex.value >= stageLabels.length
    ? '本轮训练已完成'
    : stageLabels[currentStageIndex.value])
))

const nodes = computed<PathNode[]>(() => {
  const stages: PathNode[] = stageLabels.map((label, index) => ({
    key: `stage-${index}`,
    label,
    status: index < currentStageIndex.value
      ? 'complete'
      : index === currentStageIndex.value
        ? 'current'
        : 'planned',
  }))
  if (plan.value) {
    const nextStage = stages.find((stage) => stage.status === 'planned')
    if (nextStage) {
      nextStage.isNext = true
    } else {
      stages.push({
        key: `next-focus-${plan.value.knowledgePoint}`,
        label: '后续重点',
        status: 'planned',
        isNext: true,
      })
    }
  }
  return stages
})

const nextStep = computed(() => {
  if (plan.value) return `${plan.value.knowledgePoint} · ${levelLabel[plan.value.difficulty]}`
  const next = stageLabels[Math.min(currentStageIndex.value + 1, stageLabels.length - 1)]
  return currentStageIndex.value >= stageLabels.length ? '保持并巩固本轮成果' : `下一步：${next}`
})

/* Keep the trace-derived path available to accessibility and future exports;
   the visible strip uses a stable five-stage vocabulary so sparse early traces
   never produce an empty oversized card. */
const tracePathSummary = computed(() => traceNodes.value.map((item) => item.label).join('、'))

const misconception = computed(() => {
  const value = message.value?.content.target_misconception
  return typeof value === 'string' ? misconceptionLabel(value) : undefined
})
</script>

<template>
  <section class="panel learning-path">
    <header class="path-heading">
      <div>
        <span class="section-kicker">培养路径</span>
        <h2 v-if="summary">{{ summary }}</h2>
        <strong v-else class="path-current-stage">{{ currentStageLabel }}</strong>
      </div>
      <div class="path-progress-summary">
        <span>{{ progressPercent }}%</span>
        <small>{{ nextStep }}</small>
      </div>
    </header>

    <span v-if="tracePathSummary" class="visually-hidden">{{ tracePathSummary }}</span>
    <ol class="path-nodes" :aria-label="tracePathSummary || summary || '本轮培养路径'">
      <li
        v-for="node in nodes"
        :key="node.key"
        class="path-node"
        :class="`is-${node.status}`"
      >
        <span class="path-node-icon">
          <Route v-if="node.status === 'planned'" :size="15" aria-hidden="true" />
          <CircleDot v-else-if="node.status === 'current'" :size="16" aria-hidden="true" />
          <Check v-else :size="15" aria-hidden="true" />
        </span>
        <span v-if="node.isNext && plan" class="planned-node-copy">
          <strong>{{ node.label }}</strong>
          <small>下一步：{{ plan.knowledgePoint }} · {{ levelLabel[plan.difficulty] }}</small>
        </span>
        <span v-else>{{ node.label }}</span>
      </li>
    </ol>
    <aside v-if="misconception" class="achievement-card" aria-label="本轮培养成就">
      <Award :size="22" aria-hidden="true" />
      <div>
        <span>本轮达成</span>
        <strong>已修正 {{ misconception }}</strong>
      </div>
    </aside>
  </section>
</template>
