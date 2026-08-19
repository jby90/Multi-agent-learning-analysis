<script setup lang="ts">
import { computed } from 'vue'
/** 优化15：等待提示波浪动画——逐字跳动（含省略号），表达"正在运行"而非卡死。

 * 支持多行文本（\n 保留换行）；每字按序延时起跳，循环往复。
 */
const props = withDefaults(defineProps<{
  text: string
  /** 每字动画延时间隔（毫秒） */
  stepMs?: number
  /** 单字一轮动画时长（毫秒） */
  durationMs?: number
}>(), {
  // 用户反馈（第二次）：再放慢——字间延时与单字周期进一步放缓
  stepMs: 200,
  durationMs: 2400,
})

interface WaveChar {
  key: string
  char: string
  delay: number
  isSpace: boolean
}

const lines = computed<WaveChar[][]>(() => {
  const result: WaveChar[][] = []
  let index = 0
  for (const line of String(props.text ?? '').split('\n')) {
    const chars: WaveChar[] = []
    for (const char of [...line]) {
      chars.push({
        key: `c${index}`,
        char,
        delay: index * props.stepMs,
        isSpace: char.trim() === '',
      })
      index += 1
    }
    result.push(chars)
  }
  return result
})
</script>

<template>
  <span class="wave-text" :style="{ '--wave-duration': `${durationMs}ms` }">
    <span
      v-for="(line, lineIndex) in lines"
      :key="`l${lineIndex}`"
      class="wave-line"
      :class="{ 'is-multiline': lines.length > 1 }"
    >
      <span
        v-for="item in line"
        :key="item.key"
        class="wave-char"
        :class="{ 'is-space': item.isSpace }"
        :style="{ 'animation-delay': `${item.delay}ms` }"
      >{{ item.char }}</span>
    </span>
  </span>
</template>
