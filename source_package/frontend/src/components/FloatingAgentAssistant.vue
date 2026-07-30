<script setup lang="ts">
import { Check, ChevronDown, ChevronUp, Grip, Sparkles } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue'

import type {
  AgentActivityEvent,
  AgentActivityId,
  AgentActivityStatus,
} from '../lib/interactiveApi'
import type { StateId, TraceView } from '../types/trace'
import AgentTeacherAvatar from './AgentTeacherAvatar.vue'


const props = withDefaults(defineProps<{
  events?: AgentActivityEvent[]
  view: TraceView
}>(), {
  events: () => [],
})

const STORAGE_KEY = 'learner-floating-agent-position-v1'
const expanded = ref(false)
const dragging = ref(false)
const root = ref<HTMLElement>()
const position = ref({ x: 0, y: 0 })
let dragOffset = { x: 0, y: 0 }

const activeStatuses = new Set<AgentActivityStatus>([
  'working',
  'collaborating',
  'reviewing',
  'debating',
])

const agentNames: Record<AgentActivityId, string> = {
  diagnosis: '学情诊断老师',
  knowledge: '领域知识老师',
  task: '实操任务老师',
  verification: '数据验证老师',
  review: '专业审核老师',
  evidence_review: '证据核验老师',
  pedagogy_review: '教学适配老师',
  data_safety_review: '数据安全老师',
  readability_review: '表达校阅老师',
  assessment: '分阶测验老师',
}

const agentPurposes: Record<AgentActivityId, string> = {
  diagnosis: '识别你的知识基础与需要加强的部分。',
  knowledge: '从专业材料中组织与你岗位匹配的内容。',
  task: '生成与你当前能力匹配的岗位练习。',
  verification: '使用查询结果核对数据与结论。',
  review: '综合检查事实、难度、安全与表达质量。',
  evidence_review: '核对结论是否有可靠材料支持。',
  pedagogy_review: '检查内容难度是否适合当前学员。',
  data_safety_review: '检查数据范围与查询方式是否安全。',
  readability_review: '检查内容是否清楚、易读、无歧义。',
  assessment: '准备与本轮学习目标对应的测验。',
}

const stateOwners: Partial<Record<StateId, AgentActivityId>> = {
  S1_DIAGNOSIS: 'diagnosis',
  S2_KNOWLEDGE: 'knowledge',
  S3_TASK: 'task',
  S4_VERIFY: 'verification',
  S5_REVIEW: 'review',
  S6_DEBATE: 'review',
  S7_STUDENT: 'task',
  S8_PROBE: 'task',
  S9_PATH_UPDATE: 'knowledge',
  S10_DONE: 'knowledge',
  S_FAIL: 'review',
}

const stateProgress: Partial<Record<StateId, number>> = {
  S1_DIAGNOSIS: 12,
  S2_KNOWLEDGE: 32,
  S3_TASK: 52,
  S4_VERIFY: 68,
  S5_REVIEW: 78,
  S6_DEBATE: 82,
  S7_STUDENT: 88,
  S8_PROBE: 92,
  S9_PATH_UPDATE: 97,
  S10_DONE: 100,
  S_FAIL: 100,
}

const latestEventsByAgent = computed(() => {
  const latest = new Map<AgentActivityId, AgentActivityEvent>()
  for (const event of props.events) latest.set(event.agent, event)
  return latest
})
const latestEvent = computed(() => props.events.at(-1))
const activeEvent = computed(() => [...latestEventsByAgent.value.values()]
  .filter((event) => activeStatuses.has(event.status))
  .sort((left, right) => left.sequence - right.sequence)
  .at(-1))
