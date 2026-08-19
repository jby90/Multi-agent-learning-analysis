<script setup lang="ts">
import { computed } from 'vue'

import { difficultyJourney, type DifficultyPoint } from '../lib/learningInsights'
import type { DifficultyLevel, KnowledgeCatalogEntry, TraceView } from '../types/trace'


const props = defineProps<{
  view: TraceView
  catalog: KnowledgeCatalogEntry[]
  currentDifficulty?: string | null
}>()

const journey = computed(() => difficultyJourney(
  props.view,
  props.catalog,
  props.currentDifficulty,
))
const stageX: Record<DifficultyPoint['stage'], number> = {
  assessment: 42,
  lecture: 91,
  practice: 140,
  validation: 189,
  advanced: 238,
}
const levelY: Record<DifficultyLevel, number> = {
  basic: 108,
  applied: 70,
  advanced: 32,
}
const levelLabel: Record<DifficultyLevel, string> = {
  basic: '基础',
  applied: '应用',
  advanced: '进阶',
}

const polyline = computed(() => journey.value.points.map(
  (point) => `${stageX[point.stage]},${levelY[point.level]}`,
).join(' '))

const accessibleLabel = computed(() => `资源难度轨迹：${journey.value.points.map(
  (point) => `${point.label}${levelLabel[point.level]}`,
).join('，')}`)

const difficultyStatus = computed(() => {
  const action = props.view.path?.content.difficulty_action
  if (action === 'step_up') return { label: '难度已提升', tone: 'step-up' }
  if (action === 'step_down') return { label: '已完成补充讲解', tone: 'step-down' }
  if (action === 'keep') return { label: '当前难度保持', tone: 'keep' }
  return undefined
})

function actionLabel(point: DifficultyPoint): string | undefined {
  if (point.action === 'step_up') return '提升'
  if (point.action === 'step_down') return '降低'
  return undefined
}
</script>

<template>
  <section
    v-if="journey.points.length"
    class="difficulty-journey"
    data-testid="difficulty-journey"
    role="img"
    :aria-label="accessibleLabel"
  >
    <header>
      <div>
        <strong>难度轨迹</strong>
      </div>
      <div class="difficulty-journey-status">
        <!-- 需求⑨：图例改为正方形 -->
        <span class="current-resource-legend">■ 当前学习内容</span>
        <span
          v-if="difficultyStatus"
          class="journey-status-note"
          :class="difficultyStatus.tone"
        >{{ difficultyStatus.label }}</span>
      </div>
    </header>

    <svg viewBox="0 0 276 142" aria-hidden="true">
      <g class="difficulty-grid">
        <line v-for="y in [32, 70, 108]" :key="y" x1="38" :y1="y" x2="248" :y2="y" />
        <line v-for="x in [42, 91, 140, 189, 238]" :key="x" :x1="x" y1="26" :x2="x" y2="113" />
      </g>
      <g class="difficulty-y-labels">
        <text x="4" y="36">进阶</text>
        <text x="4" y="74">应用</text>
        <text x="4" y="112">基础</text>
      </g>
      <polyline v-if="journey.points.length > 1" class="difficulty-line" :points="polyline" />
      <g
        v-for="(point, index) in journey.points"
        :key="point.stage"
        class="difficulty-point"
      >
        <rect
          v-if="index === journey.currentResourceIndex"
          class="current-resource-point"
          :x="stageX[point.stage] - 5"
          :y="levelY[point.level] - 5"
          width="10"
          height="10"
          rx="1"
        />
        <circle
          v-else
          :cx="stageX[point.stage]"
          :cy="levelY[point.level]"
          r="4"
        />
        <text
          v-if="actionLabel(point)"
          class="journey-action"
          :x="stageX[point.stage]"
          :y="levelY[point.level] - 10"
          text-anchor="middle"
        >{{ actionLabel(point) }}</text>
      </g>
      <g class="difficulty-x-labels">
        <text
          v-for="point in journey.points"
          :key="point.stage"
          :x="stageX[point.stage]"
          y="134"
          text-anchor="middle"
        >{{ point.label }}</text>
      </g>
    </svg>
  </section>
</template>
