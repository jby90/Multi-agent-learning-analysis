<script setup lang="ts">
import { ChevronDown, ChevronUp } from '@lucide/vue'
import { computed, ref } from 'vue'

import {
  agentLabel,
  collaborationText,
  messageSummary,
  payloadTypeLabel,
  publicQueryText,
  roleLabel,
  ruleLabel,
  truthBadges,
} from '../lib/tracePresentation'
import type { TraceMessage } from '../types/trace'


defineOptions({ name: 'TraceCard' })

const props = defineProps<{ message: TraceMessage }>()
const expanded = ref(false)
const badges = computed(() => truthBadges(props.message))

interface RoutingDiagnostic {
  mismatch: boolean
}

const routingDiagnostic = computed<RoutingDiagnostic | undefined>(() => {
  if (props.message.payloadType !== 'sql_result') return undefined
  const predicted = props.message.content.routing_predicted_family
  const final = props.message.content.routing_final_family
  const mismatch = props.message.content.routing_family_mismatch
  if (
    typeof predicted !== 'string'
    || typeof final !== 'string'
    || typeof mismatch !== 'boolean'
  ) return undefined
  return { mismatch }
})

const timeLabel = computed(() => {
  const date = new Date(props.message.timestamp)
  if (Number.isNaN(date.getTime())) return '时间已记录'
  return new Intl.DateTimeFormat('zh-CN', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(date)
})

const generatedSql = computed(() => {
  const value = props.message.content.generated_sql
  return typeof value === 'string' && value.trim() !== ''
    ? publicQueryText(value)
    : undefined
})

const payloadLabel = computed(() => payloadTypeLabel(props.message.payloadType))

function busReason(): string {
  if (!props.message.busErrors.length) return '记录未通过完整性检查，培养流程保持原状态。'
  const joined = props.message.busErrors.join(' ')
  if (joined.includes('reserved sender fields')) return '记录包含不应出现的信息，已被安全检查拦下。'
  if (joined.includes('trace is closed')) return '本次训练已结束，后续记录未进入培养流程。'
  return '记录内容不完整，培养流程保持原状态。'
}

function evidenceText(quote: string | undefined): string {
  const text = quote?.trim()
  if (!text) return '证据原文已记录'
  if (
    text.startsWith('{') ||
    text.startsWith('[') ||
    /\b(?:expected_points|expected_rows|standard_sql)\b/i.test(text)
  ) {
    return '查询依据与预期结果已核对'
  }
  return collaborationText(text)
}
</script>

<template>
  <article
    class="trace-card"
    :class="[
      `role-${message.role}`,
      `agent-${message.agent}`,
      { 'is-rejected': message.rejectedByBus, 'is-expanded': expanded },
    ]"
  >
    <header class="trace-card-header">
      <div class="actor-block">
        <span class="agent-mark" aria-hidden="true"></span>
        <div>
          <strong>{{ agentLabel(message.agent) }}</strong>
          <span class="role-label">{{ roleLabel(message.role) }}</span>
        </div>
      </div>
      <div class="message-meta">
        <span v-for="badge in badges" :key="badge.label" :class="`truth-${badge.tone}`">
          {{ badge.label }}
        </span>
        <time>{{ timeLabel }}</time>
      </div>
    </header>

    <p class="message-summary">{{ messageSummary(message) }}</p>
    <section
      v-if="routingDiagnostic"
      class="routing-diagnosis"
      :class="routingDiagnostic.mismatch ? 'is-mismatch' : 'is-match'"
      :aria-label="routingDiagnostic.mismatch ? '问题判断需要调整' : '问题判断一致'"
    >
      <header>
        <span>问题判断</span>
        <strong>{{ routingDiagnostic.mismatch ? '需要调整' : '一致' }}</strong>
      </header>
      <p>
        {{ routingDiagnostic.mismatch
          ? '已按实际数据范围继续查询'
          : '查询范围已经核对' }}
      </p>
    </section>
    <p v-if="message.rejectedByBus" class="bus-reason">{{ busReason() }}</p>

    <button
      class="detail-toggle"
      type="button"
      :aria-expanded="expanded"
      :aria-label="expanded ? '收起审核记录' : '展开审核记录'"
      @click="expanded = !expanded"
    >
      <ChevronUp v-if="expanded" :size="15" aria-hidden="true" />
      <ChevronDown v-else :size="15" aria-hidden="true" />
      {{ expanded ? '收起记录' : '查看记录' }}
    </button>

    <section v-if="expanded" class="technical-detail">
      <dl class="technical-grid">
        <div>
          <dt>记录类型</dt>
          <dd>{{ payloadLabel }}</dd>
        </div>
      </dl>

      <div v-if="routingDiagnostic" class="detail-section routing-business-detail">
        <h4>查询判断记录</h4>
        <p>
          {{ routingDiagnostic.mismatch
            ? '系统已依据实际数据范围完成调整。'
            : '系统判断与实际数据范围一致。' }}
        </p>
      </div>

      <div v-if="message.verdict?.ruleHits.length" class="detail-section">
        <h4>审核依据</h4>
        <p v-for="hit in message.verdict.ruleHits" :key="`${hit.ruleId}-${hit.reason}`">
          <strong>{{ ruleLabel(hit.ruleId) }}</strong> · {{ collaborationText(hit.reason) }}
        </p>
      </div>

      <div v-if="message.evidence.length" class="detail-section">
        <h4>证据记录</h4>
        <p v-for="item in message.evidence" :key="`${item.ref}-${item.quote ?? ''}`">
          <span>{{ evidenceText(item.quote) }}</span>
        </p>
      </div>

      <div v-if="generatedSql" class="detail-section">
        <h4>{{ message.content.sql_source === 'student' ? '学员提交的查询' : '系统准备的查询' }}</h4>
        <pre>{{ generatedSql }}</pre>
      </div>
    </section>
  </article>
</template>
