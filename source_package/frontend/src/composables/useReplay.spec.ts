import { effectScope, ref } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useReplay } from './useReplay'


describe('useReplay', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('plays, pauses, steps, changes speed, jumps, and resets on trace change', () => {
    vi.useFakeTimers()
    const total = ref(3)
    const scope = effectScope()
    const replay = scope.run(() => useReplay(total, { baseIntervalMs: 1000 }))
    if (!replay) throw new Error('回放控制未创建')

    expect(replay.cursor.value).toBe(3)
    replay.play()
    expect(replay.cursor.value).toBe(0)
    vi.advanceTimersByTime(1000)
    expect(replay.cursor.value).toBe(1)
    replay.pause()
    vi.advanceTimersByTime(1000)
    expect(replay.cursor.value).toBe(1)

    replay.setSpeed(2)
    replay.play()
    vi.advanceTimersByTime(499)
    expect(replay.cursor.value).toBe(1)
    vi.advanceTimersByTime(1)
    expect(replay.cursor.value).toBe(2)
    replay.step()
    expect(replay.playing.value).toBe(false)
    expect(replay.cursor.value).toBe(3)

    replay.jumpTo(2)
    expect(replay.cursor.value).toBe(2)
    total.value = 5
    expect(replay.cursor.value).toBe(5)
    scope.stop()
  })
})
