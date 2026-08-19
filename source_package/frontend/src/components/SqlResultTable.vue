<script setup lang="ts">
import { Code2, Database } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import {
  dataFieldLabel,
  learnerText,
  publicDisplayText,
  publicQueryText,
  verificationFailureCopy,
} from '../lib/tracePresentation'
import type { TraceMessage } from '../types/trace'


const props = defineProps<{
  message: TraceMessage
  /** live 实操模式：拦截说明已在实操区即时展示，结果面板不再重复正文。 */
  liveOperation?: boolean
}>()
const showSql = ref(false)
const focusMode = ref(false)

function leaveFocusMode(event: KeyboardEvent): void {
  if (event.key === 'Escape') focusMode.value = false
}

onMounted(() => window.addEventListener('keydown', leaveFocusMode))
onBeforeUnmount(() => window.removeEventListener('keydown', leaveFocusMode))

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

/** 需求⑥：结果副标题展示数据说明——去掉题干里的查询动作前缀。 */
const dataTitle = computed(() => {
  const raw = learnerText(String(props.message.content.question ?? '')).trim()
  const stripped = raw
    .replace(/^按[^，。；！？]*?(?:查询|比较|统计|核对|筛查|查看|计算)/, '')
    .replace(/^(?:查询|查看)/, '')
    .replace(/^[，。、；：\s]+/, '')
    .trim()
  return stripped || '查询结果'
})

const failureFeedback = computed(() => verificationFailureCopy(
  props.message.content.event,
  props.message.content,
))
const suppressFailureMessage = computed(() => (
  props.liveOperation && props.message.content.event === 'sandbox_rejected'
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
  <section
    class="sql-result"
    :class="{ 'is-focus-mode': focusMode }"
    aria-label="数据查询结果"
  >
    <div class="sql-result-heading">
      <div>
        <span class="resource-eyebrow"><Database :size="14" aria-hidden="true" /> 查询结果</span>
        <h3>{{ dataTitle }}</h3>
      </div>
    </div>

    <aside v-if="failureFeedback" class="panel-empty compact-empty query-failure">
      <strong>{{ failureFeedback.title }}</strong>
      <!-- 拦截说明的完整修改建议在实操区即时展示（sandbox-rejection），
           结果面板只报“发生了什么”，不再重复第三遍“怎么办”。 -->
      <p v-if="!suppressFailureMessage">{{ failureFeedback.learnerMessage }}</p>
      <p v-else class="query-failure-pointer">修改建议见实操区说明。</p>
    </aside>
    <div v-else-if="columns.length" class="table-scroll">
      <table>
        <thead>
          <tr>
            <th scope="col" class="row-no" aria-label="行号">#</th>
            <th v-for="column in columns" :key="column" scope="col">{{ dataFieldLabel(column) }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in rows" :key="rowIndex">
            <td class="row-no">{{ rowIndex + 1 }}</td>
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
