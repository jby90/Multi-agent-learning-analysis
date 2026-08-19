<script setup lang="ts">
import { ArrowRight, X } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { learnerText, misconceptionLabel } from '../lib/tracePresentation'

const props = defineProps<{
  misconception: string
  wrongLabel: string
  wrongValue: string
  correctLabel: string
  correctValue: string
}>()

const emit = defineEmits<{ complete: [] }>()
const stage = ref<'mistaken' | 'verified'>('mistaken')
const visible = ref(true)
const safeWrongLabel = computed(() => learnerText(props.wrongLabel))
const safeCorrectLabel = computed(() => learnerText(props.correctLabel))
const safeWrongValue = computed(() => publicNumericValue(props.wrongValue))
const safeCorrectValue = computed(() => publicNumericValue(props.correctValue))
const correctDigits = computed(
  () => stage.value === 'verified' ? [...safeCorrectValue.value] : [],
)
const correctionLabel = computed(
  () => learnerText(misconceptionLabel(props.misconception)),
)
const timers: number[] = []
let finished = false

function publicNumericValue(value: string): string {
  const normalized = value.normalize('NFKC').trim()
  return /^[-+]?\d+(?:\.\d+)?$/u.test(normalized) ? normalized : '—'
}

function finish(): void {
  if (finished) return
  finished = true
  for (const timer of timers) window.clearTimeout(timer)
  visible.value = false
  emit('complete')
}

onMounted(() => {
  // 只做"误认→证实"的阶段切换；关闭必须由学员手动点，避免没看清就消失。
  timers.push(window.setTimeout(() => {
    stage.value = 'verified'
  }, 700))
})

onBeforeUnmount(() => {
  for (const timer of timers) window.clearTimeout(timer)
})
</script>

<template>
  <div
    v-if="visible"
    class="collision-backdrop"
    role="status"
    aria-live="polite"
  >
    <section
      class="data-collision-moment"
      data-testid="collision-moment"
      :data-stage="stage"
    >
      <button
        type="button"
        class="collision-skip"
        aria-label="关闭数据验证"
        @click="finish"
      >
        关闭 <X :size="15" aria-hidden="true" />
      </button>

      <header class="collision-heading">
        <h2>数据验证</h2>
        <p>同一批生产记录，两种口径给出了不同答案。</p>
      </header>

      <div class="collision-comparison">
        <article
          class="collision-number is-mistaken"
          :class="{ 'is-struck': stage === 'verified' }"
        >
          <span class="collision-flag">刚才误认</span>
          <small>{{ safeWrongLabel }}</small>
          <strong>{{ safeWrongValue }}</strong>
        </article>

        <ArrowRight class="collision-arrow" :size="24" aria-hidden="true" />

        <article
          v-if="stage !== 'verified'"
          class="collision-number is-checking"
        >
          <span class="collision-flag">正在核对</span>
          <small>车间报工</small>
        </article>
        <article v-else class="collision-number is-verified">
          <span class="collision-flag is-confirmed">数据证实</span>
          <small>{{ safeCorrectLabel }}</small>
          <strong class="digit-readout" :aria-label="safeCorrectValue">
            <span
              v-for="(digit, index) in correctDigits"
              :key="`${index}-${digit}`"
              class="digit-tile"
              aria-hidden="true"
            >{{ digit }}</span>
          </strong>
        </article>
      </div>

      <p class="collision-correction" :class="{ 'is-visible': stage === 'verified' }">
        已修正 {{ correctionLabel }} · 计划量与实际完成量不再混用
      </p>
      <p class="collision-verdict" :class="{ 'is-visible': stage === 'verified' }">
        计划是“应该做多少”，实际是“真正做了多少”——不能混用。
      </p>
    </section>
  </div>
</template>
