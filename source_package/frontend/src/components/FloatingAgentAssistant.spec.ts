import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it } from 'vitest'

import type { AgentActivityEvent } from '../lib/interactiveApi'
import type { TraceView } from '../types/trace'
import FloatingAgentAssistant from './FloatingAgentAssistant.vue'


const view: TraceView = {
  visibleMessages: [],
  currentState: 'S5_REVIEW',
  knowledgeDimensions: [],
  debateGroups: [],
}

function event(
  sequence: number,
  agent: AgentActivityEvent['agent'],
  status: AgentActivityEvent['status'],
  label: string,
): AgentActivityEvent {
  return {
    sequence,
    trace_id: 'interactive-1',
    agent,
    status,
    activity: 'quality_gate',
    label,
    stage: 'S5_REVIEW',
    peers: [],
    timestamp: '2026-07-30T10:00:00Z',
  }
}

describe('FloatingAgentAssistant', () => {
  afterEach(() => localStorage.clear())

  it('uses the latest active backend event and expands into progress details', async () => {
    const wrapper = mount(FloatingAgentAssistant, {
      props: {
        view,
        events: [
          event(1, 'knowledge', 'done', '微课已经准备完成'),
          event(2, 'review', 'reviewing', '正在检查事实与表达质量'),
        ],
      },
    })

    expect(wrapper.get('[data-teacher-agent="review"]').attributes('data-teacher-state'))
      .toBe('collaborating')
    expect(wrapper.text()).toContain('正在审核')

    await wrapper.get('button[aria-label="查看后台助手进度"]').trigger('click')

    expect(wrapper.classes()).toContain('is-expanded')
    expect(wrapper.text()).toContain('正在检查事实与表达质量')
    expect(wrapper.text()).toContain('78%')
    expect(wrapper.text()).toContain('最近完成')
    expect(wrapper.text()).toContain('微课已经准备完成')
  })

  it('falls back to the trace state before a live event arrives', () => {
    const wrapper = mount(FloatingAgentAssistant, { props: { view, events: [] } })

    expect(wrapper.get('[data-teacher-agent="review"]')).toBeTruthy()
    expect(wrapper.text()).toContain('正在工作')
  })

  it('shows a safe stop instead of stale stage progress after review blocks the flow', async () => {
    const wrapper = mount(FloatingAgentAssistant, {
      props: {
        view: { ...view, currentState: 'S7_STUDENT' },
        events: [
          event(1, 'task', 'working', '正在生成自适应追问'),
          event(2, 'task', 'idle', '本轮已安全停止，等待重新开始'),
          event(3, 'review', 'blocked', '质量门已安全阻断该产物'),
        ],
      },
    })

    await wrapper.get('button[aria-label="查看后台助手进度"]').trigger('click')

    expect(wrapper.text()).toContain('需要重试')
    expect(wrapper.text()).toContain('已安全停止')
    expect(wrapper.text()).not.toContain('88%')
  })

  it('can be dragged and remembers its learner-selected position', async () => {
    const wrapper = mount(FloatingAgentAssistant, { props: { view, events: [] } })

    wrapper.get('.floating-agent-handle').element.dispatchEvent(new MouseEvent('pointerdown', {
      button: 0,
      clientX: 80,
      clientY: 80,
      bubbles: true,
    }))
    window.dispatchEvent(new MouseEvent('pointermove', { clientX: 240, clientY: 210 }))
    window.dispatchEvent(new MouseEvent('pointerup'))

    expect(wrapper.attributes('style')).toContain('left:')
    expect(localStorage.getItem('learner-floating-agent-position-v1')).toContain('"x"')
  })
})
