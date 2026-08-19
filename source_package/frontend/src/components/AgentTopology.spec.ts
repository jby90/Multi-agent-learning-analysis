import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AgentActivityEvent } from '../lib/interactiveApi'
import type { TraceView } from '../types/trace'
import AgentTopology from './AgentTopology.vue'


const baseView: TraceView = {
  visibleMessages: [],
  currentState: 'S2_KNOWLEDGE',
  knowledgeDimensions: [],
  debateGroups: [],
}

function event(
  sequence: number,
  agent: AgentActivityEvent['agent'],
  status: AgentActivityEvent['status'],
  activity: string,
  label: string,
  peers: AgentActivityEvent['peers'] = [],
  details?: Record<string, unknown>,
): AgentActivityEvent {
  return {
    sequence,
    trace_id: 'interactive-topology',
    agent,
    status,
    activity,
    label,
    stage: 'S5_REVIEW',
    peers,
    timestamp: '2026-07-30T10:30:00Z',
    details,
  }
}

describe('AgentTopology', () => {
  it('renders the real Agent, control and service module topology', () => {
    const wrapper = mount(AgentTopology, { props: { view: baseView } })

    expect(wrapper.get('[data-node="knowledge"]').classes()).toContain('is-working')
    expect(wrapper.get('[data-node="orchestrator"]').classes()).toContain('is-control')
    expect(wrapper.get('[data-node="query_sandbox"]').classes()).toContain('is-service')
    expect(wrapper.get('[data-node="assessment"]').classes()).toContain('is-service')
    expect(wrapper.text()).toContain('后台 Agent 运行拓扑')
    expect(wrapper.text()).toContain('InteractiveSessionManager')
    expect(wrapper.text()).toContain('服务 / 分支')
  })

  it('lights actual event paths and exposes safe module details', async () => {
    const events = [
      event(
        4,
        'knowledge',
        'collaborating',
        'parallel_resource_generation',
        '微课、实操与分阶测验已三路并行派发',
        ['task', 'assessment'],
        {
          fan_out: 3,
          aggregation: 'pending',
          artifact_id: 'artifact-very-long-identifier-001',
          raw_prompt: 'this must never be rendered',
        },
      ),
    ]
    const wrapper = mount(AgentTopology, { props: { view: baseView, events } })

    expect(wrapper.findAll('.topology-edge-live').length).toBeGreaterThanOrEqual(3)
    expect(wrapper.get('[data-node="knowledge"]').classes()).toContain('is-active')

    await wrapper.get('[data-node="knowledge"]').trigger('click')
    expect(wrapper.get('.topology-inspector').text()).toContain('KnowledgeAgent')
    expect(wrapper.get('.topology-inspector').text()).toContain('并发分支')
    expect(wrapper.get('.topology-inspector').text()).toContain('3')
    expect(wrapper.get('.topology-inspector').text()).not.toContain('this must never be rendered')
    // 技术实现细节默认折叠，学员视角只看中文职责与状态。
    expect((wrapper.get('details.module-implementation').element as HTMLDetailsElement).open).toBe(false)
    expect(wrapper.get('.topology-inspector').text()).toContain('技术实现')
  })

  it('maps aggregation enums to learner wording instead of raw english', async () => {
    const events = [
      event(
        9,
        'review',
        'waiting',
        'parallel_quality_review',
        '四维审核已汇聚，等待确定性裁决',
        [],
        { fan_out: 4, aggregation: 'deterministic' },
      ),
    ]
    const wrapper = mount(AgentTopology, { props: { view: baseView, events } })

    await wrapper.get('[data-node="review"]').trigger('click')
    const inspector = wrapper.get('.topology-inspector').text()
    expect(inspector).toContain('确定性汇聚')
    expect(inspector).not.toContain('deterministic')
  })

  it('shows sanitized trace output instead of raw prompts or content', async () => {
    const view: TraceView = {
      ...baseView,
      visibleMessages: [{
        msgId: 'm-1', traceId: 't-1', step: 2, agent: 'knowledge', role: 'produce',
        payloadType: 'micro_lesson', content: { private_prompt: 'hidden' },
        evidence: [{ kind: 'knowledge', ref: 'KB-1' }],
        claims: [{ text: 'claim', kind: 'fact' }], timestamp: '2026-07-30T10:30:00Z',
        rejectedByBus: false, busErrors: [],
      }],
    }
    const wrapper = mount(AgentTopology, { props: { view } })

    await wrapper.get('[data-node="knowledge"]').trigger('click')
    const inspector = wrapper.get('.topology-inspector').text()
    expect(inspector).toContain('产物类型：会话内容')
    expect(inspector).toContain('绑定 1 条证据')
    expect(inspector).not.toContain('hidden')
    expect(inspector).not.toContain('private_prompt')
  })

  it('lets reviewers inspect specialist activity from the event timeline', async () => {
    const events = [
      event(7, 'review', 'collaborating', 'parallel_quality_review', '四维并行审核已派发', ['evidence_review']),
      event(8, 'evidence_review', 'working', 'specialist_quality_review', '事实与证据审核执行中', ['review']),
    ]
    const wrapper = mount(AgentTopology, { props: { view: baseView, events } })

    await wrapper.get('[data-node="evidence_review"]').trigger('click')
    expect(wrapper.get('.topology-inspector').text()).toContain('EvidenceReviewAgent')
    expect(wrapper.get('.topology-inspector').text()).toContain('事实与证据审核执行中')
    expect(wrapper.findAll('.topology-timeline li')).toHaveLength(2)
  })
})
