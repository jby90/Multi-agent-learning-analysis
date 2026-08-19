<script setup lang="ts">
import { computed } from 'vue'

import { learnerText } from '../lib/tracePresentation'
import type { TraceClaim, TraceEvidence } from '../types/trace'


interface Segment {
  text: string
  claim?: TraceClaim
  evidence: TraceEvidence[]
  marker?: number
}

const props = defineProps<{
  text: string
  claims: TraceClaim[]
  evidence: TraceEvidence[]
  knowledgePoint?: string
  matchesBlindSpot?: boolean
}>()

const segments = computed<Segment[]>(() => {
  const matches = props.claims
    .map((claim) => ({ claim, index: props.text.indexOf(claim.text) }))
    .filter((item) => item.index >= 0)
    .sort((left, right) => left.index - right.index)
  const result: Segment[] = []
  let cursor = 0
  let marker = 0
  for (const match of matches) {
    if (match.index < cursor) continue
    if (match.index > cursor) {
      result.push({ text: props.text.slice(cursor, match.index), evidence: [] })
    }
    const evidence = props.evidence.filter(
      (item) => item.supportsClaim === match.claim.text,
    )
    if (evidence.length) marker += 1
    result.push({
      text: match.claim.text,
      claim: match.claim,
      evidence,
      ...(evidence.length ? { marker } : {}),
    })
    cursor = match.index + match.claim.text.length
  }
  if (cursor < props.text.length) {
    result.push({ text: props.text.slice(cursor), evidence: [] })
  }
  return result.length ? result : [{ text: props.text, evidence: [] }]
})
</script>

<template>
  <span class="evidence-line">
    <template v-for="(segment, index) in segments" :key="`${index}-${segment.text}`">
      <!-- 需求②：证据句按普通文本呈现（无下划线/悬停/角标） -->
      <span v-if="segment.claim" class="claim-wrap">
        {{ learnerText(segment.text) }}
        <span
          v-if="segment.claim.kind === 'speculation'"
          class="speculation-badge"
        >推断</span>
      </span>
      <span v-else>{{ learnerText(segment.text) }}</span>
    </template>
  </span>
</template>
