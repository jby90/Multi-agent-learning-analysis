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

const nodes = computed(() => {
  const values: Array<{
    key: string
    label: string
    status: 'complete' | 'current' | 'planned'
  }> = completed.value.map((label) => ({
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
      </div>
      <Route :size="18" aria-hidden="true" />
    </header>

    <ol v-if="nodes.length" class="path-nodes">
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
        <span v-if="node.status === 'planned'" class="planned-node-copy">
          <strong>下一步：{{ node.label }}</strong>
          <small>{{ plan ? levelLabel[plan.difficulty] : '' }}</small>
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
