import { describe, expect, it } from 'vitest'

import { parseTraceJsonl } from './traceParser'
import { buildTraceView, listKeyframes } from './traceModel'


function envelope(
  traceId: string,
  step: number,
  agent: string,
  role: string,
  payloadType: string,
  content: Record<string, unknown>,
  extra: Record<string, unknown> = {},
): Record<string, unknown> {
  return {
    msg_id: `${traceId}-${String(step).padStart(3, '0')}`,
    trace_id: traceId,
    step,
    agent,
    role,
    payload: { type: payloadType, content },
    evidence: [],
    claims: [],
    timestamp: '2026-07-16T02:00:00+00:00',
    ...extra,
  }
}

function source(
  traceId: string,
  title: string,
  lecture: string,
  actual: string,
  finalState: string,
): string {
  const rows = [
    envelope(traceId, 1, 'system', 'system', 'control', {
      action: 'session_start', state: 'S0_INIT', student_profile_ref: 'planner_new',
    }),
    envelope(traceId, 2, 'system', 'system', 'control', {
      action: 'profile_loaded',
      profile: { profile_id: 'planner_new', title },
      knowledge_dimensions: ['计划量与实际量口径'],
    }),
    envelope(traceId, 3, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T01',
      from_state: 'S0_INIT', to_state: 'S1_DIAGNOSIS',
    }),
    envelope(traceId, 4, 'knowledge', 'produce', 'lecture_note', {
      event: 'product_ready', knowledge_point: '计划量与实际量口径', lecture_md: lecture,
    }),
    envelope(traceId, 5, 'verification', 'produce', 'sql_result', {
      event: 'query_completed', question: '查询实际完成量',
      rows: [{ plan_qty: '1855.06', actual_qty: actual }], row_count: 1,
    }),
    envelope(traceId, 6, 'system', 'system', 'learning_path_update', {
      event: 'path_updated', has_next: false, learning_goal_achieved: true,
      completed_nodes: ['岗前测评', '岗位微课', title], current_node: title,
      summary: `${title}完成培养。`,
    }),
    envelope(traceId, 7, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T20',
      from_state: 'S9_PATH_UPDATE', to_state: finalState,
    }),
  ]
  return rows.map((row) => JSON.stringify(row)).join('\n')
}

function debateSource(): string {
  const traceId = 'demo-debate'
  const rows = [
    envelope(traceId, 1, 'system', 'system', 'control', {
      action: 'session_start', state: 'S0_INIT', student_profile_ref: 'planner_new',
    }),
    envelope(traceId, 2, 'review', 'verdict', 'review_verdict', {
      event: 'review_complete', reviewed_msg_id: 'demo-debate-product',
      reviewed_payload_type: 'lecture_note',
    }, { verdict: { decision: 'reject', rule_hits: [{ rule_id: 'R-02', reason: '误驳' }] } }),
    envelope(traceId, 3, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T05',
      from_state: 'S5_REVIEW', to_state: 'S6_DEBATE',
    }),
    envelope(traceId, 4, 'knowledge', 'rebuttal', 'rebuttal_case', {
      event: 'rebuttal_ready', rebuttal: '证据可直接支撑。',
    }),
    envelope(traceId, 5, 'review', 're_verdict', 'review_verdict', {
      event: 'review_complete', reviewed_msg_id: 'demo-debate-product',
      reviewed_payload_type: 'lecture_note',
    }, { verdict: { decision: 'approve', rule_hits: [] } }),
    envelope(traceId, 6, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T06',
      from_state: 'S6_DEBATE', to_state: 'S3_TASK',
    }),
    envelope(traceId, 7, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T15',
      from_state: 'S7_STUDENT', to_state: 'S8_PROBE',
    }),
    envelope(traceId, 8, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T17',
      from_state: 'S8_PROBE', to_state: 'S2_KNOWLEDGE', difficulty_action: 'step_down',
    }),
  ]
  return rows.map((row) => JSON.stringify(row)).join('\n')
}

