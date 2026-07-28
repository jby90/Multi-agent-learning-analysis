import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { TraceMessage } from '../types/trace'
import SqlResultTable from './SqlResultTable.vue'


function outOfScopeMessage(): TraceMessage {
  return {
    msgId: 'robustness-out-of-scope-001',
    traceId: 'robustness-single-session',
    step: 1,
    agent: 'verification',
    role: 'produce',
    payloadType: 'sql_result',
    content: {
      event: 'refuse_out_of_scope',
      question: 'H2601的能耗是多少？',
      family: 'OUT_OF_SCOPE',
      student_message: '该问题超出数据验证智能体的职责范围。',
    },
    evidence: [],
    claims: [],
    timestamp: '2026-07-21T02:00:00+00:00',
    rejectedByBus: false,
    busErrors: [],
  }
}


describe('OUT_OF_SCOPE/sql=null robustness probe', () => {
  it('renders a stable result surface without SQL controls or an exception', () => {
    const wrapper = mount(SqlResultTable, {
      props: { message: outOfScopeMessage() },
    })

    expect(wrapper.get('[aria-label="数据查询结果"]').attributes('aria-label')).toBe('数据查询结果')
    expect(wrapper.find('button').exists()).toBe(false)
    expect(wrapper.find('pre').exists()).toBe(false)
  })

  it('shows the refusal message instead of presenting an empty-query result', () => {
    const wrapper = mount(SqlResultTable, {
      props: { message: outOfScopeMessage() },
    })

    expect(wrapper.text()).toContain(
      '这个问题不在本次训练的数据范围内，请换一个与岗位任务相关的问题。',
    )
    expect(wrapper.text()).not.toContain('数据验证智能体')
    expect(wrapper.text()).not.toContain('本次查询没有返回可展示的列。')
  })
})
