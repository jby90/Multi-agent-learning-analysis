<script setup lang="ts">
import { Code2, Database } from '@lucide/vue'
import { computed, ref } from 'vue'

import {
  dataFieldLabel,
  learnerText,
  publicDisplayText,
  publicQueryText,
  verificationFailureCopy,
} from '../lib/tracePresentation'
import type { TraceMessage } from '../types/trace'


const props = defineProps<{ message: TraceMessage }>()
const showSql = ref(false)

const columns = computed(() => {
  const value = props.message.content.columns
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : []
})

const rows = computed<Record<string, unknown>[]>(() => {
  const value = props.message.content.rows
  return Array.isArray(value)
    ? value.filter(
      (item): item is Record<string, unknown> => typeof item === 'object'
        && item !== null
        && !Array.isArray(item),
    )
    : []
})

const generatedSql = computed(() => {
  const value = props.message.content.generated_sql
  return typeof value === 'string' && value.trim() !== ''
    ? publicQueryText(value)
    : undefined
})

const failureFeedback = computed(() => verificationFailureCopy(
  props.message.content.event,
  props.message.content,
))

const isStudentSql = computed(() => props.message.content.sql_source === 'student')
const sqlActionLabel = computed(() => {
  if (isStudentSql.value) return showSql.value ? '收起我提交的查询' : '查看我提交的查询'
  return showSql.value ? '收起系统准备的查询' : '查看系统准备的查询'
})

function cellValue(value: unknown): string {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'object') {
    let rawText = ''
    try {
      rawText = JSON.stringify(value)
    } catch {
      return '内容已隐藏'
    }
    return publicDisplayText(rawText, '内容已隐藏', '内容已隐藏')
  }
  const rawText = String(value)
  return publicDisplayText(rawText, rawText, '内容已隐藏')
}
</script>

<template>
  <section class="sql-result" aria-label="数据查询结果">
    <div class="sql-result-heading">
      <div>
        <span class="resource-eyebrow"><Database :size="14" aria-hidden="true" /> 数据实操</span>
        <h3>{{ learnerText(String(message.content.question ?? '查询结果')) }}</h3>
      </div>
      <span v-if="!failureFeedback" class="row-count">{{ rows.length }} 行</span>
    </div>

    <aside v-if="failureFeedback" class="panel-empty compact-empty query-failure">
      <strong>{{ failureFeedback.title }}</strong>
      <p>{{ failureFeedback.learnerMessage }}</p>
    </aside>
    <div v-else-if="columns.length" class="table-scroll">
      <table>
        <thead>
          <tr>
            <th v-for="column in columns" :key="column" scope="col">{{ dataFieldLabel(column) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in rows" :key="rowIndex">
            <td v-for="column in columns" :key="column">{{ cellValue(row[column]) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p v-else class="panel-empty compact-empty">本次查询没有返回可展示的列。</p>

    <button
      v-if="generatedSql"
      type="button"
      class="sql-toggle"
      :aria-expanded="showSql"
      :aria-label="sqlActionLabel"
      @click="showSql = !showSql"
    >
      <Code2 :size="15" aria-hidden="true" />
      {{ sqlActionLabel }}
    </button>
    <pre v-if="showSql && generatedSql" class="sql-code">{{ generatedSql }}</pre>
  </section>
</template>
