<script setup lang="ts">
import { RadarChart } from 'echarts/charts'
import { TooltipComponent } from 'echarts/components'
import { init, use, type EChartsType } from 'echarts/core'
import { SVGRenderer } from 'echarts/renderers'
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import { abbreviateDimension } from '../lib/tracePresentation'


use([RadarChart, TooltipComponent, SVGRenderer])

const props = defineProps<{
  dimensions: string[]
  blindSpots: string[]
  coveredPoints?: string[]
  /** 各维度已达到的档位（0-3：0 未学，1 基础，2 应用，3 进阶）；缺省按盲区二值回退。 */
  masteryLevels?: number[]
}>()

const root = ref<HTMLDivElement>()
let chart: EChartsType | undefined
let resizeObserver: ResizeObserver | undefined

const coveredSet = computed(() => new Set(props.coveredPoints ?? []))
/** 需求⑤：取值随实际学习档位变化——盲区 0，其余按已达到档位（默认基础=内环）。 */
const values = computed(() => props.dimensions.map((dimension, index) => {
  if (props.blindSpots.includes(dimension)) return 0
  const level = props.masteryLevels?.[index]
  if (typeof level === 'number' && Number.isFinite(level)) {
    return Math.min(3, Math.max(1, level)) / 3
  }
  return 1 / 3
}))
// 蛛网三层环线（需求③：不再标注档位字样）
const TIER_COUNT = 3

function dimensionState(dimension: string): string {
  if (coveredSet.value.has(dimension)) return '已覆盖'
  if (props.blindSpots.includes(dimension)) return '已识别盲区'
  return '暂未标记'
}

function render(): void {
  if (!root.value || !props.dimensions.length) return
  // 需求④：容器可能刚从 details 展开但尺寸尚未稳定——先同步一次尺寸再绘制
  if (chart) chart.resize()
  chart ??= init(root.value, undefined, { renderer: 'svg' })
  chart.setOption({
    animationDuration: 300,
    tooltip: {
      trigger: 'item',
      backgroundColor: '#F7F5F0',
      borderColor: '#B8B2A8',
      textStyle: { color: '#2A2A28', fontSize: 12 },
      formatter: () => props.dimensions.map(
        (dimension) => `${dimension}：${dimensionState(dimension)}`,
      ).join('<br/>'),
    },
    radar: {
      // 需求③：去档位字样后放大蛛网并整体上移，减少四周留白
      radius: '58%',
      center: ['50%', '46%'],
      splitNumber: TIER_COUNT,
      indicator: props.dimensions.map((name) => ({ name: abbreviateDimension(name), max: 1 })),
      axisName: { color: '#57534E', fontSize: 10 },
      axisLine: { lineStyle: { color: '#B8B2A8' } },
      splitLine: { lineStyle: { color: '#B8B2A8' } },
      splitArea: { areaStyle: { color: ['rgba(239, 236, 229, 0.55)', 'rgba(239, 236, 229, 0)'] } },
    },
    series: [{
      type: 'radar',
      symbol: 'circle',
      symbolSize: 4,
      lineStyle: { color: '#2F81F7', width: 1.5 },
      itemStyle: { color: '#2F81F7' },
      areaStyle: { color: 'rgba(47, 129, 247, 0.14)' },
      data: [{ value: values.value }],
    }],
  }, true)
}

onMounted(async () => {
  // 需求④：details 展开后容器尺寸可能仍在过渡——等一帧再初始化，避免蛛网缩小+标签截断
  await new Promise((resolve) => requestAnimationFrame(() => resolve(null)))
  render()
  if (root.value && typeof ResizeObserver !== 'undefined') {
    // 尺寸变化时整图重绘（先 resize 同步容器实际尺寸再 setOption）
    resizeObserver = new ResizeObserver(() => {
      chart?.resize()
      render()
    })
    resizeObserver.observe(root.value)
  }
})

watch(() => [props.dimensions, props.blindSpots], render, { deep: true })

onBeforeUnmount(() => {
  resizeObserver?.disconnect()
  chart?.dispose()
})
</script>

<template>
  <div
    v-if="dimensions.length"
    ref="root"
    class="diagnosis-radar"
    role="img"
    :aria-label="`十个知识点的学情诊断蛛网图：${dimensions.join('、')}`"
  ></div>
</template>
