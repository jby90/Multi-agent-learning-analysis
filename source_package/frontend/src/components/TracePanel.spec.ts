import { readFileSync } from 'node:fs'
import path from 'node:path'

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { AgentId, RoleId, TraceMessage, TraceView } from '../types/trace'
import TracePanel from './TracePanel.vue'


const collaborationStyles = readFileSync(
  path.resolve(process.cwd(), 'src', 'styles.css'),
  'utf8',
)

function cssRule(selector: string): string {
  const start = collaborationStyles.indexOf(`${selector} {`)
  if (start < 0) return ''
  const bodyStart = collaborationStyles.indexOf('{', start) + 1
  return collaborationStyles.slice(bodyStart, collaborationStyles.indexOf('}', bodyStart))
}

function cssNumber(declarations: string, property: string): number {
  const value = declarations.match(new RegExp(`${property}:\\s*([0-9.]+)`))?.[1]
  return Number(value ?? Number.NaN)
}

function cssFunctionNumber(declarations: string, functionName: string): number {
  const value = declarations.match(new RegExp(`${functionName}\\(([0-9.]+)\\)`))?.[1]
  return Number(value ?? Number.NaN)
}

const view: TraceView = {
  visibleMessages: [],
  currentState: 'S0_INIT',
  knowledgeDimensions: [],
  debateGroups: [],
}

function message(
  step: number,
  agent: AgentId,
  role: RoleId,
  overrides: Partial<TraceMessage> = {},
): TraceMessage {
  return {
    msgId: `trace-${step}`,
    traceId: 'trace',
    step,
    agent,
    role,
    payloadType: role === 'system' ? 'control' : 'review_verdict',
    content: { event: `event-${step}` },
    evidence: [],
    claims: [],
    timestamp: `2026-07-17T02:00:${String(step).padStart(2, '0')}+00:00`,
    rejectedByBus: false,
    busErrors: [],
    ...overrides,
  }
}

const systemStart = message(1, 'system', 'system', {
  content: { action: 'session_start', state: 'S0_INIT' },
})
const diagnosis = message(2, 'diagnosis', 'produce', { payloadType: 'profile_assessment' })
const knowledge = message(3, 'knowledge', 'produce', { payloadType: 'lecture_note' })
const rejection = message(4, 'review', 'verdict', {
  verdict: { decision: 'reject', ruleHits: [{ ruleId: 'R-02', reason: '引用不足' }] },
})
const systemInsideRange = message(5, 'system', 'system', {
  content: { action: 'state_transition', from_state: 'S5_REVIEW', to_state: 'S6_DEBATE' },
})
const rebuttal = message(6, 'knowledge', 'rebuttal', { payloadType: 'rebuttal_case' })
const reVerdict = message(7, 'review', 're_verdict', {
  verdict: { decision: 'approve', ruleHits: [] },
})
const task = message(8, 'task', 'produce', { payloadType: 'quiz_set' })
const verification = message(9, 'verification', 'produce', { payloadType: 'sql_result' })
const systemDone = message(10, 'system', 'system', {
  content: { action: 'state_transition', from_state: 'S9_PATH_UPDATE', to_state: 'S10_DONE' },
})

const collaborationView: TraceView = {
  visibleMessages: [
    systemStart,
    diagnosis,
    knowledge,
    rejection,
    systemInsideRange,
    rebuttal,
    reVerdict,
    task,
    verification,
    systemDone,
  ],
  currentState: 'S10_DONE',
  knowledgeDimensions: [],
  debateGroups: [{ id: rejection.msgId, messages: [rejection, rebuttal, reVerdict] }],
}


