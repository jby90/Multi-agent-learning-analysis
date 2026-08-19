<script setup lang="ts">
import * as echarts from 'echarts/core'
import { BarChart } from 'echarts/charts'
import { GridComponent, TooltipComponent } from 'echarts/components'
import { SVGRenderer } from 'echarts/renderers'
import { ArrowLeft, Radar } from '@lucide/vue'
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import DiagnosisRadar from './DiagnosisRadar.vue'
import WaveText from './WaveText.vue'
import { createInteractiveApi } from '../lib/interactiveApi'
import type { LearningRecord, ProfileLearningSummary } from '../lib/interactiveApi'
import { misconceptionLabel, learnerText } from '../lib/tracePresentation'

echarts.use([BarChart, GridComponent, TooltipComponent, SVGRenderer])

const props = defineProps<{
  role?: 'student' | 'admin' | null
}>()

const emit = defineEmits<{ close: [] }>()

const api = createInteractiveApi()
const token = () => sessionStorage.getItem('ref-auth-token')
const records = ref<LearningRecord[]>([])
const guest = ref(false)
const loading = ref(true)
const loadError = ref('')
const selectedId = ref<string | null>(null)
const summary = ref<ProfileLearningSummary[] | null>(null)
const summaryError = ref('')

const TIER_LABELS = ['未学', '基础档', '应用档', '进阶档']

const sortedRecords = computed(() => [...records.value].reverse())
const selected = computed(() => (
  sortedRecords.value.find((item) => item.record_id === selectedId.value)
  ?? sortedRecords.value[0]
))

const radarDimensions = computed(() => {
  const mastery = selected.value?.mastery ?? {}
  return Object.keys(mastery)
})

const radarLevels = computed(() => {
  const mastery = selected.value?.mastery ?? {}
  return Object.keys(mastery).map((key) => Number(mastery[key] ?? 0))
})

function formatDuration(seconds: number | undefined): string {
  const total = Math.max(0, Math.floor(seconds ?? 0))
  const minutes = Math.floor(total / 60)
  if (minutes < 60) return `${minutes} 分钟`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}

function tierText(tier: number | undefined): string {
  return TIER_LABELS[Math.min(3, Math.max(0, Number(tier ?? 0)))]
}

function profileTitle(profileId: string): string {
  const titles: Record<string, string> = {
    planner_new: '新入职生产计划员',
    craft_engineer: '转岗工艺工程师',
    line_leader: '一线班组长（晋升培训）',
  }
  return titles[profileId] ?? profileId
}

// ---- 优化10：每画像误区频次条形图 ----
const barEls = ref<HTMLElement[]>([])
const barChartMap = new Map<number, echarts.ECharts>()

function setBarEl(index: number, el: unknown): void {
  if (el instanceof HTMLElement) {
    barEls.value[index] = el
  }
}

function renderBars(): void {
  const profiles = summary.value ?? []
  profiles.forEach((profile, index) => {
    const el = barEls.value[index]
    if (!el) return
    const entries = Object.entries(profile.misconception_frequency ?? {})
    if (!entries.length) return
    let chart = barChartMap.get(index)
    if (!chart) {
      chart = echarts.init(el)
      barChartMap.set(index, chart)
    }
    chart.setOption({
      tooltip: { trigger: 'axis', confine: true, axisPointer: { type: 'shadow' } },
      grid: { left: 8, right: 30, top: 6, bottom: 6, containLabel: true },
      xAxis: { type: 'value', minInterval: 1, splitLine: { lineStyle: { color: '#eef2f7' } } },
      yAxis: {
        type: 'category',
        inverse: true,
        data: entries.map(([id]) => misconceptionLabel(id)),
        axisLabel: { fontSize: 11, color: '#475569' },
        axisLine: { lineStyle: { color: '#e2e8f0' } },
        axisTick: { show: false },
      },
      series: [{
        type: 'bar',
        barMaxWidth: 16,
        itemStyle: { color: '#b91c1c', borderRadius: [0, 4, 4, 0] },
        label: { show: true, position: 'right', fontSize: 11, color: '#7f1d1d' },
        data: entries.map(([, count]) => count),
      }],
    })
  })
}

watch(summary, () => {
  void nextTick(() => renderChartsSafe())
})

function renderChartsSafe(): void {
  try {
    renderBars()
  } catch {
    // jsdom/极端布局下 echarts 初始化失败不阻塞页面
  }
}

onBeforeUnmount(() => {
  barChartMap.forEach((chart) => chart.dispose())
})

onMounted(async () => {
  const current = token()
  if (!current) {
    guest.value = true
    loading.value = false
    return
  }
  try {
    const response = await api.getLearningRecords(current)
    guest.value = response.guest
    records.value = response.records
    selectedId.value = response.records.at(-1)?.record_id ?? null
  } catch {
    loadError.value = '学习记录暂时无法加载，请稍后再试。'
  } finally {
    loading.value = false
  }
  if (props.role === 'admin') {
    try {
      const response = await api.getLearningSummary(current)
      summary.value = response.profiles
      await nextTick()
      renderChartsSafe()
    } catch {
      summaryError.value = '画像汇总暂时无法加载。'
    }
  }
})
</script>

