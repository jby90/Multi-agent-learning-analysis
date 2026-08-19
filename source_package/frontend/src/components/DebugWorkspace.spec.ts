import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  AgentActivityEvent,
  InteractiveApi,
  InteractiveState,
} from '../lib/interactiveApi'
import { demoTrace } from '../test/traceFixtures'
import DebugWorkspace from './DebugWorkspace.vue'

function state(traceId: string, sessionId: string): InteractiveState {
  const messages = demoTrace(
    traceId,
    'planner_new',
    '新入职生产计划员',
    '计划量与实际量必须分开理解。',
    '1156.87',
  ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
  return {
    session_id: sessionId,
    trace_id: traceId,
    trace_path: `traces/${traceId}.jsonl`,
    state: 'S5_REVIEW',
    awaiting: 'advance',
    mode: 'live',
    profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
    messages,
    artifact: null,
    interaction: null,
  }
}

function activity(traceId: string, sequence: number, label: string): AgentActivityEvent {
  return {
    trace_id: traceId,
    sequence,
    agent: 'evidence_review',
    status: 'approved',
    activity: 'specialist_quality_review',
    label,
    stage: 'S5_REVIEW',
    peers: ['review'],
    timestamp: '2026-08-05T06:00:00Z',
    details: { cycle: 1, raw_prompt: 'must-not-render' },
  }
}

describe('DebugWorkspace', () => {
  afterEach(() => sessionStorage.clear())

  it('binds to the current browser session and ignores another trace event', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-current')
    sessionStorage.setItem('ref-interactive-trace', 'trace-current')
    const getState = vi.fn(async () => state('trace-current', 'session-current'))
    const api: InteractiveApi = {
      createSession: vi.fn(),
      getState,
      getPretest: vi.fn(),
      getDiagnosticProbes: vi.fn(),
      submitPretest: vi.fn(),
      submitDiagnosticProbes: vi.fn(),
      advance: vi.fn(),
      continueLearning: vi.fn(),
      submitSql: vi.fn(),
      submitFollowUp: vi.fn(),
      getLearningRecords: vi.fn(async () => ({ guest: true, records: [] })),
      getLearningSummary: vi.fn(async () => ({ profiles: [] })),
      subscribeAgentEvents: (_sessionId, onEvent) => {
        onEvent(activity('trace-other-user', 1, '其他用户事件'))
        onEvent(activity('trace-current', 2, '当前会话审核通过'))
        onEvent({
          ...activity('trace-current', 3, '教学适配审核阻断'),
          agent: 'pedagogy_review',
          status: 'blocked',
        })
        return vi.fn()
      },
    }

    const wrapper = mount(DebugWorkspace, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    expect(getState).toHaveBeenCalledWith('session-current')
    expect(wrapper.text()).toContain('当前浏览器会话')
    expect(wrapper.text()).toContain('当前会话审核通过')
    expect(wrapper.text()).not.toContain('其他用户事件')
    expect(wrapper.text()).not.toContain('must-not-render')
    expect(wrapper.get('[data-node="evidence_review"]').classes()).toContain('is-approved')
    expect(wrapper.get('[data-node="pedagogy_review"]').classes()).toContain('is-blocked')
  })

  it('rejects a trace binding mismatch instead of switching sessions', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-current')
    sessionStorage.setItem('ref-interactive-trace', 'trace-current')
    const api = {
      getState: vi.fn(async () => state('trace-other-user', 'session-current')),
    } as unknown as InteractiveApi

    const wrapper = mount(DebugWorkspace, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    expect(wrapper.text()).toContain('当前会话已失效或不属于这个浏览器上下文')
    expect(wrapper.find('.agent-topology').exists()).toBe(false)
  })
})