describe('TracePanel', () => {
  it('uses the approved collaboration-view title', () => {
    const wrapper = mount(TracePanel, { props: { view } })

    expect(wrapper.get('.trace-heading h2').text()).toBe('协作记录')
    expect(wrapper.find('.protocol-banner').exists()).toBe(false)
  })

  it('derives five roles, debate summary, and collapsed system records from the trace', () => {
    const wrapper = mount(TracePanel, { props: { view: collaborationView } })

    const roleItems = wrapper.findAll('.agent-collaboration-map li')
    expect(roleItems).toHaveLength(5)
    expect(roleItems.map((item) => item.text())).toEqual([
      '学情诊断识别知识盲区已参与',
      '领域知识匹配并组织讲义已参与',
      '实操任务生成岗位练习已参与',
      '数据验证用查询结果核对结论已参与',
      '专业审核按规则驳回或放行已参与',
    ])
    expect(wrapper.get('.debate-summary').text()).toContain('发生过驳回复审 · 1次')
    expect(wrapper.get('details.system-records').attributes('open')).toBeUndefined()
    expect(wrapper.get('details.system-records summary').text()).toContain('流程记录 · 3条')
    expect(wrapper.findAll('details.system-records .trace-card')).toHaveLength(3)
    expect(wrapper.find('.agent-collaboration-map [aria-current="step"]').exists()).toBe(false)
  })

  it('keeps five visibly different teacher agents on the collaboration console', () => {
    const wrapper = mount(TracePanel, { props: { view } })
    const agents = ['diagnosis', 'knowledge', 'task', 'verification', 'review']
    const roleItems = wrapper.findAll('.agent-collaboration-map li')
    const teachers = wrapper.findAll('[data-teacher-agent]')

    expect(roleItems).toHaveLength(5)
    expect(roleItems.map((item) => item.attributes('data-agent'))).toEqual(agents)
    expect(teachers.map((teacher) => teacher.attributes('data-teacher-agent'))).toEqual(agents)
    expect(new Set(teachers.map((teacher) => teacher.attributes('src'))).size).toBe(5)
    expect(teachers.every((teacher) => Boolean(teacher.attributes('data-teacher-state')))).toBe(true)
  })

  it('keeps inactive robots legible while reserving the glow for the active role', () => {
    const rosterRule = cssRule('.agent-collaboration-map li')
    const pendingRobotRule = cssRule('.is-pending .agent-robot-icon')
    const seenRobotRule = cssRule('.is-seen .agent-robot-icon')
    const activeRobotRule = cssRule('.is-active .agent-robot-icon')

    expect(cssNumber(rosterRule, 'opacity')).toBeGreaterThanOrEqual(0.8)
    expect(cssNumber(pendingRobotRule, 'opacity')).toBeGreaterThanOrEqual(0.8)
    expect(cssFunctionNumber(pendingRobotRule, 'saturate')).toBeGreaterThanOrEqual(0.7)
    expect(cssNumber(seenRobotRule, 'opacity')).toBeGreaterThanOrEqual(0.9)
    expect(cssFunctionNumber(seenRobotRule, 'saturate')).toBeGreaterThanOrEqual(0.85)
    expect(activeRobotRule).toContain('drop-shadow')
  })

  it('pre-activates the next role after a state transition', () => {
    const transitionToKnowledge = message(3, 'system', 'system', {
      content: {
        action: 'state_transition',
        from_state: 'S1_DIAGNOSIS',
        to_state: 'S2_KNOWLEDGE',
      },
    })
    const wrapper = mount(TracePanel, {
      props: {
        view: {
          ...view,
          visibleMessages: [systemStart, diagnosis, transitionToKnowledge],
          currentState: 'S2_KNOWLEDGE',
        },
      },
    })

    expect(wrapper.findAll('.agent-collaboration-map [aria-current="step"]')).toHaveLength(1)
    expect(wrapper.get('[data-agent="knowledge"]').classes()).toContain('is-active')
    expect(wrapper.get('[data-agent="knowledge"]').text()).toContain('当前执行')
    expect(wrapper.get('[data-agent="diagnosis"]').classes()).toContain('is-seen')
    expect(wrapper.get('[data-agent="task"]').classes()).toContain('is-pending')
  })

  it('moves the active light between actual speakers during debate', async () => {
    const debateView: TraceView = {
      ...view,
      visibleMessages: [systemStart, diagnosis, rejection, systemInsideRange, rebuttal],
      currentState: 'S6_DEBATE',
    }
    const wrapper = mount(TracePanel, { props: { view: debateView } })

    expect(wrapper.get('[data-agent="knowledge"]').classes()).toContain('is-active')

    await wrapper.setProps({
      view: {
        ...debateView,
        visibleMessages: [...debateView.visibleMessages, reVerdict],
      },
    })

    expect(wrapper.get('[data-agent="review"]').classes()).toContain('is-active')
    expect(wrapper.get('[data-agent="knowledge"]').classes()).not.toContain('is-active')
  })

  it('keeps system transitions outside the exact debate group', () => {
    const wrapper = mount(TracePanel, { props: { view: collaborationView } })
    const debate = wrapper.get('[data-testid="debate-group"]')

    expect(debate.findAll('.trace-card')).toHaveLength(3)
    expect(debate.find('.agent-system').exists()).toBe(false)
    expect(debate.text()).not.toContain('审核把关 → 辩论复审')
  })

  it('does not claim a debate when the trace view has no complete debate group', () => {
    const wrapper = mount(TracePanel, {
      props: { view: { ...collaborationView, debateGroups: [] } },
    })

    expect(wrapper.find('.debate-summary').exists()).toBe(false)
  })

  it('keeps unseen agents in standby instead of hiding roles from a partial trace', () => {
    const wrapper = mount(TracePanel, {
      props: {
        view: {
          ...view,
          visibleMessages: [systemStart, diagnosis, rejection],
        },
      },
    })

    expect(wrapper.findAll('.agent-collaboration-map li')).toHaveLength(5)
    expect(wrapper.get('[data-agent="diagnosis"]').classes()).toContain('is-seen')
    expect(wrapper.get('[data-agent="review"]').classes()).toContain('is-active')
    expect(wrapper.get('[data-agent="verification"]').classes()).toContain('is-pending')
  })
})
