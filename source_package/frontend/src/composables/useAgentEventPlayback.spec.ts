import { effectScope } from 'vue'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { AgentActivityEvent } from '../lib/interactiveApi'
import { agentEventHoldMs, useAgentEventPlayback } from './useAgentEventPlayback'


function event(
  sequence: number,
  status: AgentActivityEvent['status'],
  activity: string,
  aggregation?: string,
): AgentActivityEvent {
  return {
    sequence,
    trace_id: 'trace-playback',
    agent: 'review',
    status,
    activity,
    label: `event-${sequence}`,
    stage: 'S5_REVIEW',
    peers: ['knowledge'],
    timestamp: '2026-07-28T02:00:00Z',
    details: aggregation ? { aggregation } : undefined,
  }
}

afterEach(() => {
  vi.useRealTimers()
})

describe('useAgentEventPlayback', () => {
  it('makes review, parallel execution, and join states perceptible in order', () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const playback = scope.run(() => useAgentEventPlayback())!
    const review = event(1, 'reviewing', 'quality_gate')
    const running = event(2, 'collaborating', 'parallel_quality_review', 'pending')
    const joined = event(3, 'reviewing', 'parallel_quality_review', 'deterministic')
    const approved = event(4, 'approved', 'quality_gate')

    playback.receive(review)
    playback.receive(running)
    playback.receive(joined)
    playback.receive(approved)
    expect(playback.events.value.map((item) => item.sequence)).toEqual([1])

    vi.advanceTimersByTime(agentEventHoldMs(review))
    expect(playback.events.value.map((item) => item.sequence)).toEqual([1, 2])

    vi.advanceTimersByTime(agentEventHoldMs(running))
    expect(playback.events.value.map((item) => item.sequence)).toEqual([1, 2, 3])

    vi.advanceTimersByTime(agentEventHoldMs(joined))
    expect(playback.events.value.map((item) => item.sequence)).toEqual([1, 2, 3, 4])
    scope.stop()
  })

  it('deduplicates events and clears queued states on reset', () => {
    vi.useFakeTimers()
    const scope = effectScope()
    const playback = scope.run(() => useAgentEventPlayback())!
    const review = event(1, 'reviewing', 'quality_gate')

    playback.receive(review)
    playback.receive(review)
    playback.receive(event(2, 'collaborating', 'parallel_quality_review', 'pending'))
    playback.reset()
    vi.runAllTimers()

    expect(playback.events.value).toEqual([])
    scope.stop()
  })

  it('holds the deterministic hard-rule route long enough to explain skipped debate', () => {
    const routed = event(9, 'blocked', 'deterministic_rejection_route')
    const debate = event(10, 'debating', 'bounded_debate')

    expect(agentEventHoldMs(routed)).toBeGreaterThan(agentEventHoldMs(debate))
  })

  it('keeps resource fan-out and join events perceptible', () => {
    const working = event(11, 'working', 'parallel_resource_generation', 'pending')
    const joined = event(12, 'done', 'parallel_resource_generation', 'deterministic')

    expect(agentEventHoldMs(working)).toBeGreaterThan(0)
    expect(agentEventHoldMs(joined)).toBeGreaterThan(agentEventHoldMs(working))
  })

  it('keeps evidence fan-out and deterministic bundle join perceptible', () => {
    const working = event(13, 'working', 'parallel_evidence_retrieval', 'pending')
    const joined = event(14, 'done', 'parallel_evidence_retrieval', 'deterministic')

    expect(agentEventHoldMs(working)).toBeGreaterThan(0)
    expect(agentEventHoldMs(joined)).toBeGreaterThan(agentEventHoldMs(working))
  })
})
