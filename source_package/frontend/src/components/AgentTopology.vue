<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type {
  AgentActivityEvent,
  AgentActivityId,
  AgentActivityStatus,
} from '../lib/interactiveApi'
import type { StateId, TraceMessage, TraceView } from '../types/trace'


type SystemNodeId = 'orchestrator' | 'state_machine' | 'review_arbiter' | 'query_sandbox'
type TopologyNodeId = AgentActivityId | SystemNodeId
type NodeKind = 'agent' | 'control' | 'service'

type TopologyNode = {
  id: TopologyNodeId
  label: string
  short: string
  kind: NodeKind
  x: number
  y: number
  module: string
  className: string
  responsibility: string
}

type NodeRuntimeConfig = {
  model: string
  strategy: string
  parameters: readonly string[]
}

type TopologyEdge = {
  from: TopologyNodeId
  to: TopologyNodeId
  label?: string
}

const props = withDefaults(defineProps<{
  view: TraceView
  events?: AgentActivityEvent[]
  debugMode?: boolean
}>(), {
  events: () => [],
  debugMode: false,
})

const nodes: readonly TopologyNode[] = [
  {
    id: 'diagnosis', label: '学情诊断', short: 'DIAG', kind: 'agent', x: 13, y: 20,
    module: 'agents/diagnosis_agent.py', className: 'DiagnosisAgent',
    responsibility: '识别岗位知识盲区，形成可执行的个性化学习起点。',
  },
  {
    id: 'knowledge', label: '领域知识', short: 'KNOW', kind: 'agent', x: 37, y: 13,
    module: 'agents/knowledge_agent.py', className: 'KnowledgeAgent',
    responsibility: '绑定领域证据，组织岗位微课与知识解释。',
  },
  {
    id: 'assessment', label: '分阶测验分支', short: 'TEST', kind: 'service', x: 13, y: 46,
    module: 'agents/task_agent.py', className: 'TaskAgent.generate_assessment',
    responsibility: '按学习契约生成与当前难度匹配的分阶测验。',
  },
  {
    id: 'task', label: '实操任务', short: 'TASK', kind: 'agent', x: 13, y: 73,
    module: 'agents/task_agent.py', className: 'TaskAgent',
    responsibility: '生成贴近岗位场景的数据实操任务与引导。',
  },
  {
    id: 'verification', label: '数据验证', short: 'VERIFY', kind: 'agent', x: 37, y: 82,
    module: 'agents/verification_agent.py', className: 'VerificationAgent',
    responsibility: '依据查询结果验证结论，约束事实与数据口径。',
  },
  {
    id: 'orchestrator', label: '会话编排器', short: 'FLOW', kind: 'control', x: 49, y: 47,
    module: 'orchestrator/interactive_session.py', className: 'InteractiveSessionManager',
    responsibility: '执行有序主链、局部并行派发、汇聚与安全收尾。',
  },
  {
    id: 'state_machine', label: '状态机', short: 'STATE', kind: 'control', x: 49, y: 70,
    module: 'orchestrator/engine.py', className: 'OrchestratorEngine',
    responsibility: '约束合法状态转移，阻止越级和不确定跳转。',
  },
  {
    id: 'query_sandbox', label: '查询沙箱', short: 'SQL', kind: 'service', x: 63, y: 82,
    module: 'agents/sandbox.py', className: 'ReadOnlyExecutor',
    responsibility: '校验只读查询与授权边界，并安全执行数据实操。',
  },
  {
    id: 'review', label: '专业审核', short: 'REVIEW', kind: 'agent', x: 70, y: 25,
    module: 'agents/review_agent.py', className: 'ReviewAgent',
    responsibility: '调度专项审核，根据规则性质执行放行、辩护或局部重生成。',
  },
  {
    id: 'review_arbiter', label: '确定性仲裁', short: 'JOIN', kind: 'control', x: 70, y: 55,
    module: 'agents/review_arbiter.py', className: 'DeterministicReviewArbiter',
    responsibility: '汇聚四路审核结果，按照固定规则形成唯一裁决。',
  },
  {
    id: 'evidence_review', label: '证据审核', short: 'R-02', kind: 'agent', x: 89, y: 13,
    module: 'agents/evidence_review_agent.py', className: 'EvidenceReviewAgent',
    responsibility: '检查事实主张、证据引用和证据边界是否对应。',
  },
  {
    id: 'pedagogy_review', label: '教学适配', short: 'R-03', kind: 'agent', x: 89, y: 32,
    module: 'agents/pedagogy_review_agent.py', className: 'PedagogyReviewAgent',
    responsibility: '检查任务难度与岗位画像、学习契约是否匹配。',
  },
  {
    id: 'data_safety_review', label: '数据安全', short: 'R-05', kind: 'agent', x: 89, y: 51,
    module: 'agents/deterministic_review_agents.py', className: 'DataSafetyReviewAgent',
    responsibility: '确定性检查数据边界、查询安全与敏感信息风险。',
  },
  {
    id: 'readability_review', label: '表达校阅', short: 'R-06', kind: 'agent', x: 89, y: 70,
    module: 'agents/deterministic_review_agents.py', className: 'ReadabilityReviewAgent',
    responsibility: '确定性检查表达结构、可读性与工程术语泄漏。',
  },
] as const

