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
    expect(wrapper.find('.parallel-proof').exists()).toBe(true)
    expect(wrapper.findAll('.agent-packet')).toHaveLength(4)
  })

  it('shows live evidence for both parallel review branches', () => {
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
    expect(wrapper.get('.parallel-proof').classes()).toContain('is-running')
    expect(wrapper.text()).toContain('R-02')
    expect(wrapper.text()).toContain('R-03')
    expect(wrapper.text()).toContain('证据审核 Agent')
    expect(wrapper.text()).toContain('教学适配 Agent')
    const reviewGroup = wrapper.get('[data-agent="review"]')
    expect(reviewGroup.find('.review-agent-team').exists()).toBe(true)
    expect(reviewGroup.find('[data-agent="evidence_review"]').exists()).toBe(true)
    expect(reviewGroup.find('[data-agent="pedagogy_review"]').exists()).toBe(true)
    expect(reviewGroup.find('.review-agent-team').classes()).toContain('is-parallel')
    expect(wrapper.find('.proof-branch').exists()).toBe(false)
    expect(wrapper.text()).toContain('双路并行执行中')
  })

  it('shows the shared resource fork/join stage without adding another proof panel', () => {
    const dispatched = {
      ...event(
        3,
        'knowledge',
        'collaborating',
        '个性化微课与实操草稿已双路并行派发',
        ['task'],
        { fan_out: 2, aggregation: 'pending', stage_id: 'resource-generation' },
      ),
      activity: 'parallel_resource_generation',
    }
    const taskWorking = {
      ...event(
        4,
        'task',
        'working',
        '正在并行准备实操任务草稿',
        ['knowledge'],
        { fan_out: 2, aggregation: 'pending', branch_id: 'task' },
      ),
      activity: 'parallel_resource_generation',
    }
    const wrapper = mount(AgentStage, { props: { view, events: [dispatched, taskWorking] } })

    expect(wrapper.text()).toContain('2 路资源并发')
    expect(wrapper.get('[data-agent="knowledge"]').classes()).toContain('is-collaborating')
    expect(wrapper.get('[data-agent="task"]').classes()).toContain('is-working')
    expect(wrapper.text()).toContain('正在并行准备实操任务草稿')
    expect(wrapper.findAll('.parallel-proof')).toHaveLength(0)
  })

  it('keeps deterministic join evidence visible after review completes', () => {
    const completed = {
      ...event(
        6,
        'review',
        'reviewing',
        '双路审核已汇聚，正在执行确定性裁决',
        ['knowledge'],
        {
          fan_out: 2,
          aggregation: 'deterministic',
          stage_id: 'quality-review-axes',
          contract_id: 'lc-1234567890abcdefghijkl',
          artifact_id: 'msg-artifact-1',
          correlation_id: 'msg-artifact-1',
          parallel_elapsed_ms: 820,
          branches: [
            { branch_id: 'evidence_review', status: 'succeeded', required: true, elapsed_ms: 790 },
            { branch_id: 'pedagogy_review', status: 'succeeded', required: true, elapsed_ms: 610 },
          ],
        },
      ),
      activity: 'parallel_quality_review',
    }
    const arbitratingWrapper = mount(AgentStage, { props: { view, events: [completed] } })
    expect(arbitratingWrapper.get('.review-agent-team').classes()).toContain('is-arbitrating')
    expect(arbitratingWrapper.text()).toContain('结果已汇聚 · 正在仲裁')

    const finalReview = {
      ...event(7, 'review', 'approved', '质量门已通过'),
      activity: 'quality_gate',
    }
    const later = event(8, 'task', 'working', '正在准备下一阶段任务')
    const wrapper = mount(AgentStage, { props: { view, events: [completed, finalReview, later] } })

    expect(wrapper.get('.parallel-proof').classes()).toContain('is-complete')
    expect(wrapper.get('[data-agent="review"] .review-agent-team').classes()).toContain('is-complete')
    expect(wrapper.get('[data-agent="evidence_review"]').classes()).toContain('is-done')
    expect(wrapper.get('[data-agent="pedagogy_review"]').classes()).toContain('is-done')
    expect(wrapper.text()).toContain('已汇聚 · 确定性裁决')
    expect(wrapper.text()).toContain('790 ms')
    expect(wrapper.text()).toContain('610 ms')
    expect(wrapper.text()).toContain('820 ms')
    expect(wrapper.text()).toContain('≈ 580 ms')
    expect(wrapper.text()).toContain('lc-1234567…ghijkl')
  })

  it('shows only the routed specialist during targeted dispute review', () => {
    const completed = {
      ...event(10, 'review', 'reviewing', '双路审核已汇聚，正在执行确定性裁决', ['task'], {
        fan_out: 2,
        aggregation: 'deterministic',
        branches: [
          { branch_id: 'evidence_review', status: 'succeeded', required: true },
          { branch_id: 'pedagogy_review', status: 'succeeded', required: true },
        ],
      }),
      activity: 'parallel_quality_review',
    }
    const reviewDebate = {
      ...event(11, 'review', 'debating', '正在围绕证据进行有界复核', ['task'], {
        dispute_route: 'targeted_debate',
        rule_ids: ['R-02'],
        specialist_agents: ['evidence_review'],
      }),
      activity: 'bounded_debate',
    }
    const evidenceDebate = {
      ...event(12, 'evidence_review', 'debating', '正在针对 R-02 证据边界进行定向复核', ['task', 'review']),
      activity: 'targeted_dispute_review',
    }
    const taskDebate = {
      ...event(13, 'task', 'debating', '正在围绕证据进行有界复核', ['review']),
      activity: 'bounded_debate',
    }
    const wrapper = mount(AgentStage, {
      props: {
        view: { ...view, currentState: 'S6_DEBATE' },
        events: [completed, reviewDebate, evidenceDebate, taskDebate],
      },
    })

    expect(wrapper.get('.review-agent-team').classes()).toContain('is-debating')
    expect(wrapper.get('[data-agent="evidence_review"]').classes()).toContain('is-debating')
    expect(wrapper.get('[data-agent="pedagogy_review"]').classes()).toContain('is-done')
    expect(wrapper.text()).toContain('争议点定向复核中')
    expect(wrapper.text()).toContain('3 活跃')
  })

  it('shows hard-rule rejects as direct regeneration instead of debate', () => {
    const completed = {
      ...event(20, 'review', 'reviewing', '双路审核已汇聚', ['knowledge'], {
        aggregation: 'deterministic',
        branches: [
          { branch_id: 'evidence_review', status: 'succeeded', required: true },
          { branch_id: 'pedagogy_review', status: 'succeeded', required: true },
        ],
      }),
      activity: 'parallel_quality_review',
    }
    const routed = {
      ...event(21, 'review', 'blocked', '硬规则命中，已跳过模型辩论', ['knowledge'], {
        dispute_route: 'local_regeneration',
        hard_veto_rules: ['R-04'],
      }),
      activity: 'deterministic_rejection_route',
    }
    const wrapper = mount(AgentStage, { props: { view, events: [completed, routed] } })

    expect(wrapper.get('.review-agent-team').classes()).toContain('is-regenerating')
    expect(wrapper.text()).toContain('硬规则命中 · 跳过辩论')
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
