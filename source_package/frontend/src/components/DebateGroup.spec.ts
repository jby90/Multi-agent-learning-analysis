import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AgentId, RoleId, TraceMessage } from '../types/trace'
import DebateGroup from './DebateGroup.vue'


function message(
  step: number,
  agent: AgentId,
  role: RoleId,
  decision?: 'reject' | 'approve',
): TraceMessage {
  return {
    msgId: `debate-${step}`,
    traceId: 'debate',
    step,
    agent,
    role,
    payloadType: role === 'rebuttal' ? 'rebuttal_case' : 'review_verdict',
    content: { event: `event-${step}` },
    evidence: [],
    claims: [],
    ...(decision ? { verdict: { decision, ruleHits: [] } } : {}),
    timestamp: `2026-07-17T02:00:${String(step).padStart(2, '0')}+00:00`,
    rejectedByBus: false,
    busErrors: [],
  }
}


describe('DebateGroup', () => {
  it('labels the trace-backed rejection, rebuttal, and re-review in order', () => {
    const wrapper = mount(DebateGroup, {
      props: {
        group: {
          id: 'debate-1',
          messages: [
            message(1, 'review', 'verdict', 'reject'),
            message(2, 'knowledge', 'rebuttal'),
            message(3, 'review', 're_verdict', 'approve'),
          ],
        },
      },
    })

    expect(wrapper.findAll('.debate-phase').map((item) => item.text()))
      .toEqual(['驳回', '补充说明', '再次审核'])
    expect(wrapper.findAll('.debate-cards .trace-card')).toHaveLength(3)
    expect(wrapper.findAll('.debate-cards .trace-card').map((item) => (
      item.classes().find((name) => name.startsWith('role-'))
    ))).toEqual(['role-verdict', 'role-rebuttal', 'role-re_verdict'])
  })
})
