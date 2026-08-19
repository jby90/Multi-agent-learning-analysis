<script setup lang="ts">
import { Check, Circle, CircleDot, Route } from '@lucide/vue'
import { computed } from 'vue'

import type { InteractiveState } from '../lib/interactiveApi'


const props = defineProps<{ state?: InteractiveState }>()

/** 闭环四名实一致：当前知识点前测已验证（微课懒生成）时，第 3 步
 * 的真实动作是直入实操，步骤名随之切换。 */
const lectureDeferred = computed(() => {
  const value = props.state
  if (!value) return false
  const artifact = value.artifact as
    | { payload?: { content?: { knowledge_point_plan?: Array<{ tier?: string }> } } }
    | null
    | undefined
  const plan = artifact?.payload?.content?.knowledge_point_plan
  const first = Array.isArray(plan) ? plan[0] : undefined
  return first?.tier === 'correct'
})

const steps = computed(() => [
  '选择岗位',
  '完成岗前测评',
  lectureDeferred.value ? '直入实操（已验证）' : '打开岗位微课',
  '领取实操任务',
  '完成数据实操',
  '完成理解核对',
])

const currentIndex = computed(() => {
  const value = props.state
  if (!value) return 0
  if (value.awaiting === 'pretest') return 1
  if (value.state === 'S2_KNOWLEDGE') return 2
  if (value.state === 'S3_TASK') return 3
  if (value.awaiting === 'sql') return 4
  if (value.awaiting === 'follow_up' || value.state === 'S8_PROBE') return 5
  if (value.state === 'S9_PATH_UPDATE' || value.awaiting === 'done') return 5
  return 1
})

const currentLabel = computed(() => steps.value[currentIndex.value] ?? steps.value.at(-1)!)
const nextLabel = computed(() => steps.value[currentIndex.value + 1] ?? '完成本次训练')
</script>

<template>
  <section
    class="panel live-step-guide"
    data-testid="live-step-guide"
    :aria-label="`实操步骤指引，当前：${currentLabel}`"
  >
    <header>
      <span class="section-kicker">学习导航</span>
      <Route :size="19" aria-hidden="true" />
    </header>
    <h2>当前：{{ currentLabel }}</h2>
    <p class="live-guide-next">接下来：{{ nextLabel }}</p>

    <ol>
      <li
        v-for="(step, index) in steps"
        :key="step"
        :class="{
          'is-complete': index < currentIndex,
          'is-current': index === currentIndex,
        }"
      >
        <span>
          <Check v-if="index < currentIndex" :size="14" aria-hidden="true" />
          <CircleDot v-else-if="index === currentIndex" :size="15" aria-hidden="true" />
          <Circle v-else :size="13" aria-hidden="true" />
        </span>
        <strong>{{ step }}</strong>
      </li>
    </ol>
  </section>
</template>