const edges: readonly TopologyEdge[] = [
  { from: 'state_machine', to: 'orchestrator', label: '状态约束' },
  { from: 'orchestrator', to: 'diagnosis' },
  { from: 'orchestrator', to: 'knowledge' },
  { from: 'orchestrator', to: 'assessment' },
  { from: 'orchestrator', to: 'task' },
  { from: 'orchestrator', to: 'verification' },
  { from: 'orchestrator', to: 'review' },
  { from: 'verification', to: 'query_sandbox', label: '安全查询' },
  { from: 'review', to: 'evidence_review' },
  { from: 'review', to: 'pedagogy_review' },
  { from: 'review', to: 'data_safety_review' },
  { from: 'review', to: 'readability_review' },
  { from: 'evidence_review', to: 'review_arbiter' },
  { from: 'pedagogy_review', to: 'review_arbiter' },
  { from: 'data_safety_review', to: 'review_arbiter' },
  { from: 'readability_review', to: 'review_arbiter' },
  { from: 'review_arbiter', to: 'review', label: '确定性裁决' },
] as const

const runtimeConfig: Record<TopologyNodeId, NodeRuntimeConfig> = {
  diagnosis: {
    model: 'Qwen3-32B + 确定性评分',
    strategy: '混合执行',
    parameters: ['输入：岗位画像与岗前测评', '输出：盲区、初始难度、学习契约'],
  },
  knowledge: {
    model: 'Qwen3-235B-A22B',
    strategy: 'BM25 检索增强生成',
    parameters: ['只使用当前领域证据包', '产物必须绑定引用并进入 Review'],
  },
  assessment: {
    model: 'Qwen3-235B-A22B + 题库约束',
    strategy: '资源并行分支',
    parameters: ['难度来自 LearningContract', '与微课、实操共享证据边界'],
  },
  task: {
    model: 'Qwen3-235B-A22B',
    strategy: '目录选题 + 受控情境化',
    parameters: ['输出查询权限与完成标准', '不得扩大授权表、字段或口径'],
  },
  verification: {
    model: '主链不依赖 LLM',
    strategy: '确定性验证',
    parameters: ['SQLGlot 校验与改写', '真实只读查询结果作为事实源'],
  },
  orchestrator: {
    model: '无 LLM',
    strategy: '有序主链 + 局部并行 DAG',
    parameters: ['证据/资源阶段最大并发：3', '会话以 session_id 与 trace_id 隔离'],
  },
  state_machine: {
    model: '无 LLM',
    strategy: '确定性状态机',
    parameters: ['固定 21 条状态转移', '非法越级与未审产物默认阻断'],
  },
  query_sandbox: {
    model: '无 LLM',
    strategy: '只读安全执行',
    parameters: ['仅允许授权查询', '拦截写操作、越权字段与危险结构'],
  },
  review: {
    model: 'Qwen3-32B + 确定性规则',
    strategy: '四路专项审核并行汇聚',
    parameters: ['语义审核受并发预算约束', '拒绝后仅允许受控辩护或局部重生成'],
  },
  review_arbiter: {
    model: '无 LLM',
    strategy: '确定性汇聚裁决',
    parameters: ['输出：通过 / 带修正通过 / 拒绝', '任一硬规则失败时 fail-closed'],
  },
  evidence_review: {
    model: 'Qwen3-32B + 证据规则',
    strategy: '事实与引用审核',
    parameters: ['检查主张—引用对应关系', '数值必须可由查询证据复算'],
  },
  pedagogy_review: {
    model: 'Qwen3-32B',
    strategy: '教学适配审核',
    parameters: ['检查岗位责任范围', '检查难度、误区与学习目标一致性'],
  },
  data_safety_review: {
    model: '无 LLM',
    strategy: '确定性硬规则',
    parameters: ['检查数据口径与查询安全', '不接受模型对硬规则的辩护覆盖'],
  },
  readability_review: {
    model: '无 LLM',
    strategy: '确定性表达校阅',
    parameters: ['阻止内部协议术语泄漏', '检查严重可读性与结构缺陷'],
  },
}

const nodeMap = new Map<TopologyNodeId, TopologyNode>(nodes.map((node) => [node.id, node]))
const activityNodeIds = new Set<TopologyNodeId>([
  'diagnosis', 'knowledge', 'assessment', 'task', 'verification', 'review',
  'evidence_review', 'pedagogy_review', 'data_safety_review', 'readability_review',
])

const stageLabels: Record<string, string> = {
  S0_INIT: '会话建立',
  S1_DIAGNOSIS: '岗前测评',
  S2_KNOWLEDGE: '岗位微课',
  S3_TASK: '实操任务',
  S4_VERIFY: '数据验证',
  S5_REVIEW: '专业审核',
  S6_DEBATE: '定向辩护',
  S7_STUDENT: '学员作答',
  S8_PROBE: '理解核对',
  S9_PATH_UPDATE: '培养路径更新',
  S10_DONE: '训练完成',
  S_FAIL: '安全终止',
}

