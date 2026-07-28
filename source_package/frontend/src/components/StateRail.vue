<script setup lang="ts">
import { computed } from 'vue'

import { STATE_ORDER, stateLabel } from '../lib/tracePresentation'
import type { StateId, TraceMessage } from '../types/trace'


const props = defineProps<{
  messages: TraceMessage[]
  currentState: string
}>()

const visited = computed(() => {
  const result = new Set<string>()
  for (const message of props.messages) {
    if (message.rejectedByBus) continue
    if (message.content.action === 'session_start' && typeof message.content.state === 'string') {
      result.add(message.content.state)
    }
    if (message.content.action === 'state_transition') {
      if (typeof message.content.from_state === 'string') result.add(message.content.from_state)
      if (typeof message.content.to_state === 'string') result.add(message.content.to_state)
    }
  }
  return result
})

const states = computed(() => {
  const values: string[] = [...STATE_ORDER]
  if (props.currentState === 'S_FAIL') values.push('S_FAIL')
  return values
})

function status(state: string): 'current' | 'visited' | 'future' {
  if (state === props.currentState) return 'current'
  return visited.value.has(state) ? 'visited' : 'future'
}
</script>

<template>
  <nav class="state-rail" aria-label="培养流程状态">
    <ol>
      <li
        v-for="state in states"
        :key="state"
        :class="`is-${status(state)}`"
        :aria-current="state === currentState ? 'step' : undefined"
      >
        <span class="state-dot" aria-hidden="true"></span>
        <span>{{ stateLabel(state as StateId) }}</span>
      </li>
    </ol>
  </nav>
</template>
