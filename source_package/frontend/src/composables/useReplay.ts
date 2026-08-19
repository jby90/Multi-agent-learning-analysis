import {
  onScopeDispose,
  ref,
  watch,
  type Ref,
} from 'vue'


export type ReplaySpeed = 1 | 2 | 5

export interface ReplayOptions {
  baseIntervalMs?: number
}


export function useReplay(total: Readonly<Ref<number>>, options: ReplayOptions = {}) {
  const baseIntervalMs = options.baseIntervalMs ?? 900
  const cursor = ref(Math.max(0, total.value))
  const playing = ref(false)
  const speed = ref<ReplaySpeed>(1)
  let timer: ReturnType<typeof setInterval> | undefined

  function clearTimer(): void {
    if (timer !== undefined) {
      clearInterval(timer)
      timer = undefined
    }
  }

  function pause(): void {
    playing.value = false
    clearTimer()
  }

  function tick(): void {
    if (cursor.value < total.value) cursor.value += 1
    if (cursor.value >= total.value) pause()
  }

  function schedule(): void {
    clearTimer()
    timer = setInterval(tick, baseIntervalMs / speed.value)
  }

  function play(): void {
    if (total.value <= 0) return
    if (cursor.value >= total.value) cursor.value = 0
    if (playing.value) return
    playing.value = true
    schedule()
  }

  function step(): void {
    pause()
    if (cursor.value < total.value) cursor.value += 1
  }

  function stepBack(): void {
    pause()
    if (cursor.value > 0) cursor.value -= 1
  }

  function restart(): void {
    pause()
    cursor.value = 0
  }

  function setSpeed(value: ReplaySpeed): void {
    speed.value = value
    if (playing.value) schedule()
  }

  function jumpTo(stepNumber: number): void {
    pause()
    cursor.value = Math.max(0, Math.min(Math.trunc(stepNumber), total.value))
  }

  watch(
    total,
    (value) => {
      pause()
      cursor.value = Math.max(0, value)
    },
    { flush: 'sync' },
  )

  onScopeDispose(clearTimer)

  return {
    stepBack,
    restart,
    cursor,
    playing,
    speed,
    play,
    pause,
    step,
    setSpeed,
    jumpTo,
  }
}