const statusLabels: Record<AgentActivityStatus | 'idle', string> = {
  idle: '待机', queued: '排队', working: '执行中', waiting: '等待汇聚',
  collaborating: '协作中', reviewing: '审核中', debating: '定向复核',
  approved: '已通过', blocked: '已阻断', done: '已完成',
}

const activityInputs: Record<string, string> = {
  evidence_retrieval: '学习契约与领域包',
  parallel_evidence_retrieval: '知识、业务数据与教学三类证据源',
  parallel_resource_generation: '统一证据包与学习契约',
  quality_gate: '候选教学产物与绑定证据',
  parallel_quality_review: '待审核产物、规则集与证据包',
  specialist_quality_review: '专项规则与候选产物',
  targeted_dispute_review: '争议规则、历史裁决与受控辩护',
  bounded_debate: '可辩争议点与固定轮次预算',
  deterministic_rejection_route: '不可辩硬规则命中结果',
  local_regeneration: '被驳回的局部产物与修正约束',
}

const activeStatuses = new Set<AgentActivityStatus>([
  'working', 'collaborating', 'reviewing', 'debating',
])

const completedStatuses = new Set<AgentActivityStatus>(['approved', 'done'])

const latestByAgent = computed(() => {
  const result = new Map<AgentActivityId, AgentActivityEvent>()
  for (const event of props.events) result.set(event.agent, event)
  return result
})

const latestEvent = computed(() => props.events.at(-1))

const stateOwner: Partial<Record<StateId, AgentActivityId>> = {
  S1_DIAGNOSIS: 'diagnosis', S2_KNOWLEDGE: 'knowledge', S3_TASK: 'task',
  S4_VERIFY: 'verification', S5_REVIEW: 'review', S6_DEBATE: 'review',
  S8_PROBE: 'task',
}

const fallbackActive = computed(() => stateOwner[props.view.currentState as StateId])

function statusFor(node: TopologyNode): AgentActivityStatus | 'idle' {
  if (activityNodeIds.has(node.id)) {
    const event = latestByAgent.value.get(node.id as AgentActivityId)
    if (event) return event.status
    if (fallbackActive.value === node.id) return 'working'
    const seen = props.view.visibleMessages.some((message) => message.agent === node.id)
    return seen ? 'done' : 'idle'
  }
  if (node.kind !== 'agent') {
    if (node.id === 'orchestrator') {
      if (props.view.currentState === 'S_FAIL') return 'blocked'
      if (props.view.currentState === 'S10_DONE') return 'done'
      return props.events.length || props.view.currentState !== 'S0_INIT' ? 'working' : 'idle'
    }
    if (node.id === 'state_machine') return props.view.currentState === 'S_FAIL' ? 'blocked' : 'working'
    if (node.id === 'review_arbiter') {
      const review = latestByAgent.value.get('review')
      return review?.activity === 'parallel_quality_review' ? review.status : 'idle'
    }
    if (node.id === 'query_sandbox') {
      const verification = latestByAgent.value.get('verification')
      return verification?.status ?? (fallbackActive.value === 'verification' ? 'working' : 'idle')
    }
    return 'idle'
  }
  return 'idle'
}

function nodeClass(node: TopologyNode): string[] {
  const status = statusFor(node)
  return [
    `is-${node.kind}`,
    `is-${status}`,
    selectedId.value === node.id ? 'is-selected' : '',
    activeStatuses.has(status as AgentActivityStatus) ? 'is-active' : '',
  ]
}

type DynamicEdge = TopologyEdge & { key: string }

const dynamicEdges = computed<DynamicEdge[]>(() => {
  const result: DynamicEdge[] = []
  const seen = new Set<string>()
  function add(from: TopologyNodeId, to: TopologyNodeId): void {
    if (!nodeMap.has(from) || !nodeMap.has(to) || from === to) return
    const key = `${from}->${to}`
    if (seen.has(key)) return
    seen.add(key)
    result.push({ from, to, key })
  }
  for (const event of latestByAgent.value.values()) {
    if (!activeStatuses.has(event.status)) continue
    add('orchestrator', event.agent)
    for (const peer of event.peers) add(event.agent, peer)
    if (event.activity === 'parallel_quality_review') add(event.agent, 'review_arbiter')
    if (event.agent === 'verification') add(event.agent, 'query_sandbox')
  }
  if (!result.length && fallbackActive.value) add('orchestrator', fallbackActive.value)
  return result
})

function point(id: TopologyNodeId): TopologyNode {
  return nodeMap.get(id) ?? nodes[0]
}

const selectedId = ref<TopologyNodeId>('orchestrator')

watch(latestEvent, (event) => {
  if (event && activeStatuses.has(event.status)) selectedId.value = event.agent
})

