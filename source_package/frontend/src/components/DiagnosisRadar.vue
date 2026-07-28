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
}>()

const root = ref<HTMLDivElement>()
let chart: EChartsType | undefined
let resizeObserver: ResizeObserver | undefined

const values = computed(() => props.dimensions.map(
  (dimension) => props.blindSpots.includes(dimension) ? 0 : 1,
))

function render(): void {
  if (!root.value || !props.dimensions.length) return
  chart ??= init(root.value, undefined, { renderer: 'svg' })
  chart.setOption({
    animationDuration: 300,
    tooltip: {
      trigger: 'item',
      backgroundColor: '#F7F5F0',
      borderColor: '#B8B2A8',
      textStyle: { color: '#2A2A28', fontSize: 12 },
      formatter: () => props.dimensions.map(
        (dimension, index) => `${dimension}：${values.value[index] === 0 ? '已识别盲区' : '暂未标记'}`,
      ).join('<br/>'),
    },
    radar: {
      radius: '55%',
      splitNumber: 1,
      indicator: props.dimensions.map((name) => ({ name: abbreviateDimension(name), max: 1 })),
      axisName: { color: '#57534E', fontSize: 10 },
      axisLine: { lineStyle: { color: '#B8B2A8' } },
      splitLine: { lineStyle: { color: '#B8B2A8' } },
      splitArea: { areaStyle: { color: ['#EFECE5'] } },
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

onMounted(() => {
  render()
  if (root.value && typeof ResizeObserver !== 'undefined') {
    resizeObserver = new ResizeObserver(() => chart?.resize())
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
    :aria-label="`十个知识点的学情诊断雷达图：${dimensions.join('、')}`"
  ></div>
</template>