function collisionSource(): string {
  const traceId = 'demo-collision'
  return [
    envelope(traceId, 1, 'system', 'system', 'control', {
      action: 'session_start', state: 'S0_INIT', student_profile_ref: 'planner_new',
    }),
    envelope(traceId, 2, 'verification', 'produce', 'sql_result', {
      event: 'query_completed',
      rows: [{ plan_qty: '1855.06', actual_qty: '1156.87' }],
      columns: ['plan_qty', 'actual_qty'],
    }),
    envelope(traceId, 3, 'system', 'system', 'control', {
      event: 'probe_outcome', answer_result: 'correct', target_misconception: 'M-01',
    }),
    envelope(traceId, 4, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T16',
      from_state: 'S8_PROBE', to_state: 'S9_PATH_UPDATE',
    }),
    envelope(traceId, 5, 'system', 'system', 'learning_path_update', {
      event: 'path_updated', summary: '完成培养',
    }),
  ].map((row) => JSON.stringify(row)).join('\n')
}

describe('trace view model', () => {
  it('changes every visible data area when the trace is replaced', () => {
    const first = buildTraceView(
      parseTraceJsonl(source('demo-a', '计划员甲', '甲的岗位微课', '1156.87', 'S10_DONE'), 'a.jsonl'),
      7,
    )
    const second = buildTraceView(
      parseTraceJsonl(source('demo-b', '班组长乙', '乙的岗位微课', '998.10', 'S_FAIL'), 'b.jsonl'),
      7,
    )

    expect(first.profile?.title).toBe('计划员甲')
    expect(second.profile?.title).toBe('班组长乙')
    expect(first.lecture?.content.lecture_md).toBe('甲的岗位微课')
    expect(second.lecture?.content.lecture_md).toBe('乙的岗位微课')
    expect(first.sqlResult?.content.rows).toEqual([
      { plan_qty: '1855.06', actual_qty: '1156.87' },
    ])
    expect(second.sqlResult?.content.rows).toEqual([
      { plan_qty: '1855.06', actual_qty: '998.10' },
    ])
    expect(first.currentState).toBe('S10_DONE')
    expect(second.currentState).toBe('S_FAIL')
    expect(first.path?.content.current_node).toBe('计划员甲')
    expect(second.path?.content.current_node).toBe('班组长乙')
  })

  it('uses the replay cursor to derive the current state and resources', () => {
    const trace = parseTraceJsonl(
      source('demo-a', '计划员甲', '岗位微课', '1156.87', 'S10_DONE'),
      'a.jsonl',
    )

    const early = buildTraceView(trace, 3)
    const late = buildTraceView(trace, 7)

    expect(early.currentState).toBe('S1_DIAGNOSIS')
    expect(early.lecture).toBeUndefined()
    expect(late.currentState).toBe('S10_DONE')
    expect(late.lecture).toBeDefined()
    expect(late.visibleMessages).toHaveLength(7)
  })

  it('groups the debate and creates only evidence-backed keyframes', () => {
    const trace = parseTraceJsonl(debateSource(), 'debate.jsonl')
    const view = buildTraceView(trace, trace.messages.length)

    expect(view.debateGroups).toHaveLength(1)
    expect(view.debateGroups[0]?.messages.map((item) => item.role)).toEqual([
      'verdict', 'rebuttal', 're_verdict',
    ])
    expect(listKeyframes(trace)).toEqual([
      { kind: 'debate', label: '复审开始', step: 2 },
      { kind: 'probe', label: '验证任务开始', step: 7 },
      { kind: 'step_down', label: '补充讲解', step: 8 },
    ])
  })

  it('derives the data-collision frame from the verified trace instead of hardcoded values', () => {
    const trace = parseTraceJsonl(collisionSource(), 'collision.jsonl')
    const collisionFrame = buildTraceView(trace, 4)
    const afterFrame = buildTraceView(trace, 5)

    expect(listKeyframes(trace)).toContainEqual({
      kind: 'collision', label: '数据验证', step: 4,
    })
    expect(collisionFrame.dataCollision).toEqual({
      misconception: 'M-01',
      wrongLabel: '计划量',
      wrongValue: '1855.06',
      correctLabel: '实际完成量',
      correctValue: '1156.87',
      step: 4,
    })
    expect(afterFrame.dataCollision).toBeUndefined()
  })
})