const selectedNode = computed(() => nodeMap.get(selectedId.value) ?? nodes[0])
const selectedRuntimeConfig = computed(() => runtimeConfig[selectedNode.value.id])
const selectedEvent = computed(() => (
  activityNodeIds.has(selectedNode.value.id)
    ? latestByAgent.value.get(selectedNode.value.id as AgentActivityId)
    : latestEvent.value
))

const selectedTraceMessage = computed<TraceMessage | undefined>(() => {
  if (selectedNode.value.kind !== 'agent') return undefined
  const id = selectedNode.value.id
  if (!['diagnosis', 'knowledge', 'task', 'verification', 'review'].includes(id)) return undefined
  return [...props.view.visibleMessages].reverse().find((message) => message.agent === id)
})

function shortValue(value: unknown): string | undefined {
  if (typeof value === 'number' && Number.isFinite(value)) return String(Math.round(value * 100) / 100)
  if (typeof value !== 'string' || !value.trim()) return undefined
  const normalized = value.trim()
  return normalized.length > 28 ? `${normalized.slice(0, 18)}…${normalized.slice(-6)}` : normalized
}

const selectedMetrics = computed(() => {
  const details = selectedEvent.value?.details
  if (!details) return []
  const definitions: Array<[string, string]> = [
    ['fan_out', '并发分支'], ['parallel_elapsed_ms', '并行耗时(ms)'],
    ['elapsed_ms', '执行耗时(ms)'], ['cycle', '审核轮次'],
    ['branch_id', '当前分支'], ['aggregation', '汇聚方式'],
    ['artifact_id', '产物编号'], ['evidence_bundle_id', '证据包'],
  ]
  return definitions.flatMap(([key, label]) => {
    const value = shortValue(details[key])
    return value ? [{ key, label, value }] : []
  }).slice(0, 4)
})

const outputSummary = computed(() => {
  const event = selectedEvent.value
  const message = selectedTraceMessage.value
  if (message) {
    const parts = [`产物类型：${message.payloadType}`]
    if (message.evidence.length) parts.push(`绑定 ${message.evidence.length} 条证据`)
    if (message.claims.length) parts.push(`形成 ${message.claims.length} 条可审核主张`)
    if (message.verdict?.decision) parts.push(`裁决：${message.verdict.decision}`)
    return parts.join(' · ')
  }
  if (event) return event.label
  if (selectedNode.value.id === 'orchestrator') {
    return `当前主链位于“${stageLabels[props.view.currentState] ?? props.view.currentState}”阶段。`
  }
  return '本轮尚未产生该模块的可展示输出。'
})

const inputSummary = computed(() => {
  const event = selectedEvent.value
  if (!event) return selectedNode.value.kind === 'agent' ? '等待编排器派发任务。' : '会话状态与权威事件流。'
  return activityInputs[event.activity] ?? '当前阶段状态、学习契约及上游权威产物。'
})

const selectedRecentEvents = computed(() => {
  if (!activityNodeIds.has(selectedNode.value.id)) return props.events.slice(-5).reverse()
  return props.events.filter((event) => (
    event.agent === selectedNode.value.id || event.peers.includes(selectedNode.value.id as AgentActivityId)
  )).slice(-5).reverse()
})

const timelineEvents = computed(() => props.events.slice(-8))

function eventTime(timestamp: string): string {
  const date = new Date(timestamp)
  return Number.isNaN(date.getTime())
    ? '--:--:--'
    : date.toLocaleTimeString('zh-CN', { hour12: false })
}

function eventNodeLabel(id: AgentActivityId): string {
  return nodeMap.get(id)?.label ?? id
}

function safeEventDetails(event: AgentActivityEvent): string {
  const details = event.details ?? {}
  const allowed = [
    'fan_out', 'parallel_elapsed_ms', 'elapsed_ms', 'cycle', 'branch_id',
    'aggregation', 'artifact_id', 'evidence_bundle_id', 'decision', 'rule_id',
  ]
  const values = allowed.flatMap((key) => {
    const value = shortValue(details[key])
    return value ? [`${key}=${value}`] : []
  })
  if (event.peers.length) values.push(`peers=${event.peers.join(',')}`)
  return values.join(' · ')
}

function isDynamicEdge(edge: TopologyEdge): boolean {
  return dynamicEdges.value.some((item) => item.from === edge.from && item.to === edge.to)
}

const headline = computed(() => latestEvent.value?.label ?? (
  fallbackActive.value
    ? `${eventNodeLabel(fallbackActive.value)}正在执行当前阶段`
    : '等待 Agent 工作流启动'
))

const activeCount = computed(() => nodes.filter((node) => (
  node.kind === 'agent' && activeStatuses.has(statusFor(node) as AgentActivityStatus)
)).length)

const completedCount = computed(() => nodes.filter((node) => (
  node.kind === 'agent' && completedStatuses.has(statusFor(node) as AgentActivityStatus)
)).length)
</script>