<template>
  <section class="records-page" aria-label="学习记录">
    <!-- 优化10：返回按钮固定左上角（sticky），标题仅"岗位学习档案"置于其下方 -->
    <header class="records-head">
      <button
        type="button"
        class="records-back"
        aria-label="返回训练"
        @click="emit('close')"
      >
        <ArrowLeft :size="15" aria-hidden="true" /> 返回训练
      </button>
      <h2 class="records-title">岗位学习档案</h2>
    </header>

    <div v-if="loading" class="records-placeholder"><WaveText text="正在加载学习记录…" /></div>
    <div v-else-if="guest" class="records-placeholder">
      当前以游客身份学习，学习记录仅本次会话内可见。<br />
      登录后每次学习的掌握情况与用时都会自动归档到此处。
    </div>
    <div v-else-if="loadError" class="records-placeholder">{{ loadError }}</div>

    <template v-else>
      <div v-if="!records.length" class="records-placeholder">
        还没有完成过整轮知识点训练——完成一轮学习后，这里会展示每次的日期、用时与掌握档位。
      </div>

      <div v-else class="records-body">
        <div class="records-list" role="list" aria-label="历次学习">
          <button
            v-for="item in sortedRecords"
            :key="item.record_id"
            type="button"
            class="records-item"
            :class="{ 'is-active': item.record_id === selected?.record_id }"
            role="listitem"
            @click="selectedId = item.record_id"
          >
            <div class="records-item-main">
              <strong>{{ learnerText(item.knowledge_point) }}</strong>
              <small>{{ item.date }} · {{ item.start_time }}–{{ item.end_time }} · {{ formatDuration(item.duration_seconds) }}</small>
            </div>
            <div class="records-item-side">
              <span class="records-tier">{{ tierText(item.mastery[item.knowledge_point]) }}</span>
              <small>{{ profileTitle(item.profile_id) }}</small>
            </div>
          </button>
        </div>

        <div v-if="selected" class="records-detail">
          <header class="records-detail-head">
            <div>
              <span class="records-kicker">{{ selected.date }} · 用时 {{ formatDuration(selected.duration_seconds) }}</span>
              <h3>{{ learnerText(selected.knowledge_point) }}</h3>
            </div>
            <Radar :size="18" aria-hidden="true" />
          </header>

          <div class="records-radar">
            <DiagnosisRadar
              :dimensions="radarDimensions"
              :blind-spots="[]"
              :mastery-levels="radarLevels"
            />
          </div>

          <ul class="records-mastery" aria-label="各知识点掌握档位">
            <li v-for="(tier, point) in selected.mastery" :key="point">
              <span>{{ learnerText(String(point)) }}</span>
              <strong>{{ tierText(Number(tier)) }}</strong>
            </li>
          </ul>

          <div class="records-meta">
            <span>查询 {{ selected.query_count ?? 0 }} 次</span>
            <span>追问 {{ selected.follow_up_rounds ?? 0 }} 轮</span>
            <span>答错 {{ selected.wrong_answer_rounds ?? 0 }} 轮</span>
            <span v-if="selected.sql_failure_count">SQL 失误 {{ selected.sql_failure_count }} 次</span>
          </div>

          <div
            v-if="selected.misconception_hits && Object.keys(selected.misconception_hits).length"
            class="records-mistakes"
          >
            <strong>本轮流经的误区</strong>
            <ul>
              <li
                v-for="(count, misconception) in selected.misconception_hits"
                :key="misconception"
              >
                {{ misconceptionLabel(String(misconception)) }}
                <small>×{{ count }}</small>
              </li>
            </ul>
          </div>
        </div>
      </div>
    </template>

    <!-- 0818 需求 6：画像维度汇总（仅 admin 可见） -->
    <section
      v-if="props.role === 'admin'"
      class="records-summary"
      aria-label="画像学习总览"
    >
      <h3>画像学习总览</h3>
      <p v-if="summaryError" class="records-placeholder">{{ summaryError }}</p>
      <p v-else-if="summary && !summary.length" class="records-placeholder">
        暂无学员完成过整轮训练，汇总数据将在首批记录产生后出现。
      </p>
      <div v-else-if="summary" class="records-summary-grid">
        <article
          v-for="(profile, index) in summary"
          :key="profile.profile_id"
          class="records-summary-card"
        >
          <header>
            <strong>{{ profileTitle(profile.profile_id) }}</strong>
            <small>{{ profile.learner_count }} 名学员 · {{ profile.round_count }} 轮 · 平均 {{ formatDuration(profile.avg_duration_seconds) }}</small>
          </header>
          <!-- 优化10：每画像板块正下方以条形图展示各误区频次 -->
          <div
            v-if="profile.misconception_frequency && Object.keys(profile.misconception_frequency).length"
            class="records-mistake-chart"
            :aria-label="`${profileTitle(profile.profile_id)}误区频次`"
            :ref="(el) => setBarEl(index, el)"
          ></div>
          <p v-else class="records-summary-empty">该画像暂无误区命中记录。</p>
        </article>
      </div>
    </section>
  </section>
</template>