const currentEvent = computed(() => activeEvent.value ?? latestEvent.value)
const currentAgent = computed<AgentActivityId>(() => (
  currentEvent.value?.agent
  ?? stateOwners[props.view.currentState as StateId]
  ?? 'knowledge'
))
const currentStatus = computed<AgentActivityStatus>(() => {
  if (currentEvent.value) return currentEvent.value.status
  if (props.view.currentState === 'S10_DONE') return 'done'
  if (props.view.currentState === 'S_FAIL') return 'blocked'
  return 'working'
})
const isActive = computed(() => activeStatuses.has(currentStatus.value))
const isComplete = computed(() => ['approved', 'done'].includes(currentStatus.value))
const progress = computed(() => stateProgress[props.view.currentState as StateId] ?? 8)
const progressLabel = computed(() => currentStatus.value === 'blocked' ? '流程状态' : '本轮进度')
const progressValue = computed(() => currentStatus.value === 'blocked' ? '已安全停止' : `${progress.value}%`)

const statusLabel = computed(() => {
  const labels: Record<AgentActivityStatus, string> = {
    idle: '待机中',
    queued: '等待接力',
    working: '正在工作',
    waiting: '等待结果',
    collaborating: '正在协作',
    reviewing: '正在审核',
    debating: '正在复核',
    approved: '已经通过',
    blocked: '需要重试',
    done: '本步完成',
  }
  return labels[currentStatus.value]
})

const currentTask = computed(() => currentEvent.value?.label ?? (() => {
  const fallbacks: Partial<Record<StateId, string>> = {
    S1_DIAGNOSIS: '正在识别你的知识基础与薄弱点',
    S2_KNOWLEDGE: '正在准备与你岗位匹配的学习内容',
    S3_TASK: '正在生成并核对本轮实操任务',
    S4_VERIFY: '正在核验查询结果与统计口径',
    S5_REVIEW: '正在检查事实、难度与表达质量',
    S6_DEBATE: '正在复核有争议的内容',
    S7_STUDENT: '材料已经就绪，正在等待你的操作',
    S8_PROBE: '正在根据你的判断准备下一轮提示',
    S9_PATH_UPDATE: '正在更新后续培养路径',
    S10_DONE: '本轮训练已经完成',
    S_FAIL: '本轮内容需要重新准备',
  }
  return fallbacks[props.view.currentState as StateId] ?? '正在准备本轮学习内容'
})())

const completedItems = computed(() => {
  return [...latestEventsByAgent.value.values()]
    .filter((event) => ['done', 'approved'].includes(event.status))
    .sort((left, right) => right.sequence - left.sequence)
    .map((event) => ({ agent: event.agent, label: event.label }))
    .slice(0, 3)
})

const panelStyle = computed(() => ({
  left: `${position.value.x}px`,
  top: `${position.value.y}px`,
}))

function clampPosition(x: number, y: number): { x: number; y: number } {
  const rect = root.value?.getBoundingClientRect()
  const targetWidth = expanded.value ? 320 : 92
  const targetHeight = expanded.value ? 310 : 118
  const width = Math.max(rect?.width ?? 0, targetWidth)
  const height = Math.max(rect?.height ?? 0, targetHeight)
  const margin = 12
  return {
    x: Math.max(margin, Math.min(x, window.innerWidth - width - margin)),
    y: Math.max(margin, Math.min(y, window.innerHeight - height - margin)),
  }
}

function savePosition(): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(position.value))
  } catch {
    // Display preference persistence is optional.
  }
}

function restorePosition(): void {
  let stored: { x?: unknown; y?: unknown } | undefined
  try {
    stored = JSON.parse(localStorage.getItem(STORAGE_KEY) ?? 'null') ?? undefined
  } catch {
    stored = undefined
  }
  const defaultPosition = {
    x: window.innerWidth - 112,
    y: window.innerHeight - 158,
  }
  position.value = clampPosition(
    typeof stored?.x === 'number' ? stored.x : defaultPosition.x,
    typeof stored?.y === 'number' ? stored.y : defaultPosition.y,
  )
}

function move(event: PointerEvent): void {
  if (!dragging.value) return
  position.value = clampPosition(
    event.clientX - dragOffset.x,
    event.clientY - dragOffset.y,
  )
}