<template>
  <section class="agent-topology" aria-label="后台 Agent 运行拓扑">
    <header class="topology-heading">
      <div>
        <span class="topology-kicker">RUNTIME TOPOLOGY</span>
        <h2>后台 Agent 运行拓扑</h2>
        <p>{{ headline }}</p>
      </div>
      <div class="topology-summary" aria-label="拓扑运行摘要">
        <span><i class="is-live" />{{ activeCount }} 个活跃</span>
        <span>{{ completedCount }} 个完成</span>
        <span>{{ stageLabels[view.currentState] ?? view.currentState }}</span>
      </div>
    </header>

    <div class="topology-main">
      <div class="topology-canvas" aria-label="Agent 与控制模块连接图">
        <div class="topology-legend">
          <span><i class="agent-dot" />Agent</span>
          <span><i class="control-dot" />控制模块</span>
          <span><i class="service-dot" />服务 / 分支</span>
          <span><i class="flow-dot" />本轮真实调用</span>
          <span><i class="passed-dot" />通过</span>
          <span><i class="blocked-dot" />阻断 / 报错</span>
        </div>

        <svg class="topology-lines" viewBox="0 0 100 100" preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <marker id="topology-arrow" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
            <marker id="topology-arrow-live" viewBox="0 0 10 10" refX="7" refY="5" markerWidth="4" markerHeight="4" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" />
            </marker>
          </defs>
          <line
            v-for="edge in edges"
            :key="`base-${edge.from}-${edge.to}`"
            class="topology-edge"
            :class="{ 'is-live': isDynamicEdge(edge) }"
            :x1="point(edge.from).x"
            :y1="point(edge.from).y"
            :x2="point(edge.to).x"
            :y2="point(edge.to).y"
            marker-end="url(#topology-arrow)"
          />
          <line
            v-for="edge in dynamicEdges"
            :key="`live-${edge.key}`"
            class="topology-edge-live"
            :x1="point(edge.from).x"
            :y1="point(edge.from).y"
            :x2="point(edge.to).x"
            :y2="point(edge.to).y"
            marker-end="url(#topology-arrow-live)"
          />
        </svg>

        <button
          v-for="node in nodes"
          :key="node.id"
          type="button"
          class="topology-node"
          :class="nodeClass(node)"
          :data-node="node.id"
          :style="{ left: `${node.x}%`, top: `${node.y}%` }"
          :aria-pressed="selectedId === node.id"
          @click="selectedId = node.id"
        >
          <span class="node-led" />
          <span class="node-glyph">{{ node.short }}</span>
          <span class="node-copy">
            <b>{{ node.label }}</b>
            <small>{{ statusLabels[statusFor(node)] }}</small>
          </span>
          <span v-if="activeStatuses.has(statusFor(node) as AgentActivityStatus)" class="node-pulse" />
        </button>
      </div>

      <aside class="topology-inspector" aria-label="所选模块详情">
        <div class="inspector-heading">
          <span :class="`is-${selectedNode.kind}`">{{ selectedNode.kind === 'agent' ? 'AGENT' : selectedNode.kind === 'control' ? 'CONTROL' : 'SERVICE / BRANCH' }}</span>
          <small>{{ statusLabels[statusFor(selectedNode)] }}</small>
          <h3>{{ selectedNode.label }}</h3>
          <p>{{ selectedNode.responsibility }}</p>
        </div>

        <dl class="module-identity">
          <div><dt>Python 模块</dt><dd>{{ selectedNode.module }}</dd></div>
          <div><dt>实现单元</dt><dd>{{ selectedNode.className }}</dd></div>
          <div v-if="debugMode"><dt>模型配置</dt><dd>{{ selectedRuntimeConfig.model }}</dd></div>
          <div v-if="debugMode"><dt>执行策略</dt><dd>{{ selectedRuntimeConfig.strategy }}</dd></div>
          <div><dt>当前阶段</dt><dd>{{ stageLabels[selectedEvent?.stage ?? view.currentState] ?? selectedEvent?.stage ?? view.currentState }}</dd></div>
        </dl>

        <section v-if="debugMode" class="module-parameters" aria-label="节点运行参数">
          <strong>节点设置与边界</strong>
          <ul>
            <li v-for="parameter in selectedRuntimeConfig.parameters" :key="parameter">{{ parameter }}</li>
          </ul>
        </section>

        <div class="io-summary">
          <article>
            <span>本次输入</span>
            <p>{{ inputSummary }}</p>
          </article>
          <article>
            <span>安全输出摘要</span>
            <p>{{ outputSummary }}</p>
          </article>
        </div>

        <div v-if="selectedMetrics.length" class="module-metrics">
          <div v-for="metric in selectedMetrics" :key="metric.key">
            <span>{{ metric.label }}</span><b>{{ metric.value }}</b>
          </div>
        </div>

        <div class="module-events">
          <div class="module-events-heading">
            <strong>最近行为</strong><small>{{ selectedRecentEvents.length }} 条</small>
          </div>
          <ol v-if="selectedRecentEvents.length">
            <li v-for="event in selectedRecentEvents" :key="`${event.trace_id}-${event.sequence}`">
              <i :class="`is-${event.status}`" />
              <div>
                <b>{{ event.label }}</b>
                <small v-if="debugMode">#{{ event.sequence }} · {{ event.activity }} · {{ eventNodeLabel(event.agent) }} · {{ eventTime(event.timestamp) }}</small>
                <small v-else>{{ eventNodeLabel(event.agent) }} · {{ eventTime(event.timestamp) }}</small>
                <code v-if="debugMode && safeEventDetails(event)">{{ safeEventDetails(event) }}</code>
              </div>
            </li>
          </ol>
          <p v-else class="empty-events">尚未收到该模块的运行事件。</p>
        </div>
      </aside>
    </div>

    <footer class="topology-timeline">
      <div class="timeline-heading">
        <span>EVENT STREAM</span>
        <strong>真实事件时间线</strong>
        <small>只展示脱敏后的业务活动</small>
      </div>
      <ol v-if="timelineEvents.length">
        <li
          v-for="event in timelineEvents"
          :key="`${event.trace_id}-${event.sequence}`"
          :class="{ 'is-active': activeStatuses.has(event.status) }"
          @click="selectedId = event.agent"
        >
          <span>#{{ event.sequence }}</span>
          <b>{{ eventNodeLabel(event.agent) }}</b>
          <small>{{ event.label }}</small>
        </li>
      </ol>
      <div v-else class="timeline-empty">工作流启动后，Agent 的派发、并行、汇聚和裁决会依次出现在这里。</div>
    </footer>
  </section>
