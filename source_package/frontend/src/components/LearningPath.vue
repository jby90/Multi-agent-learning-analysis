<script setup lang="ts">
import { Award, Check, CircleDot, Route } from '@lucide/vue'
import { computed } from 'vue'

import { difficultyJourney, nextLearningPlan } from '../lib/learningInsights'
import { learnerText, misconceptionLabel } from '../lib/tracePresentation'
import type { InteractiveState } from '../lib/interactiveApi'
import type { KnowledgeCatalogEntry, TraceView } from '../types/trace'


const props = defineProps<{
  view: TraceView
  catalog: KnowledgeCatalogEntry[]
  state?: InteractiveState
}>()

const message = computed(() => props.view.path)

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined
}

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
/* 最新任务消息可能没有难度字段（如追问类消息）；回溯最近的
   带难度的任务消息，保证头部档位在追问/重练阶段不消失。 */
/* 头部档位与难度轨迹面板同源：取轨迹最后一点（测评/微课/实操/
   验证/进阶的权威档），学员看到的头部档位与轨迹完全一致。 */
const journeyDifficulty = computed(
  () => difficultyJourney(props.view, props.catalog).points.at(-1)?.level,
)

const lastTaskDifficulty = computed(() => {
  // 回退链：任务消息 → 微课消息。追问类消息没有难度字段；首个任务
  // 出现前微课已带档位，保证"训练中"从微课帧起就有档位显示。
  for (const payloadType of ['quiz_set', 'practice_guide', 'lecture_note']) {
    for (const message of [...props.view.visibleMessages].reverse()) {
      if (
        message.payloadType === payloadType
        && stringValue(message.content?.difficulty)
      ) {
        return stringValue(message.content?.difficulty)
      }
    }
  }
  return undefined
})

/* 头部用结构化短语（知识点·状态），不用口语化长句——叙事交给
   "已修正"徽章、下一步小字与训练报告页。 */
const summary = computed(() => {
  const point = stringValue(props.view.lecture?.content.knowledge_point)
    ?? stringValue(props.view.task?.content.knowledge_point)
  if (!point) return undefined
  const finished = props.view.path?.content.learning_goal_achieved === true
    || props.view.currentState === 'S10_DONE'
  const tier = {
    basic: '基础档',
    applied: '应用档',
    advanced: '进阶档',
  }[journeyDifficulty.value ?? lastTaskDifficulty.value ?? '']
  const tierText = tier && !finished ? ` · ${tier}` : ''
  return `当前：${point}${tierText} · ${finished ? '已完成' : '训练中'}`
})

function record(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? value as Record<string, unknown>
    : undefined
}

const liveKnowledgePoint = computed(() => {
  const state = props.state
  if (!state) return undefined
  if (
    state.awaiting === 'diagnostic_probe'
    && state.interaction?.kind === 'supplemental_diagnosis'
  ) {
    const provisional = state.interaction.provisional_route?.knowledge_point
    if (provisional?.trim()) return learnerText(provisional)
  }
  const artifact = record(state.artifact)
  const payload = record(artifact?.payload)
  const content = record(payload?.content)
  const artifactPoint = content?.knowledge_point ?? content?.selected_knowledge_point
  if (typeof artifactPoint === 'string' && artifactPoint.trim()) {
    return learnerText(artifactPoint)
  }
  if (state.training_report?.knowledge_point) {
    return learnerText(state.training_report.knowledge_point)
  }
  const interaction = state.interaction
  if (interaction?.kind === 'diagnostic_route' && interaction.knowledge_point) {
    return learnerText(interaction.knowledge_point)
  }
  const contractPoint = state.learning_contract?.target_knowledge_points?.[0]
  return contractPoint ? learnerText(contractPoint) : undefined
})

const liveDifficulty = computed(() => {
  const state = props.state
  if (!state) return undefined
  const artifact = record(state.artifact)
  const content = record(record(artifact?.payload)?.content)
  const value = content?.difficulty ?? state.current_difficulty
  return typeof value === 'string' ? value : undefined
})

const effectivePlan = computed(() => props.state ? undefined : plan.value)
const displaySummary = computed(() => liveKnowledgePoint.value
  ? `当前训练：${liveKnowledgePoint.value}`
  : summary.value)


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
  if (effectivePlan.value) {
    values.push({
      key: `planned-${effectivePlan.value.knowledgePoint}`,
      label: effectivePlan.value.knowledgePoint,
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
  if (effectivePlan.value) {
    const nextStage = stages.find((stage) => stage.status === 'planned')
    if (nextStage) {
      nextStage.isNext = true
    } else {
      stages.push({
        key: `next-focus-${effectivePlan.value.knowledgePoint}`,
        label: '后续重点',
        status: 'planned',
        isNext: true,
      })
    }
  }
  return stages
})

const nextStep = computed(() => {
  if (liveKnowledgePoint.value) {
    const difficulty = liveDifficulty.value && levelLabel[liveDifficulty.value as keyof typeof levelLabel]
    const point = difficulty ? `${liveKnowledgePoint.value} · ${difficulty}` : liveKnowledgePoint.value
    return `当前：${point}`
  }
  if (effectivePlan.value) {
    // 尚无微课/任务消息 = 第一个单元还没开始，此时列出的第一个
    // 盲区是"即将开始"的当前单元，不是"下一"个。
    if (!displaySummary.value) {
      return `即将开始：${effectivePlan.value.knowledgePoint}`
    }
    // 当前单元训练期间，这里报当前环节；下一知识点只在橙色
    // 节点标签出现一处，避免同屏重复两遍。
    return `当前环节：${currentStageLabel.value}`
  }
  const next = stageLabels[Math.min(currentStageIndex.value + 1, stageLabels.length - 1)]
  return currentStageIndex.value >= stageLabels.length ? '保持并巩固本轮成果' : `下一环节：${next}`
})

/* Keep the trace-derived path available to accessibility and future exports;
   the visible strip uses a stable five-stage vocabulary so sparse early traces
   never produce an empty oversized card. */
const tracePathSummary = computed(() => traceNodes.value.map((item) => item.label).join('、'))
const pathAriaLabel = computed(() => liveKnowledgePoint.value
  ? `当前训练：${liveKnowledgePoint.value}`
  : tracePathSummary.value || displaySummary.value || '本轮培养路径')

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
        <!-- 优化11：删"下一知识点：xxx · 档"小字 -->
        <h2 v-if="displaySummary">
          {{ displaySummary }}
        </h2>
        <strong v-else class="path-current-stage">{{ currentStageLabel }}</strong>
      </div>
      <div class="path-progress-summary">
        <span>{{ progressPercent }}%</span>
        <small>{{ nextStep }}</small>
      </div>
    </header>

    <span v-if="!state && tracePathSummary" class="visually-hidden">{{ tracePathSummary }}</span>
    <ol class="path-nodes" :aria-label="pathAriaLabel">
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
        <span>{{ node.label }}</span>
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