function finishDrag(): void {
  if (!dragging.value) return
  dragging.value = false
  savePosition()
  window.removeEventListener('pointermove', move)
  window.removeEventListener('pointerup', finishDrag)
  window.removeEventListener('pointercancel', finishDrag)
}

function startDrag(event: PointerEvent): void {
  if (event.button !== 0) return
  const target = event.target as HTMLElement
  if (target.closest('button')) return
  const rect = root.value?.getBoundingClientRect()
  if (!rect) return
  dragging.value = true
  dragOffset = {
    x: event.clientX - rect.left,
    y: event.clientY - rect.top,
  }
  window.addEventListener('pointermove', move)
  window.addEventListener('pointerup', finishDrag)
  window.addEventListener('pointercancel', finishDrag)
}

async function toggleExpanded(): Promise<void> {
  expanded.value = !expanded.value
  await nextTick()
  position.value = clampPosition(position.value.x, position.value.y)
  savePosition()
}

function handleResize(): void {
  position.value = clampPosition(position.value.x, position.value.y)
}

onMounted(() => {
  restorePosition()
  window.addEventListener('resize', handleResize)
})

onBeforeUnmount(() => {
  finishDrag()
  window.removeEventListener('resize', handleResize)
})
</script>

<template>
  <aside
    ref="root"
    class="floating-agent-assistant"
    :class="[
      `is-${currentStatus}`,
      { 'is-expanded': expanded, 'is-dragging': dragging, 'is-active': isActive },
    ]"
    :style="panelStyle"
    aria-label="学习助手"
  >
    <header class="floating-agent-handle" @pointerdown="startDrag">
      <Grip :size="15" aria-hidden="true" />
      <span>学习助手</span>
      <button
        type="button"
        :aria-label="expanded ? '收起学习助手' : '展开学习助手'"
        :aria-expanded="expanded"
        @click="toggleExpanded"
      >
        <ChevronDown v-if="expanded" :size="16" aria-hidden="true" />
        <ChevronUp v-else :size="16" aria-hidden="true" />
      </button>
    </header>

    <button
      v-if="!expanded"
      type="button"
      class="floating-agent-orb"
      aria-label="查看后台助手进度"
      @click="toggleExpanded"
    >
      <span class="assistant-ripple" aria-hidden="true" />
      <span class="assistant-avatar">
        <AgentTeacherAvatar :agent="currentAgent" :status="currentStatus" :size="68" />
        <Sparkles v-if="isComplete" class="assistant-sparkle" :size="15" aria-hidden="true" />
      </span>
      <span class="assistant-status-dot" :class="{ 'is-running': isActive }" />
      <small>{{ statusLabel }}</small>
    </button>

    <div v-else class="floating-agent-detail">
      <section class="assistant-current">
        <span class="assistant-avatar is-large">
          <AgentTeacherAvatar :agent="currentAgent" :status="currentStatus" :size="78" />
          <span v-if="isActive" class="assistant-work-dots" aria-hidden="true">
            <i /><i /><i />
          </span>
        </span>
        <div>
          <span class="assistant-state">{{ statusLabel }}</span>
          <strong>{{ agentNames[currentAgent] }}</strong>
          <p>{{ currentTask }}</p>
        </div>
      </section>

      <section class="assistant-progress" aria-label="本轮后台进度">
        <div>
          <span>{{ progressLabel }}</span>
          <strong>{{ progressValue }}</strong>
        </div>
        <span class="assistant-progress-track">
          <i :style="{ width: currentStatus === 'blocked' ? '100%' : `${progress}%` }" />
        </span>
      </section>

      <section class="assistant-purpose">
        <span>当前职责</span>
        <p>{{ agentPurposes[currentAgent] }}</p>
      </section>

      <section v-if="completedItems.length" class="assistant-completed">
        <span>最近完成</span>
        <ul>
          <li v-for="item in completedItems" :key="item.agent">
            <Check :size="13" aria-hidden="true" />
            <span>{{ agentNames[item.agent] }}</span>
            <small>{{ item.label }}</small>
          </li>
        </ul>
      </section>
      <p v-else class="assistant-waiting-copy">后台任务开始后，完成情况会显示在这里。</p>
    </div>
  </aside>
