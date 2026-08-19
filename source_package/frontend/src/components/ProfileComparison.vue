<script setup lang="ts">
import * as echarts from 'echarts/core'
import { LegendComponent, TooltipComponent } from 'echarts/components'
import { RadarChart } from 'echarts/charts'
import { SVGRenderer } from 'echarts/renderers'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { learnerText } from '../lib/tracePresentation'
import { computeCoverageTiers, tierFromDifficulty } from '../lib/coverageTiers'
import type { KnowledgeCatalogEntry, TraceDocument, TraceView } from '../types/trace'


export interface ComparisonEntry {
  document: TraceDocument
  view: TraceView
  /** 正在回放的会话：数据跟随回放光标，其余定格会话终态作对照。 */
  isCurrent?: boolean
}

const props = defineProps<{
  entries: ComparisonEntry[]
  catalog: KnowledgeCatalogEntry[]
}>()

echarts.use([RadarChart, LegendComponent, TooltipComponent, SVGRenderer])

/** 优化8：三画像只保留"知识点覆盖范围及掌握档位"重叠蛛网图。
 * 此前的画像信息卡、难度折线、掌握扇形、底部对照条已按需求删除。 */
const PROFILE_COLORS: Record<string, string> = {
  planner_new: '#2563eb',
  craft_engineer: '#7c3aed',
  line_leader: '#ea580c',
}

function profileTitle(entry: ComparisonEntry): string {
  const profile = entry.view.profile as { title?: string } | undefined
  return learnerText(profile?.title || entry.document.profileId || '画像')
}

function profileColor(entry: ComparisonEntry): string {
  return PROFILE_COLORS[entry.document.profileId ?? ''] ?? '#64748b'
}

const ordered = computed(() => {
  const order = ['planner_new', 'craft_engineer', 'line_leader']
  return [...props.entries].sort(
    (left, right) => order.indexOf(left.document.profileId ?? '') - order.indexOf(right.document.profileId ?? ''),
  )
})

/**
 * 档位取值=双真实数据源（均为会话内可核查的证据）：
 * ① 训练覆盖：会话消息中该知识点讲义/任务达到的最高难度（coverageTiers）；
 * ② 诊断起点：v4 计划（T02 画像评估产物）为领域内每个知识点判定的
 *    initial_difficulty——前测答对(correct)与答错(wrong)的起点档均真实有据。
 * 计划恰好覆盖画像领域全集（correct 通道 + 待训通道），域外知识点不在计划中=0。
 * 训练过的点取实际达到档，未训练的点取诊断起点档。
 */
function planStartTiers(entry: ComparisonEntry): Map<string, number> {
  const plan = entry.view.diagnosis?.content.knowledge_point_plan
  const tiers = new Map<string, number>()
  if (!Array.isArray(plan)) return tiers
  for (const item of plan) {
    if (typeof item !== 'object' || item === null) continue
    const record = item as Record<string, unknown>
    const point = String(record.knowledge_point ?? '')
    if (!point) continue
    const tier = tierFromDifficulty(record.initial_difficulty)
    if (tier > 0) tiers.set(point, tier)
  }
  return tiers
}

function tierOf(entry: ComparisonEntry, point: string): number {
  const trained = computeCoverageTiers(entry.view, [point])[0] ?? 0
  if (trained > 0) return trained
  return planStartTiers(entry).get(point) ?? 0
}

const radarDimensions = computed(() => props.catalog
  .map((entry) => entry.knowledgePoint)
  .filter((point): point is string => typeof point === 'string' && point.trim() !== ''))

function radarAxisName(point: string): string {
  // 长知识点名两行排布，避免多轴拥挤重叠
  const value = point ?? ''
  return value.length > 5 ? `${value.slice(0, 5)}\n${value.slice(5)}` : value
}

const radarEl = ref<HTMLElement | null>(null)
let radarChart: echarts.ECharts | null = null
/** 优化14：首播动画（从 0 覆盖到最终档位）只播一次，此后数据跟随不再重播 */
let radarIntroduced = false

function chartOption(values: number[][] | null, animate: boolean): echarts.EChartsCoreOption {
  const final = ordered.value.map((entry) => ({
    name: profileTitle(entry),
    value: radarDimensions.value.map((point) => tierOf(entry, point)),
  }))
  const data = values
    ? ordered.value.map((entry, index) => ({ name: profileTitle(entry), value: values[index] ?? [] }))
    : final
  return {
    animation: animate,
    animationDuration: animate ? 1400 : 0,
    animationEasing: 'cubicOut',
    color: ordered.value.map(profileColor),
    tooltip: { confine: true },
    legend: {
      bottom: 0,
      icon: 'circle',
      itemWidth: 9,
      itemHeight: 9,
      textStyle: { fontSize: 12, color: '#475569' },
    },
    radar: {
      indicator: radarDimensions.value.map((point) => ({
        name: radarAxisName(point),
        max: 3,
      })),
      splitNumber: 3,
      axisName: { fontSize: 11, color: '#64748b', lineHeight: 14 },
      radius: '64%',
      center: ['50%', '46%'],
    },
    series: [{
      type: 'radar',
      symbolSize: 5,
      areaStyle: { opacity: 0.1 },
      lineStyle: { width: 2.5 },
      label: { show: false },
      data,
    }],
  }
}

function renderChart(): void {
  // 空数据守卫：indicator 为空时 echarts 布局会崩
  if (!radarEl.value || !ordered.value.length || !radarDimensions.value.length) return
  radarChart ??= echarts.init(radarEl.value)
  if (radarIntroduced) {
    radarChart.setOption(chartOption(null, false))
    return
  }
  // 优化14：动态覆盖动画——三画像依次从中心展开到最终档位，最终定格完整覆盖情况
  radarIntroduced = true
  const zeros = ordered.value.map(() => radarDimensions.value.map(() => 0))
  radarChart.setOption(chartOption(zeros, false))
  window.setTimeout(() => {
    radarChart?.setOption(chartOption(null, true))
  }, 120)
}

onMounted(() => {
  requestAnimationFrame(() => renderChart())
})

watch(
  () => [
    props.entries.map((entry) => `${entry.document.traceId}:${entry.isCurrent}:${entry.view.visibleMessages.length}`).join('|'),
    props.catalog.length,
  ],
  () => renderChart(),
)

onBeforeUnmount(() => {
  radarChart?.dispose()
})
</script>

<template>
  <section class="panel profile-comparison" data-testid="profile-comparison">
    <header class="panel-heading comparison-heading">
      <h2>岗位对比</h2>
    </header>
    <div class="comparison-radar-only">
      <p class="comparison-subtitle is-large">知识点覆盖范围及掌握档位（三画像）</p>
      <div ref="radarEl" class="chart-canvas is-radar-full"></div>
    </div>
  </section>
</template>
