<script setup lang="ts">
import {
  Bookmark,
  Download,
  Pause,
  Play,
  StepForward,
  Upload,
  UserRound,
  Users,
} from '@lucide/vue'

import type { Keyframe } from '../types/trace'
import type { ReplaySpeed } from '../composables/useReplay'


withDefaults(defineProps<{
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
  canExport?: boolean
}>(), {
  canExport: true,
})

const emit = defineEmits<{
  select: [fileName: string]
  play: []
  pause: []
  step: []
  speed: [value: ReplaySpeed]
  jump: [step: number]
  comparison: [value: boolean]
  entry: [value: 'replay' | 'live']
  view: [value: 'student' | 'collaboration']
  import: [file: File]
  export: []
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

function importSelected(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('import', file)
  input.value = ''
}

function importDropped(event: DragEvent): void {
  const file = event.dataTransfer?.files[0]
  if (file) emit('import', file)
}
</script>

<template>
  <div class="workshop-header">
    <div class="workshop-status" aria-label="车间数据状态">
      <span><b>船号</b> H2601</span>
      <span><b>工序</b> YCL · 预处理</span>
      <span><b>数据截至</b> 2025-07-31</span>
      <span><b>会话模式</b> {{ entryMode === 'live' ? '实时实操' : '回放复盘' }}</span>
    </div>

    <header class="replay-toolbar">
      <div class="product-mark">
        <span class="product-kicker">岗位训练台</span>
        <strong>船厂数字化岗位培训</strong>
        <span>{{ entryMode === 'live' ? '真实生产数据实操' : '培养记录回放' }}</span>
      </div>

      <div class="entry-switch control-cluster" aria-label="训练进入方式">
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
      </div>

      <template v-if="entryMode === 'replay'">
        <div class="trace-selector control-cluster">
          <label for="trace-select">回放会话</label>
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

        <div v-if="canCompare" class="view-switch control-cluster" aria-label="画像查看方式">
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
      </template>

      <div
        class="trace-transfer control-cluster"
        data-testid="trace-transfer"
        aria-label="导入或导出会话记录"
        @dragover.prevent
        @drop.prevent="importDropped"
      >
        <label class="trace-transfer-action" aria-label="选择要导入的会话记录">
          <Upload :size="15" aria-hidden="true" />
          <span>导入</span>
          <input
            class="visually-hidden"
            type="file"
            accept=".jsonl,application/x-ndjson,application/json"
            aria-label="导入会话记录"
            @change="importSelected"
          />
        </label>
        <button
          type="button"
          class="trace-transfer-action"
          aria-label="导出当前会话"
          :disabled="!canExport"
          @click="emit('export')"
        >
          <Download :size="15" aria-hidden="true" />
          <span>导出</span>
        </button>
      </div>

      <div class="audience-switch control-cluster" aria-label="页面查看方式">
        <button
          type="button"
          :class="{ 'is-active': viewMode === 'student' }"
          aria-label="切换到学员模式"
          @click="emit('view', 'student')"
        >学员模式</button>
        <button
          type="button"
          :class="{ 'is-active': viewMode === 'collaboration' }"
          aria-label="切换到协同视图"
          @click="emit('view', 'collaboration')"
        >协同视图</button>
      </div>
    </header>
  </div>
</template>
