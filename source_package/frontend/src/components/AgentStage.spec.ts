import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AgentActivityEvent } from '../lib/interactiveApi'
import type { TraceView } from '../types/trace'
import AgentStage from './AgentStage.vue'


const view: TraceView = {
  visibleMessages: [],
  currentState: 'S2_KNOWLEDGE',
  knowledgeDimensions: [],
  debateGroups: [],
}

function event(
  sequence: number,
  agent: AgentActivityEvent['agent'],
  status: AgentActivityEvent['status'],
  label: string,
  peers: AgentActivityEvent['peers'] = [],
  details?: Record<string, unknown>,
): AgentActivityEvent {
  return {
    sequence,
    trace_id: 'interactive-1',
    agent,
    status,
    activity: 'test',
    label,
    stage: 'S5_REVIEW',
    peers,
    timestamp: '2026-07-27T10:00:00Z',
    details,
  }
}

describe('AgentStage', () => {
  it('falls back to the authoritative trace state during replay', () => {
    const wrapper = mount(AgentStage, { props: { view } })
    expect(wrapper.get('[data-agent="knowledge"]').classes()).toContain('is-working')
    expect(wrapper.get('[data-teacher-agent="knowledge"]').attributes('data-teacher-state')).toBe('working')
    expect(wrapper.text()).toContain('领域知识正在执行当前阶段')
  })

  it('renders collaboration and approach states from live events', () => {
    const events = [
      event(1, 'knowledge', 'collaborating', '候选产物已送审', ['review']),
      {
        ...event(
          2,
          'review',
          'reviewing',
          '正在执行质量审查',
          ['knowledge'],
          { fan_out: 2, aggregation: 'deterministic' },
        ),
        activity: 'parallel_quality_review',
      },
    ]
    const wrapper = mount(AgentStage, { props: { view, events } })
    expect(wrapper.get('[data-agent="knowledge"]').classes()).toContain('is-approaching')
    expect(wrapper.get('[data-agent="review"]').classes()).toContain('is-reviewing')
    expect(wrapper.get('[data-teacher-agent="review"]').attributes('data-teacher-state')).toBe('collaborating')
    expect(wrapper.text()).toContain('正在执行质量审查')
    expect(wrapper.text()).toContain('2 路并发审核')
    expect(wrapper.find('.parallel-review-flow').exists()).toBe(false)
    expect(wrapper.findAll('.agent-packet')).toHaveLength(4)
  })

  it('summarizes parallel review without adding a central overlay', () => {
    const parallel = {
      ...event(
        5,
        'review',
        'collaborating',
        '事实证据与难度适配正在双路并行审核',
        ['knowledge'],
        { fan_out: 2, aggregation: 'pending' },
      ),
      activity: 'parallel_quality_review',
    }
    const wrapper = mount(AgentStage, { props: { view, events: [parallel] } })

    expect(wrapper.text()).toContain('2 路并发审核')
    expect(wrapper.text()).toContain('事实证据与难度适配正在双路并行审核')
    expect(wrapper.find('.parallel-review-flow').exists()).toBe(false)
    expect(wrapper.findAll('.audit-packet')).toHaveLength(0)
  })

  it('lets the presenter pause and resume all stage motion', async () => {
    const wrapper = mount(AgentStage, { props: { view } })
    const toggle = wrapper.get('.motion-toggle')

    expect(toggle.text()).toContain('FLOW 00%')
    expect(wrapper.get('.agent-stage').classes()).not.toContain('is-motion-paused')

    await toggle.trigger('click')
    expect(toggle.text()).toContain('已暂停 00%')
    expect(wrapper.get('.agent-stage').classes()).toContain('is-motion-paused')

    await toggle.trigger('click')
    expect(toggle.text()).toContain('FLOW 00%')
    expect(wrapper.get('.agent-stage').classes()).not.toContain('is-motion-paused')
  })

  it('shows a visible blocked state without exposing hidden reasoning', () => {
    const events = [event(3, 'verification', 'blocked', '安全校验已阻断查询')]
    const wrapper = mount(AgentStage, { props: { view, events } })
    expect(wrapper.get('[data-agent="verification"]').classes()).toContain('is-blocked')
    expect(wrapper.text()).toContain('已阻断')
  })
})
