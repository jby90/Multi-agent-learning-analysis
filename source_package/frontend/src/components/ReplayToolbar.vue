<script setup lang="ts">
import {
  Bookmark,
  Pause,
  Play,
  RotateCcw,
  StepBack,
  StepForward,
  Workflow,
  UserRound,
  Users,
} from '@lucide/vue'

import type { Keyframe } from '../types/trace'
import type { ReplaySpeed } from '../composables/useReplay'


const props = withDefaults(defineProps<{
  traces: { fileName: string; label: string }[]
  selectedFile: string
  playing: boolean
  cursor: number
  total: number
  speed: ReplaySpeed
  keyframes: Keyframe[]
  comparison: boolean
  canCompare: boolean
  entryMode: 'replay' | 'live'
  viewMode: 'student' | 'collaboration'
  hasSession?: boolean
  authRole?: 'student' | 'admin' | null
  /** 优化21：前测进行中时禁用协同视图切换 */
  viewDisabled?: boolean
}>(), {
  authRole: null,
  hasSession: false,
  viewDisabled: false,
})

const emit = defineEmits<{
  select: [fileName: string]
  play: []
  pause: []
  step: []
  back: []
  restart: []
  speed: [value: ReplaySpeed]
  jump: [step: number]
  comparison: [value: boolean]
  entry: [value: 'replay' | 'live']
  view: [value: 'student' | 'collaboration']
  debug: []
}>()

function selectTrace(event: Event): void {
  emit('select', (event.target as HTMLSelectElement).value)
}

function selectSpeed(event: Event): void {
  emit('speed', Number((event.target as HTMLSelectElement).value) as ReplaySpeed)
}

function selectKeyframe(event: Event): void {
  const value = Number((event.target as HTMLSelectElement).value)
  if (Number.isInteger(value) && value > 0) emit('jump', value)
  ;(event.target as HTMLSelectElement).value = ''
}
</script>