</template>

<style scoped>
.floating-agent-assistant {
  position: fixed;
  z-index: 80;
  width: 92px;
  overflow: hidden;
  border: 1px solid rgba(0, 113, 227, .16);
  border-radius: 22px;
  background: rgba(255, 255, 255, .9);
  box-shadow: 0 18px 55px rgba(31, 55, 87, .17), 0 2px 10px rgba(31, 55, 87, .08);
  color: #162338;
  backdrop-filter: blur(22px) saturate(1.18);
  transition: width .28s cubic-bezier(.2,.8,.2,1), box-shadow .2s ease;
  user-select: none;
  touch-action: none;
}
.floating-agent-assistant.is-expanded { width: 320px; }
.floating-agent-assistant.is-dragging {
  cursor: grabbing;
  box-shadow: 0 24px 70px rgba(31, 55, 87, .24);
  transition: none;
}
.floating-agent-handle {
  display: flex;
  align-items: center;
  gap: 6px;
  height: 34px;
  padding: 0 8px 0 10px;
  color: #65758a;
  cursor: grab;
}
.floating-agent-handle span {
  flex: 1;
  overflow: hidden;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}
.floating-agent-handle button {
  display: grid;
  width: 24px;
  height: 24px;
  padding: 0;
  border: 0;
  border-radius: 50%;
  background: #f2f6fb;
  color: #5c6d82;
  cursor: pointer;
  place-items: center;
}
.floating-agent-orb {
  position: relative;
  display: grid;
  width: 100%;
  min-height: 92px;
  padding: 2px 8px 9px;
  border: 0;
  background: transparent;
  color: inherit;
  cursor: pointer;
  place-items: center;
}
.assistant-avatar {
  position: relative;
  display: block;
  width: 62px;
  height: 62px;
  transform-origin: 50% 90%;
}
.assistant-avatar.is-large { width: 76px; height: 76px; flex: 0 0 76px; }
.floating-agent-assistant:not(.is-dragging) .assistant-avatar {
  animation: assistant-idle 3.4s ease-in-out infinite;
}
.floating-agent-assistant.is-active:not(.is-dragging) .assistant-avatar {
  animation: assistant-work .9s ease-in-out infinite;
}
.assistant-ripple {
  position: absolute;
  top: 15px;
  left: 50%;
  width: 52px;
  height: 52px;
  border: 1px solid rgba(0, 113, 227, .24);
  border-radius: 50%;
  transform: translateX(-50%);
  opacity: 0;
}
.is-active .assistant-ripple { animation: assistant-ripple 1.8s ease-out infinite; }
.assistant-sparkle {
  position: absolute;
  top: 2px;
  right: -2px;
  color: #12a967;
  animation: assistant-sparkle 1.7s ease-in-out infinite;
}
.assistant-status-dot {
  position: absolute;
  top: 43px;
  right: 15px;
  width: 9px;
  height: 9px;
  border: 2px solid #fff;
  border-radius: 50%;
  background: #9aa8b6;
  box-shadow: 0 1px 5px rgba(0,0,0,.16);
}
.assistant-status-dot.is-running {
  background: #22c97a;
  animation: assistant-dot 1.15s ease-in-out infinite;
}
.floating-agent-orb small { color: #506178; font-size: 10px; font-weight: 700; }
.floating-agent-detail { padding: 2px 16px 16px; }
.assistant-current {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 8px 0 12px;
  border-bottom: 1px solid #e8edf3;
}
.assistant-current > div { min-width: 0; }
.assistant-state {
  display: block;
  margin-bottom: 3px;
  color: #0071e3;
  font-size: 10px;
  font-weight: 700;
}
.assistant-current strong { display: block; font-size: 16px; }
.assistant-current p,
.assistant-purpose p {
  display: -webkit-box;
  overflow: hidden;
  margin: 4px 0 0;
  color: #65758a;
  font-size: 11px;
  line-height: 1.55;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
}
.assistant-work-dots {
  position: absolute;
  right: -3px;
  bottom: 8px;
  display: flex;
  gap: 2px;
  padding: 4px 5px;
  border-radius: 10px;
  background: #fff;
  box-shadow: 0 3px 12px rgba(20, 44, 77, .16);
}
.assistant-work-dots i {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: #0071e3;
  animation: assistant-thinking .9s ease-in-out infinite;
}
.assistant-work-dots i:nth-child(2) { animation-delay: .14s; }
.assistant-work-dots i:nth-child(3) { animation-delay: .28s; }
.assistant-progress { padding: 12px 0 8px; }
.assistant-progress > div { display: flex; align-items: center; justify-content: space-between; }
.assistant-progress span,
.assistant-purpose > span,
.assistant-completed > span { color: #8491a2; font-size: 10px; font-weight: 700; }
.assistant-progress strong { color: #0071e3; font-size: 13px; }
.assistant-progress-track {
  display: block;
  height: 5px;
  margin-top: 7px;
  overflow: hidden;
  border-radius: 999px;
  background: #edf2f7;
}
.assistant-progress-track i {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: linear-gradient(90deg, #0071e3, #4cb6ff);
  transition: width .45s ease;
}
.is-blocked .assistant-progress strong { color: #c24135; }
.is-blocked .assistant-progress-track i {
  background: linear-gradient(90deg, #e08378, #c24135);
}
.assistant-purpose { padding: 7px 0 9px; }
.assistant-completed { padding-top: 9px; border-top: 1px solid #e8edf3; }
.assistant-completed ul { display: grid; gap: 5px; margin: 7px 0 0; padding: 0; list-style: none; }
.assistant-completed li {
  display: grid;
  grid-template-columns: 14px auto minmax(0, 1fr);
  align-items: center;
  gap: 5px;
  color: #19925c;
  font-size: 10px;
}
.assistant-completed li span { color: #314157; font-weight: 700; }
.assistant-completed li small { overflow: hidden; color: #7c8998; text-overflow: ellipsis; white-space: nowrap; }
.assistant-waiting-copy { margin: 10px 0 0; color: #8b97a5; font-size: 10px; }

@keyframes assistant-idle {
  0%,100% { transform: translateY(0) rotate(-1deg); }
  50% { transform: translateY(-5px) rotate(1deg); }
}
@keyframes assistant-work {
  0%,100% { transform: translateY(0) rotate(-1.5deg) scale(1); }
  50% { transform: translateY(-4px) rotate(1.5deg) scale(1.035); }
}
@keyframes assistant-ripple {
  0% { opacity: .65; transform: translateX(-50%) scale(.75); }
  100% { opacity: 0; transform: translateX(-50%) scale(1.55); }
}
@keyframes assistant-dot { 0%,100% { transform: scale(.72); opacity: .5; } 50% { transform: scale(1.2); opacity: 1; } }
@keyframes assistant-sparkle { 0%,100% { transform: rotate(-8deg) scale(.8); opacity: .55; } 50% { transform: rotate(8deg) scale(1.15); opacity: 1; } }
@keyframes assistant-thinking { 0%,100% { transform: translateY(0); opacity: .35; } 50% { transform: translateY(-3px); opacity: 1; } }

@media (max-width: 720px) {
  .floating-agent-assistant.is-expanded { width: min(320px, calc(100vw - 24px)); }
}
@media (prefers-reduced-motion: reduce) {
  .floating-agent-assistant *,
  .floating-agent-assistant *::before,
  .floating-agent-assistant *::after { animation: none !important; transition-duration: .01ms !important; }
}
</style>
