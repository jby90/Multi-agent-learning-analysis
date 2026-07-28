<script setup lang="ts">
import { MessagesSquare } from '@lucide/vue'

import type { DebateGroup, TraceMessage } from '../types/trace'
import TraceCard from './TraceCard.vue'


defineProps<{ group: DebateGroup }>()

function phaseLabel(message: TraceMessage): string {
  if (message.role === 'verdict') {
    return message.verdict?.decision === 'reject' ? '驳回' : '初次审核'
  }
  if (message.role === 'rebuttal') return '补充说明'
  if (message.role === 're_verdict') return '再次审核'
  return '复审记录'
}

function phaseTone(message: TraceMessage): string {
  if (message.role === 'verdict' && message.verdict?.decision === 'reject') return 'is-reject'
  if (message.role === 'rebuttal') return 'is-rebuttal'
  if (message.role === 're_verdict') return 'is-re-review'
  return 'is-neutral'
}
</script>

<template>
  <section class="debate-group" data-testid="debate-group" aria-label="复审过程">
    <header class="debate-heading">
      <span class="debate-icon"><MessagesSquare :size="16" aria-hidden="true" /></span>
      <div>
        <strong>复审过程</strong>
      </div>
    </header>
    <div class="debate-cards">
      <section
        v-for="message in group.messages"
        :key="message.msgId"
        class="debate-stage"
        :class="phaseTone(message)"
      >
        <span class="debate-phase">{{ phaseLabel(message) }}</span>
        <TraceCard :message="message" />
      </section>
    </div>
  </section>
</template>
