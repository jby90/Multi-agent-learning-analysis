<script setup lang="ts">
import { ArrowLeft, CircleAlert, RefreshCw, ShieldCheck } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import {
  createInteractiveApi,
  type AgentActivityEvent,
  type InteractiveApi,
  type InteractiveState,
} from '../lib/interactiveApi'
import { buildTraceView } from '../lib/traceModel'
import { parseTraceJsonl } from '../lib/traceParser'
import AgentTopology from './AgentTopology.vue'

const SESSION_KEY = 'ref-interactive-session'
const TRACE_KEY = 'ref-interactive-trace'
const props = withDefaults(defineProps<{
  api?: InteractiveApi
  pollIntervalMs?: number
}>(), {
  pollIntervalMs: 1500,
})
const api = props.api ?? createInteractiveApi()
const state = ref<InteractiveState>()
const events = ref<AgentActivityEvent[]>([])
const loading = ref(true)
const error = ref('')
let closeEvents: (() => void) | undefined
let pollTimer: number | undefined

const document = computed(() => {
  const current = state.value
  if (!current?.messages.length) return undefined
  try {
    return parseTraceJsonl(
      current.messages.map((message) => JSON.stringify(message)).join('\n'),
      `${current.trace_id}.jsonl`,
    )
  } catch {
    return undefined
  }
})

const view = computed(() => document.value
  ? buildTraceView(document.value, document.value.messages.length)
  : undefined)

const sessionLabel = computed(() => state.value?.session_id.slice(-8) ?? '--------')
const traceLabel = computed(() => state.value?.trace_id.slice(-12) ?? '------------')

function backToTraining(): void {
  const url = new URL(window.location.href)
  url.searchParams.delete('view')
  window.location.assign(`${url.pathname}${url.search}${url.hash}`)
}

function receiveEvent(event: AgentActivityEvent): void {
  const current = state.value
  if (!current || event.trace_id !== current.trace_id) return
  const index = events.value.findIndex((item) => item.sequence === event.sequence)
  if (index >= 0) events.value[index] = event
  else events.value.push(event)
  events.value.sort((left, right) => left.sequence - right.sequence)
  if (events.value.length > 500) events.value.splice(0, events.value.length - 500)
}

async function refresh(): Promise<void> {
  const sessionId = sessionStorage.getItem(SESSION_KEY)
  if (!sessionId) {
    state.value = undefined
    error.value = '当前浏览器没有进行中的训练会话。请先返回训练页并选择岗位。'
    loading.value = false
    return
  }
  try {
    const next = await api.getState(sessionId)
    if (next.session_id !== sessionId) throw new Error('session binding mismatch')
    const boundTrace = sessionStorage.getItem(TRACE_KEY)
    if (boundTrace && boundTrace !== next.trace_id) throw new Error('trace binding mismatch')
    sessionStorage.setItem(TRACE_KEY, next.trace_id)
    state.value = next
    error.value = ''
    if (!closeEvents && api.subscribeAgentEvents) {
      closeEvents = api.subscribeAgentEvents(sessionId, receiveEvent)
    }
  } catch {
    error.value = '当前会话已失效或不属于这个浏览器上下文，请返回训练页重新开始。'
  } finally {
    loading.value = false
  }
}

onMounted(() => {
  void refresh()
  if (props.pollIntervalMs > 0) {
    pollTimer = window.setInterval(() => void refresh(), props.pollIntervalMs)
  }
})

onBeforeUnmount(() => {
  closeEvents?.()
  if (pollTimer !== undefined) window.clearInterval(pollTimer)
})
</script>

