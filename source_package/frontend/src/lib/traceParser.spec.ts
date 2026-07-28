import { describe, expect, it } from 'vitest'

import { TraceParseError, parseTraceJsonl } from './traceParser'


function line(value: unknown): string {
  return JSON.stringify(value)
}

function message(
  step: number,
  overrides: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    msg_id: `demo-a-${String(step).padStart(3, '0')}`,
    trace_id: 'demo-a',
    step,
    agent: 'system',
    role: 'system',
    payload: { type: 'control', content: { event: 'noop' } },
    evidence: [],
    claims: [],
    timestamp: '2026-07-16T02:00:00+00:00',
    ...overrides,
  }
}

function validSource(): string {
  return [
    message(2, {
      msg_id: 'demo-a-002',
      payload: {
        type: 'control',
        content: {
          action: 'profile_loaded',
          profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
        },
      },
    }),
    message(1, {
      msg_id: 'demo-a-001',
      payload: {
        type: 'control',
        content: {
          action: 'session_start',
          state: 'S0_INIT',
          student_profile_ref: 'planner_new',
        },
      },
    }),
  ].map(line).join('\n')
}

describe('parseTraceJsonl', () => {
  it('sorts steps and exposes a camel-case protocol boundary', () => {
    const trace = parseTraceJsonl(validSource(), 'demo-a.jsonl')

    expect(trace.traceId).toBe('demo-a')
    expect(trace.fileName).toBe('demo-a.jsonl')
    expect(trace.messages.map((item) => item.step)).toEqual([1, 2])
    expect(trace.messages[1]?.msgId).toBe('demo-a-002')
    expect(trace.messages[1]?.payloadType).toBe('control')
    expect(trace.messages[1]?.content.profile).toEqual({
      profile_id: 'planner_new',
      title: '新入职生产计划员',
    })
  })

  it('reports malformed JSON in Chinese without an English stack', () => {
    expect(() => parseTraceJsonl(`${line(message(1))}\n{"broken"`, 'bad.jsonl'))
      .toThrowError(TraceParseError)
    expect(() => parseTraceJsonl(`${line(message(1))}\n{"broken"`, 'bad.jsonl'))
      .toThrow('第2行不是有效的会话记录')
  })

  it('rejects duplicate or non-contiguous steps', () => {
    const duplicate = [message(1), message(1)].map(line).join('\n')
    expect(() => parseTraceJsonl(duplicate, 'duplicate.jsonl')).toThrow(
      '步骤编号重复',
    )

    const gap = [message(1), message(3)].map(line).join('\n')
    expect(() => parseTraceJsonl(gap, 'gap.jsonl')).toThrow(
      '步骤编号必须从1连续递增',
    )
  })

  it('rejects inconsistent trace ids', () => {
    const source = [
      message(1),
      message(2, { trace_id: 'demo-b', msg_id: 'demo-b-002' }),
    ].map(line).join('\n')

    expect(() => parseTraceJsonl(source, 'mixed.jsonl')).toThrow(
      '包含多个会话编号',
    )
  })

  it('requires the session-start record used by replay', () => {
    const source = line(message(1, {
      payload: { type: 'control', content: { action: 'profile_loaded' } },
    }))

    expect(() => parseTraceJsonl(source, 'no-start.jsonl')).toThrow(
      '缺少会话建立记录',
    )
  })
})
