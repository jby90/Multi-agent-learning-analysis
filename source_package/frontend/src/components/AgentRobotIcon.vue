<script setup lang="ts">
import type { AgentId } from '../types/trace'


type CollaborationAgent = Exclude<AgentId, 'system'>

withDefaults(defineProps<{
  agent: CollaborationAgent
  size?: number
}>(), {
  size: 38,
})
</script>

<template>
  <svg
    class="agent-robot-icon"
    :data-robot-agent="agent"
    :width="size"
    :height="size"
    viewBox="0 0 48 48"
    fill="none"
    stroke="currentColor"
    stroke-width="1.8"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    focusable="false"
  >
    <g v-if="agent === 'diagnosis'" class="robot-glyph-diagnosis">
      <path d="M24 4v5" />
      <circle cx="24" cy="3.5" r="1.5" fill="currentColor" stroke="none" />
      <path d="M13 18c0-5.5 4.7-9 11-9s11 3.5 11 9v8H13z" fill="currentColor" fill-opacity="0.12" />
      <path d="M16 18h16" />
      <circle cx="20" cy="20.5" r="1.5" fill="currentColor" stroke="none" />
      <circle cx="28" cy="20.5" r="1.5" fill="currentColor" stroke="none" />
      <path d="M16 29 11 40h26l-5-11z" />
      <path d="M19 34h10M17 38h14" />
      <path d="M8 16v-3h4M40 16v-3h-4" />
    </g>

    <g v-else-if="agent === 'knowledge'" class="robot-glyph-knowledge">
      <path d="m24 6 13 7v18l-13 8-13-8V13z" fill="currentColor" fill-opacity="0.1" />
      <path d="M16 16c3.8 0 6.4 1.2 8 3.5 1.6-2.3 4.2-3.5 8-3.5v13c-3.8 0-6.4 1.1-8 3.2-1.6-2.1-4.2-3.2-8-3.2z" />
      <path d="M24 19.5v12.7" />
      <circle cx="8" cy="21" r="2" />
      <circle cx="40" cy="21" r="2" />
      <path d="M10 21h3M35 21h3M20 42h8" />
      <path d="M24 6V3" />
      <path d="m22 3 2-2 2 2" />
    </g>

    <g v-else-if="agent === 'task'" class="robot-glyph-task">
      <rect x="11" y="12" width="26" height="19" rx="2" fill="currentColor" fill-opacity="0.11" />
      <path d="M18 12V8h12v4M17 20h14M20 25h8" />
      <circle cx="18" cy="18" r="1.4" fill="currentColor" stroke="none" />
      <circle cx="30" cy="18" r="1.4" fill="currentColor" stroke="none" />
      <path d="M11 18H7l-3 4 3 4h4M37 18h4l3 4-3 4h-4" />
      <path d="M9 35h30l-3 7H12z" />
      <circle cx="17" cy="38.5" r="2" />
      <circle cx="24" cy="38.5" r="2" />
      <circle cx="31" cy="38.5" r="2" />
    </g>

    <g v-else-if="agent === 'verification'" class="robot-glyph-verification">
      <path d="m15 7-5 7v21l5 6h18l5-6V14l-5-7z" fill="currentColor" fill-opacity="0.1" />
      <rect x="15" y="13" width="18" height="14" rx="1.5" />
      <path d="m17.5 23 3.7-4.5 3.8 4 5.5-6" />
      <path d="M18 32h12M20 36h8" />
      <circle cx="16" cy="32" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="32" cy="32" r="1.3" fill="currentColor" stroke="none" />
      <path d="M10 18H6v10h4M38 18h4v10h-4" />
    </g>

    <g v-else class="robot-glyph-review">
      <path d="m24 5 14 6v12c0 9-5.3 15.2-14 20-8.7-4.8-14-11-14-20V11z" fill="currentColor" fill-opacity="0.1" />
      <path d="M17 14h14v10H17z" />
      <circle cx="21" cy="19" r="1.3" fill="currentColor" stroke="none" />
      <circle cx="27" cy="19" r="1.3" fill="currentColor" stroke="none" />
      <path d="m17 31 4 4 10-10" />
      <path d="M24 5V2M22 2h4" />
      <circle cx="8" cy="20" r="2" />
      <circle cx="40" cy="20" r="2" />
    </g>
  </svg>
</template>