<template>
  <main class="debug-workspace" aria-label="当前会话调试工作台">
    <header class="debug-toolbar">
      <div class="debug-title">
        <span class="debug-mark">FLOW</span>
        <div>
          <small>SESSION-SCOPED OBSERVABILITY</small>
          <h1>智能体调试工作台</h1>
        </div>
      </div>
      <div v-if="state" class="debug-binding" aria-label="当前调试会话绑定">
        <ShieldCheck :size="17" aria-hidden="true" />
        <span><small>当前浏览器会话</small><b>{{ sessionLabel }}</b></span>
        <span><small>事件轨迹</small><b>{{ traceLabel }}</b></span>
        <span><small>运行状态</small><b>{{ state.state }} · {{ state.awaiting }}</b></span>
      </div>
      <div class="debug-actions">
        <button type="button" aria-label="刷新当前会话" @click="refresh">
          <RefreshCw :size="16" aria-hidden="true" />刷新
        </button>
        <button type="button" class="is-primary" aria-label="返回训练页面" @click="backToTraining">
          <ArrowLeft :size="16" aria-hidden="true" />返回训练
        </button>
      </div>
    </header>

    <section v-if="loading" class="debug-empty" aria-live="polite">
      <RefreshCw class="debug-loading" :size="28" aria-hidden="true" />
      <strong>正在绑定当前会话</strong>
      <p>只读取这个浏览器持有的会话标识，不扫描服务器上的其他会话。</p>
    </section>
    <section v-else-if="error || !view" class="debug-empty is-error" role="alert">
      <CircleAlert :size="30" aria-hidden="true" />
      <strong>无法打开调试工作台</strong>
      <p>{{ error || '当前会话尚未产生可显示的运行轨迹。' }}</p>
      <button type="button" @click="backToTraining">返回训练页面</button>
    </section>
    <section v-else class="debug-topology-shell">
      <AgentTopology :view="view" :events="events" debug-mode />
    </section>

    <footer class="debug-security-note">
      本页不提供服务器会话列表，不从 URL 接收会话编号；节点日志仅展示脱敏后的业务事件，不展示密钥、数据库凭据、系统提示词或模型内部推理。
    </footer>
  </main>
</template>

<style scoped>
.debug-workspace { min-height:100vh; display:grid; grid-template-rows:auto minmax(0,1fr) auto; gap:12px; padding:18px; color:#dff5ff; background:radial-gradient(circle at 50% 0,rgba(28,125,176,.16),transparent 38%),#04111d; }
.debug-toolbar { min-width:0; display:grid; grid-template-columns:auto minmax(0,1fr) auto; align-items:center; gap:18px; padding:12px 16px; background:rgba(8,29,45,.92); border:1px solid rgba(108,205,245,.15); border-radius:16px; box-shadow:0 18px 54px rgba(0,0,0,.22); }
.debug-title { display:flex; align-items:center; gap:11px; }.debug-mark { width:44px; height:44px; display:grid; place-items:center; color:#071622; font:900 10px/1 ui-monospace,Consolas,monospace; background:#58dbff; border-radius:12px; box-shadow:0 0 24px rgba(65,211,250,.28); }
.debug-title small { display:block; color:#5cbdda; font:700 8px/1.2 ui-monospace,Consolas,monospace; letter-spacing:.12em; }.debug-title h1 { margin:3px 0 0; color:#f5fbff; font-size:18px; }
.debug-binding { min-width:0; justify-self:end; display:flex; align-items:center; gap:13px; padding:8px 12px; color:#50dda9; background:rgba(63,221,166,.06); border:1px solid rgba(74,222,173,.14); border-radius:11px; }.debug-binding span { min-width:0; display:grid; gap:2px; }.debug-binding small { color:#658fa2; font-size:8px; }.debug-binding b { color:#cce6ef; font:600 9px/1.2 ui-monospace,Consolas,monospace; }
.debug-actions { display:flex; gap:7px; }.debug-actions button,.debug-empty button { display:inline-flex; align-items:center; justify-content:center; gap:6px; padding:8px 11px; color:#bcd6e1; background:rgba(255,255,255,.04); border:1px solid rgba(135,204,233,.16); border-radius:9px; cursor:pointer; }.debug-actions button.is-primary { color:#052031; background:#5bdcff; border-color:#5bdcff; }
.debug-topology-shell { min-height:680px; overflow:hidden; border:1px solid rgba(107,203,242,.15); border-radius:18px; box-shadow:0 24px 80px rgba(0,0,0,.28); }.debug-topology-shell :deep(.agent-topology) { min-height:680px; }
.debug-empty { min-height:520px; display:grid; place-items:center; align-content:center; gap:12px; text-align:center; background:rgba(8,28,43,.82); border:1px solid rgba(113,196,231,.13); border-radius:18px; }.debug-empty strong { color:#fff; font-size:20px; }.debug-empty p { max-width:600px; margin:0; color:#7ea4b5; }.debug-empty.is-error svg { color:#ff6877; }.debug-loading { color:#56d9ff; animation:debug-spin 1.2s linear infinite; }
.debug-security-note { padding:8px 12px; color:#607f8e; font-size:10px; text-align:center; }
@keyframes debug-spin { to { transform:rotate(360deg); } }
@media (max-width:1000px) { .debug-toolbar { grid-template-columns:1fr auto; }.debug-binding { grid-column:1/-1; grid-row:2; justify-self:stretch; justify-content:flex-start; }.debug-topology-shell { min-height:760px; } }
</style>