<template>
  <div class="workshop-header">
    <header class="replay-toolbar">
      <div class="product-mark">
        <span class="product-symbol" aria-hidden="true">智</span>
        <!-- 0819：品牌区两行并一行——只留主标题，与徽标同行更简洁 -->
        <span class="product-copy">
          <strong>船厂数字化岗位培训</strong>
        </span>
      </div>

      <!-- 优化20：入口切换仅 admin 可见——游客（未登录跳过）按学员对待，不见管理入口 -->
      <nav
        v-if="authRole === 'admin'"
        class="entry-switch control-cluster"
        aria-label="训练进入方式"
      >
        <button
          type="button"
          :class="{ 'is-active': entryMode === 'live' }"
          aria-label="进入实操通道"
          @click="emit('entry', 'live')"
        >实操通道</button>
        <button
          type="button"
          :class="{ 'is-active': entryMode === 'replay' }"
          aria-label="进入会话回放"
          @click="emit('entry', 'replay')"
        >会话回放</button>
      </nav>

      <span class="toolbar-spacer" aria-hidden="true"></span>
      <span class="toolbar-spacer toolbar-spacer-half" aria-hidden="true"></span>

      <div v-if="entryMode === 'replay' && canCompare" class="view-switch control-cluster" aria-label="画像查看方式">
        <button
          type="button"
          :class="{ 'is-active': !comparison }"
          aria-label="单画像查看"
          @click="emit('comparison', false)"
        >
          <UserRound :size="15" aria-hidden="true" />
          单画像
        </button>
        <button
          type="button"
          :class="{ 'is-active': comparison }"
          aria-label="三画像同屏"
          @click="emit('comparison', true)"
        >
          <Users :size="15" aria-hidden="true" />
          三画像
        </button>
      </div>

      <!-- 学员模式/协同视图只作用于单画像：三画像下无差异功能，隐藏避免假活
           （2026-08-16 实测）。优化1：占位隐藏（visibility）而非摘除——
           否则点"三画像"后整排按钮左移，切回单画像又右移，体验割裂。 -->
      <div
        v-if="(entryMode === 'replay' || hasSession) && authRole === 'admin'"
        class="audience-switch control-cluster"
        :class="{ 'is-slot-hidden': comparison }"
        :aria-hidden="comparison ? 'true' : undefined"
        aria-label="页面查看方式"
      >
        <button
          type="button"
          :class="{ 'is-active': viewMode === 'student' }"
          aria-label="切换到学员模式"
          @click="emit('view', 'student')"
        >学员模式</button>
        <!-- 优化21：实操通道前测答题期间禁用协同视图 -->
        <button
          type="button"
          :class="{ 'is-active': viewMode === 'collaboration' }"
          aria-label="切换到协同视图"
          :disabled="viewDisabled"
          :title="viewDisabled ? '前测进行中，暂不可切换协同视图' : undefined"
          @click="emit('view', 'collaboration')"
        >协同视图</button>
      </div>

      <button
        v-if="entryMode === 'live' && hasSession && authRole === 'admin'"
        type="button"
        class="debug-workspace-action"
        aria-label="打开当前会话调试工作台"
        title="只查看当前浏览器正在进行的会话"
        @click="emit('debug')"
      >
        <Workflow :size="15" aria-hidden="true" />
        <span>调试工作台</span>
      </button>

      <!-- 0818 需求 1：头像固定最右上角（原 trace-transfer 位置），导入/导出移入头像下拉 -->
      <div class="toolbar-center-zone toolbar-user-zone">
        <slot name="user-menu"></slot>
      </div>
    </header>

    <!-- 优化8：三画像对比时回放命令条无意义，隐藏 -->
    <!-- 优化11：删"船号/工序/数据截至/回放复盘"数据块，会话选择+回放控制铺满整行 -->
    <div v-if="entryMode === 'replay' && !comparison" class="session-commandbar is-fullrow">
      <div class="trace-selector control-cluster">
          <label for="trace-select">当前会话</label>
          <select
            id="trace-select"
            :value="selectedFile"
            aria-label="选择回放会话"
            @change="selectTrace"
          >
            <option v-for="trace in traces" :key="trace.fileName" :value="trace.fileName">
              {{ trace.label }}
            </option>
          </select>
      </div>

      <div class="playback-controls control-cluster" aria-label="回放控制">
          <button
            type="button"
            class="icon-button"
            aria-label="回到开头"
            :disabled="cursor <= 0"
            @click="emit('restart')"
          >
            <RotateCcw :size="16" aria-hidden="true" />
          </button>
          <button
            type="button"
            class="icon-button"
            aria-label="回退一步"
            :disabled="cursor <= 0"
            @click="emit('back')"
          >
            <StepBack :size="17" aria-hidden="true" />
          </button>
          <button
            v-if="playing"
            type="button"
            class="icon-button is-active"
            aria-label="暂停回放"
            @click="emit('pause')"
          >
            <Pause :size="17" aria-hidden="true" />
          </button>
          <button
            v-else
            type="button"
            class="icon-button"
            aria-label="播放回放"
            @click="emit('play')"
          >
            <Play :size="17" aria-hidden="true" />
          </button>
          <button
            type="button"
            class="icon-button"
            aria-label="前进一步"
            :disabled="cursor >= total"
            @click="emit('step')"
          >
            <StepForward :size="17" aria-hidden="true" />
          </button>
          <div class="playback-progress">
            <span>{{ cursor }} / {{ total }}</span>
            <progress :value="cursor" :max="Math.max(total, 1)"></progress>
          </div>
          <select :value="speed" aria-label="切换回放速度" @change="selectSpeed">
            <option :value="1">1x</option>
            <option :value="2">2x</option>
            <option :value="5">5x</option>
          </select>
      </div>

      <div v-if="keyframes.length" class="bookmark-control control-cluster">
          <Bookmark :size="16" aria-hidden="true" />
          <select aria-label="跳到关键帧" value="" @change="selectKeyframe">
            <option value="" disabled>关键帧</option>
            <option v-for="frame in keyframes" :key="`${frame.kind}-${frame.step}`" :value="frame.step">
              {{ frame.label }}
            </option>
          </select>
      </div>
    </div>
  </div>
</template>
