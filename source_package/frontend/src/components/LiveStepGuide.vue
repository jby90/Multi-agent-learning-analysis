<script setup lang="ts">
import { Check, Circle, CircleDot, Route } from '@lucide/vue'
import { computed } from 'vue'

import type { InteractiveState } from '../lib/interactiveApi'


const props = defineProps<{ state?: InteractiveState }>()

const steps = [
  '选择岗位',
  '完成岗前测评',
  '打开岗位微课',
  '领取实操任务',
  '完成数据实操',
  '完成理解核对',
]

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

const currentLabel = computed(() => steps[currentIndex.value] ?? steps.at(-1)!)
const nextLabel = computed(() => steps[currentIndex.value + 1] ?? '完成本次训练')
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
