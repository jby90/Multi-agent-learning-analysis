import { describe, expect, it } from 'vitest'

import { assertReplayTraceSafe, isReplayTraceFile } from './traceAssetsPlugin'


describe('isReplayTraceFile', () => {
  it('accepts only the three profile replay trace names', () => {
    const official = [
      'demo-planner_new-20260716133542.jsonl',
      'demo-craft_engineer-20260716133542.jsonl',
      'demo-line_leader-20260716133542.jsonl',
    ]
    const legacyOrMalformed = [
      'demo-0148752f24d1.jsonl',
      'demo-8dfba20df2fd.jsonl',
      'demo-planner_new-2026071613354.jsonl',
      'demo-craft_engineer-debate-20260716133542.jsonl',
      'demo-planner_new-debate-20260716134230.jsonl',
      'demo-planner_new-20260716133542.jsonl.bak',
    ]

    expect(official.every(isReplayTraceFile)).toBe(true)
    expect(legacyOrMalformed.some(isReplayTraceFile)).toBe(false)
  })
})


describe('assertReplayTraceSafe', () => {
  it('accepts an ordinary reviewed message', () => {
    const contents = JSON.stringify({
      agent: 'review',
      role: 'verdict',
      payload: { type: 'review_verdict', content: { event: 'review_complete' } },
      verdict: { decision: 'approve', rule_hits: [] },
    })

    expect(() => assertReplayTraceSafe('demo-planner_new-20260716133542.jsonl', contents))
      .not.toThrow()
  })

  it.each([
    'injected_for_demo',
    'injection_label',
    '人工误驳',
    '人工误判',
    '人工注入',
    '故障注入',
  ])('rejects a replay containing %s before it enters the manifest', (marker) => {
    const contents = JSON.stringify({ content: { note: marker } })

    expect(() => assertReplayTraceSafe(
      'demo-planner_new-debate-20260716134230.jsonl',
      contents,
    )).toThrow(/不符合正式回放要求/u)
  })
})
