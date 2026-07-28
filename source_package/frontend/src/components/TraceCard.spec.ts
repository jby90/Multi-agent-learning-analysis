import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import TraceCard from './TraceCard.vue'
import type { TraceMessage } from '../types/trace'


const message: TraceMessage = {
  msgId: 'demo-a-004',
  traceId: 'demo-a',
  step: 4,
  agent: 'knowledge',
  role: 'produce',
  payloadType: 'lecture_note',
  content: {
    event: 'product_ready',
    knowledge_point: '完成率计算',
    cached: true,
  },
  evidence: [
    { kind: 'kb_chunk', ref: 'KB-003', quote: '完成率定义。' },
    {
      kind: 'quiz_answer_key',
      ref: 'M-01:1',
      quote: '{"expected_points":["计划与实际不同"],"standard_sql":"SELECT plan_qty"}',
    },
  ],
  claims: [],
  timestamp: '2026-07-16T02:00:00+00:00',
  latencyMs: 120,
  model: 'qwen3-235b-a22b',
  tokenUsage: { promptTokens: 20, completionTokens: 5, totalTokens: 25 },
  rejectedByBus: false,
  busErrors: [],
}

function routingMessage(
  predicted: string | undefined,
  final: string | undefined,
  mismatch: boolean | undefined,
): TraceMessage {
  return {
    ...message,
    msgId: 'demo-a-routing',
    agent: 'verification',
    role: 'produce',
    payloadType: 'sql_result',
    content: {
      event: 'query_completed',
      question: '查询生产异常传播情况',
      row_count: 2,
      routing_predicted_family: predicted,
      routing_final_family: final,
      routing_family_mismatch: mismatch,
    },
  }
}


describe('TraceCard', () => {
  it('expands a user-readable review record without leaking implementation identifiers', async () => {
    const wrapper = mount(TraceCard, { props: { message } })

    expect(wrapper.get('.trace-card').classes()).toContain('agent-knowledge')
    expect(wrapper.text()).toContain(
      '依据学员盲区选取知识点，从知识库调取内容并逐句核对引用',
    )
    expect(wrapper.text()).toContain('历史记录')
    expect(wrapper.text()).not.toContain('demo-a-004')
    expect(wrapper.text()).not.toContain('lecture_note')

    await wrapper.get('button[aria-label="展开审核记录"]').trigger('click')

    expect(wrapper.text()).not.toMatch(/demo-a-004|qwen3|令牌|消息编号|KB-003/)
    expect(wrapper.text()).toContain('完成率定义')
    expect(wrapper.text()).toContain('查询依据与预期结果已核对')
    expect(wrapper.text()).not.toMatch(/expected_points|standard_sql|M-01:1/)
    expect(wrapper.get('button').attributes('aria-expanded')).toBe('true')
  })

  it('shows a teaching-language review reason without exposing an R rule identifier', async () => {
    const reviewMessage: TraceMessage = {
      ...message,
      agent: 'review',
      role: 'verdict',
      payloadType: 'review_verdict',
      verdict: {
        decision: 'reject',
        ruleHits: [{ ruleId: 'R-02', reason: '现有引文不足以直接支撑该结论，正在补充说明。' }],
      },
    }
    const wrapper = mount(TraceCard, { props: { message: reviewMessage } })

    expect(wrapper.text()).toContain('审核驳回 · 引用不足')
    expect(wrapper.text()).not.toMatch(/\bR-0[1-5]\b/u)

    await wrapper.get('button[aria-label="展开审核记录"]').trigger('click')

    expect(wrapper.text()).toContain('引用不足 · 现有引文不足以直接支撑该结论，正在补充说明。')
    expect(wrapper.text()).not.toMatch(/人工误判|注入|injected_for_demo/iu)
    expect(wrapper.text()).not.toMatch(/\bR-0[1-5]\b/u)
  })

  it.each([
    '人工误驳',
    '人工误判',
    '人工注入',
    '故障注入',
    'injected_for_demo',
    'injection_label',
  ])('fails closed when an imported review reason contains %s', async (marker) => {
    const reviewMessage: TraceMessage = {
      ...message,
      agent: 'review',
      role: 'verdict',
      payloadType: 'review_verdict',
      verdict: {
        decision: 'reject',
        ruleHits: [{
          ruleId: 'R-02',
          reason: marker,
        }],
      },
    }
    const wrapper = mount(TraceCard, { props: { message: reviewMessage } })

    await wrapper.get('button[aria-label="展开审核记录"]').trigger('click')

    expect(wrapper.text()).toContain('当前内容暂时无法展示，请稍后再试。')
    expect(wrapper.text()).not.toMatch(
      /人工误驳|人工误判|人工注入|故障注入|injected_for_demo|injection_label/iu,
    )
  })

  it('describes a consistent query judgment without exposing routing fields or family codes', () => {
    const wrapper = mount(TraceCard, {
      props: { message: routingMessage('Q2', 'Q2', false) },
    })
    const diagnosis = wrapper.get('.routing-diagnosis')

    expect(diagnosis.classes()).toContain('is-match')
    expect(diagnosis.text()).toContain('问题判断一致')
    expect(diagnosis.text()).toContain('查询范围已经核对')
    expect(wrapper.text()).not.toMatch(/\bQ2\b|routing_[a-z0-9_]+/iu)
  })

  it('describes an adjusted query judgment only in business language', async () => {
    const wrapper = mount(TraceCard, {
      props: { message: routingMessage('Q4', 'Q5', true) },
    })
    const diagnosis = wrapper.get('.routing-diagnosis')

    expect(diagnosis.classes()).toContain('is-mismatch')
    expect(diagnosis.text()).toContain('问题判断需要调整')
    expect(diagnosis.text()).toContain('已按实际数据范围继续查询')
    expect(wrapper.text()).not.toMatch(/\bQ[45]\b|routing_[a-z0-9_]+/iu)

    await wrapper.get('button[aria-label="展开审核记录"]').trigger('click')

    expect(wrapper.get('.routing-business-detail').text()).toContain(
      '系统已依据实际数据范围完成调整',
    )
    expect(wrapper.find('.routing-field-grid').exists()).toBe(false)
    expect(wrapper.text()).not.toMatch(/\bQ[45]\b|routing_[a-z0-9_]+/iu)
  })

  it('does not invent a routing diagnosis when any required field is missing', () => {
    const wrapper = mount(TraceCard, {
      props: { message: routingMessage('Q4', undefined, true) },
    })

    expect(wrapper.find('.routing-diagnosis').exists()).toBe(false)
    expect(wrapper.find('.routing-field-grid').exists()).toBe(false)
  })

  it('fails closed when expanded imported SQL contains internal markers', async () => {
    const wrapper = mount(TraceCard, {
      props: {
        message: {
          ...routingMessage('Q2', 'Q2', false),
          content: {
            ...routingMessage('Q2', 'Q2', false).content,
            generated_sql: 'SELECT safe_rejected_flag, msg_id_copy, T22_FUTURE, _S42_FUTURE FROM trace',
          },
        },
      },
    })

    await wrapper.get('button[aria-label="展开审核记录"]').trigger('click')

    expect(wrapper.get('pre').text()).toBe('查询内容已隐藏')
    expect(wrapper.text()).not.toMatch(
      /safe_rejected|msg_id|T22_FUTURE|S42_FUTURE/iu,
    )
  })
})