</template>

<style scoped>
.agent-topology {
  height: 100%; min-height: 0; display: grid; grid-template-rows: auto minmax(0,1fr) 108px;
  overflow: hidden; color: #eaf8ff; background: #061827;
}
.topology-heading { display:flex; align-items:center; justify-content:space-between; gap:20px; padding:14px 18px 12px; border-bottom:1px solid rgba(120,213,255,.13); background:linear-gradient(120deg,rgba(16,52,76,.86),rgba(6,24,39,.94)); }
.topology-heading > div:first-child { min-width:0; }
.topology-kicker { display:block; color:#50d7ff; font:700 9px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace; letter-spacing:.14em; }
.topology-heading h2 { margin:3px 0 2px; color:#fff; font-size:20px; line-height:1.15; }
.topology-heading p { max-width:620px; margin:0; overflow:hidden; color:#8fb5c8; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }
.topology-summary { display:flex; gap:7px; flex-wrap:wrap; justify-content:flex-end; }
.topology-summary span { display:flex; align-items:center; gap:6px; padding:6px 9px; color:#b7d4e2; font-size:10px; background:rgba(255,255,255,.045); border:1px solid rgba(132,207,240,.13); border-radius:8px; }
.topology-summary i { width:6px; height:6px; border-radius:50%; background:#577486; }
.topology-summary i.is-live { background:#41e2ac; box-shadow:0 0 9px rgba(65,226,172,.8); animation:topology-led 1.2s ease-in-out infinite alternate; }
.topology-main { min-width:0; min-height:0; display:grid; grid-template-columns:minmax(0,1fr) 320px; overflow:hidden; }
.topology-canvas { position:relative; min-width:0; min-height:0; overflow:hidden; background:radial-gradient(circle at 49% 47%,rgba(21,169,218,.12),transparent 24%),linear-gradient(rgba(90,196,235,.035) 1px,transparent 1px),linear-gradient(90deg,rgba(90,196,235,.035) 1px,transparent 1px); background-size:auto,32px 32px,32px 32px; }
.topology-canvas::after { content:""; position:absolute; left:49%; top:47%; width:240px; height:240px; transform:translate(-50%,-50%); border:1px dashed rgba(72,211,255,.12); border-radius:50%; animation:topology-orbit 18s linear infinite; pointer-events:none; }
.topology-legend { position:absolute; z-index:4; left:14px; bottom:10px; display:flex; gap:10px; padding:6px 8px; color:#7fa5b8; font-size:9px; background:rgba(5,20,33,.82); border:1px solid rgba(111,198,235,.1); border-radius:8px; }
.topology-legend span { display:flex; align-items:center; gap:4px; }
.topology-legend i { width:7px; height:7px; display:inline-block; border-radius:50%; }
.agent-dot { background:#22bde9; }.control-dot { background:#9d7cff; clip-path:polygon(50% 0,100% 25%,100% 75%,50% 100%,0 75%,0 25%); }.service-dot { background:#36d69d; border-radius:2px!important; }.flow-dot { background:#fff; box-shadow:0 0 7px #41d7ff; }.passed-dot { background:#4bdfa8; box-shadow:0 0 6px rgba(75,223,168,.7); }.blocked-dot { background:#ff6474; box-shadow:0 0 6px rgba(255,100,116,.7); }
.topology-lines { position:absolute; z-index:1; inset:0; width:100%; height:100%; overflow:visible; }
.topology-edge { stroke:rgba(91,171,204,.18); stroke-width:.22; vector-effect:non-scaling-stroke; marker-end:url(#topology-arrow); }
.topology-edge.is-live { stroke:rgba(64,210,255,.34); }
#topology-arrow path { fill:rgba(91,171,204,.38); } #topology-arrow-live path { fill:#70e8ff; }
.topology-edge-live { stroke:#4ddaff; stroke-width:.38; stroke-dasharray:1.4 1.1; vector-effect:non-scaling-stroke; filter:drop-shadow(0 0 2px #24c9ff); animation:topology-flow 1.2s linear infinite; }
.topology-node { --tone:#26c8ef; position:absolute; z-index:3; width:120px; min-height:54px; display:grid; grid-template-columns:37px minmax(0,1fr); align-items:center; gap:7px; padding:8px 9px; transform:translate(-50%,-50%); color:#dff7ff; text-align:left; background:rgba(8,35,52,.94); border:1px solid color-mix(in srgb,var(--tone) 30%,transparent); border-radius:12px; box-shadow:0 8px 28px rgba(0,0,0,.18); cursor:pointer; transition:border-color .2s,background .2s,box-shadow .2s,transform .2s; }
.topology-node:hover,.topology-node.is-selected { z-index:5; transform:translate(-50%,-50%) scale(1.045); border-color:color-mix(in srgb,var(--tone) 72%,transparent); background:rgba(11,46,67,.98); box-shadow:0 10px 34px rgba(0,0,0,.28),0 0 18px color-mix(in srgb,var(--tone) 18%,transparent); }
.topology-node.is-control { --tone:#9c7eff; border-radius:18px 10px; }.topology-node.is-service { --tone:#45d8a4; border-radius:6px; }.topology-node.is-blocked { --tone:#ff6474; }.topology-node.is-done,.topology-node.is-approved { --tone:#4bdfa8; }.topology-node.is-idle { opacity:.72; }
.node-glyph { width:35px; height:35px; display:grid; place-items:center; color:var(--tone); font:700 8px/1 ui-monospace,SFMono-Regular,Consolas,monospace; background:color-mix(in srgb,var(--tone) 10%,transparent); border:1px solid color-mix(in srgb,var(--tone) 24%,transparent); border-radius:10px; }
.is-control .node-glyph { clip-path:polygon(50% 0,96% 25%,96% 75%,50% 100%,4% 75%,4% 25%); border-radius:0; }.is-service .node-glyph { border-radius:4px; }
.node-copy { min-width:0; display:grid; gap:3px; }.node-copy b { overflow:hidden; color:#f4fbff; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }.node-copy small { color:#75a0b4; font-size:9px; }
.node-led { position:absolute; right:7px; top:7px; width:5px; height:5px; border-radius:50%; background:#496c7c; }.is-active .node-led { background:var(--tone); box-shadow:0 0 8px var(--tone); }
.node-pulse { position:absolute; inset:-5px; border:1px solid color-mix(in srgb,var(--tone) 40%,transparent); border-radius:15px; pointer-events:none; animation:topology-pulse 1.7s ease-out infinite; }
.topology-inspector { min-width:0; min-height:0; display:flex; flex-direction:column; gap:10px; padding:14px; overflow:auto; background:linear-gradient(180deg,#0b2434,#081d2c); border-left:1px solid rgba(108,201,238,.13); }
.inspector-heading > span { display:inline-flex; padding:3px 6px; color:#66dfff; font:700 8px/1 ui-monospace,SFMono-Regular,Consolas,monospace; letter-spacing:.08em; background:rgba(75,210,255,.1); border-radius:4px; }.inspector-heading > span.is-control { color:#ba9cff; background:rgba(159,126,255,.12); }.inspector-heading > span.is-service { color:#5ce2b1; background:rgba(69,216,164,.1); }
.inspector-heading > small { float:right; color:#74a1b5; font-size:9px; }.inspector-heading h3 { margin:8px 0 4px; color:#fff; font-size:20px; }.inspector-heading p { margin:0; color:#96b7c7; font-size:11px; line-height:1.55; }
.module-identity { display:grid; gap:1px; margin:0; overflow:hidden; border:1px solid rgba(117,197,231,.12); border-radius:9px; }.module-identity div { min-width:0; display:grid; grid-template-columns:72px minmax(0,1fr); gap:8px; padding:7px 8px; background:rgba(255,255,255,.025); }.module-identity dt { color:#638ca0; font-size:9px; }.module-identity dd { min-width:0; margin:0; overflow:hidden; color:#c8dfeb; font:500 9px/1.35 ui-monospace,SFMono-Regular,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
.module-parameters { padding:8px 9px; background:rgba(159,126,255,.055); border:1px solid rgba(159,126,255,.12); border-radius:9px; }.module-parameters strong { color:#d8cbff; font-size:9px; }.module-parameters ul { display:grid; gap:4px; margin:6px 0 0; padding-left:16px; }.module-parameters li { color:#9db8c6; font-size:9px; line-height:1.4; }
.io-summary { display:grid; grid-template-columns:1fr 1fr; gap:7px; }.io-summary article { min-width:0; padding:8px; background:rgba(255,255,255,.035); border:1px solid rgba(124,201,233,.1); border-radius:9px; }.io-summary span { display:block; margin-bottom:4px; color:#5f91a8; font-size:8px; }.io-summary p { margin:0; color:#c7dce6; font-size:9px; line-height:1.45; }
.module-metrics { display:grid; grid-template-columns:1fr 1fr; gap:5px; }.module-metrics div { min-width:0; display:grid; gap:2px; padding:6px 7px; background:rgba(46,201,242,.055); border-radius:7px; }.module-metrics span { color:#628da1; font-size:8px; }.module-metrics b { overflow:hidden; color:#d8f6ff; font:600 9px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
.module-events { min-height:0; display:flex; flex:1 1 auto; flex-direction:column; overflow:hidden; }.module-events-heading { display:flex; align-items:center; justify-content:space-between; padding:2px 0 6px; border-bottom:1px solid rgba(118,197,231,.12); }.module-events-heading strong { font-size:10px; }.module-events-heading small { color:#638da1; font-size:8px; }.module-events ol { min-height:0; display:grid; align-content:start; gap:1px; margin:0; padding:5px 0 0; overflow:auto; list-style:none; }.module-events li { display:grid; grid-template-columns:8px minmax(0,1fr); gap:7px; padding:5px 3px; }.module-events li > i { width:5px; height:5px; margin-top:4px; border-radius:50%; background:#557585; }.module-events li > i.is-working,.module-events li > i.is-collaborating,.module-events li > i.is-reviewing,.module-events li > i.is-debating { background:#47d8ff; box-shadow:0 0 6px #47d8ff; }.module-events li > i.is-done,.module-events li > i.is-approved { background:#45daa6; }.module-events li > i.is-blocked { background:#ff6474; }.module-events li div { min-width:0; display:grid; gap:2px; }.module-events li b { overflow:hidden; color:#cfe5ef; font-size:9px; text-overflow:ellipsis; white-space:nowrap; }.module-events li small { color:#5e899d; font-size:8px; }.empty-events { margin:auto; color:#5b8295; font-size:9px; }
.module-events li code { display:block; overflow:hidden; color:#6d9aad; font:500 8px/1.35 ui-monospace,SFMono-Regular,Consolas,monospace; text-overflow:ellipsis; white-space:nowrap; }
.topology-timeline { min-width:0; display:grid; grid-template-columns:150px minmax(0,1fr); gap:10px; padding:10px 14px; overflow:hidden; border-top:1px solid rgba(115,203,239,.13); background:#071b2a; }
.timeline-heading { display:flex; flex-direction:column; justify-content:center; }.timeline-heading span { color:#47d6ff; font:700 8px/1.2 ui-monospace,SFMono-Regular,Consolas,monospace; letter-spacing:.1em; }.timeline-heading strong { margin:3px 0 2px; color:#e9f8ff; font-size:11px; }.timeline-heading small { color:#60889b; font-size:8px; }
.topology-timeline ol { min-width:0; display:grid; grid-auto-flow:column; grid-auto-columns:minmax(110px,1fr); gap:5px; margin:0; padding:0; overflow-x:auto; list-style:none; }.topology-timeline li { min-width:0; display:grid; align-content:center; gap:2px; padding:7px 8px; color:#7ea5b8; background:rgba(255,255,255,.025); border:1px solid rgba(115,194,228,.09); border-radius:8px; cursor:pointer; }.topology-timeline li.is-active { color:#51d9ff; background:rgba(45,198,239,.07); border-color:rgba(74,211,248,.22); }.topology-timeline li span { font:700 8px/1 ui-monospace,SFMono-Regular,Consolas,monospace; }.topology-timeline li b { overflow:hidden; color:#d3e7f0; font-size:9px; text-overflow:ellipsis; white-space:nowrap; }.topology-timeline li small { overflow:hidden; font-size:8px; text-overflow:ellipsis; white-space:nowrap; }.timeline-empty { display:grid; place-items:center; color:#5d8497; font-size:10px; border:1px dashed rgba(110,190,225,.12); border-radius:8px; }
@keyframes topology-flow { to { stroke-dashoffset:-5; } }
@keyframes topology-pulse { 0% { opacity:.8; transform:scale(.96); } 100% { opacity:0; transform:scale(1.12); } }
@keyframes topology-led { to { opacity:.4; transform:scale(.75); } }
@keyframes topology-orbit { to { transform:translate(-50%,-50%) rotate(360deg); } }
@media (max-width:1100px) { .topology-main { grid-template-columns:minmax(0,1fr) 280px; }.topology-node { width:105px; }.node-copy b { font-size:10px; }.io-summary { grid-template-columns:1fr; } }
@media (prefers-reduced-motion:reduce) { .topology-edge-live,.node-pulse,.topology-summary i.is-live,.topology-canvas::after { animation:none!important; } }
</style>
