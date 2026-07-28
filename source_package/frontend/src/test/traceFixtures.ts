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


export function demoTrace(
  traceId: string,
  profileId: string,
  title: string,
  lectureText: string,
  actual: string,
  options: { debate?: boolean; cached?: boolean; collision?: boolean } = {},
): string {
  const learningByProfile: Record<string, {
    blindSpots: string[]
    diagnosisDifficulty: string
    lecturePoint: string
    lectureCoverage: string[]
    chunkRef: string
  }> = {
    planner_new: {
      blindSpots: ['三道工序与传导关系', '计划量与实际量口径', '传导时滞分析', '完成率计算', '异常识别标准'],
      diagnosisDifficulty: 'basic',
      lecturePoint: '三道工序与传导关系',
      lectureCoverage: ['三道工序与传导关系'],
      chunkRef: 'KB-001',
    },
    craft_engineer: {
      blindSpots: ['完成率计算', '月度聚合方法', '跨工序归因方法', '异常识别标准'],
      diagnosisDifficulty: 'applied',
      lecturePoint: '完成率计算',
      lectureCoverage: ['完成率计算'],
      chunkRef: 'KB-003',
    },
    line_leader: {
      blindSpots: ['计划量与实际量口径', '完成率计算', '异常识别标准', '责任单元定位'],
      diagnosisDifficulty: 'basic',
      lecturePoint: '计划量与实际量口径',
      lectureCoverage: ['计划量与实际量口径', '完成率计算'],
      chunkRef: 'KB-002',
    },
  }
  const learning = learningByProfile[profileId] ?? learningByProfile.line_leader!
  const rows: Record<string, unknown>[] = [
    envelope(traceId, 1, 'system', 'system', 'control', {
      action: 'session_start', state: 'S0_INIT', student_profile_ref: profileId,
    }),
    envelope(traceId, 2, 'system', 'system', 'control', {
      action: 'profile_loaded',
      profile: {
        profile_id: profileId,
        title,
        background: `${title}的真实岗位背景`,
        strengths: ['现场经验'],
      },
      knowledge_dimensions: ['计划量与实际量口径', '完成率计算'],
    }),
    envelope(traceId, 3, 'diagnosis', 'produce', 'profile_assessment', {
      event: 'diagnosis_ready', difficulty: learning.diagnosisDifficulty,
      blind_spots: learning.blindSpots,
      pretest_score: { correct: 3, total: 5, rate: 0.6 },
    }),
    envelope(traceId, 4, 'knowledge', 'produce', 'lecture_note', {
      event: 'product_ready', knowledge_point: learning.lecturePoint,
      coverage: learning.lectureCoverage,
      lecture_md: `# 岗位微课\n\n${lectureText}`,
      ...(options.cached ? { cached: true } : {}),
    }, {
      evidence: [{
        kind: 'kb_chunk', ref: learning.chunkRef, quote: '计划量是计划完成的工作量。',
        supports_claim: lectureText,
      }],
      claims: [{ text: lectureText, kind: 'fact' }],
      model: 'qwen3-235b-a22b', latency_ms: 120,
      token_usage: { prompt_tokens: 20, completion_tokens: 5, total_tokens: 25 },
    }),
  ]
  if (options.debate) {
    rows.push(
      envelope(traceId, 5, 'review', 'verdict', 'review_verdict', {
        event: 'review_complete', reviewed_msg_id: `${traceId}-004`,
        reviewed_payload_type: 'lecture_note',
      }, { verdict: { decision: 'reject', rule_hits: [{ rule_id: 'R-02', reason: '现有引文不足以直接支撑该结论。' }] } }),
      envelope(traceId, 6, 'system', 'system', 'control', {
        action: 'state_transition', transition_id: 'T05',
        from_state: 'S5_REVIEW', to_state: 'S6_DEBATE',
      }),
      envelope(traceId, 7, 'knowledge', 'rebuttal', 'rebuttal_case', {
        event: 'rebuttal_ready', rebuttal: '引用可直接支撑该结论。',
      }),
      envelope(traceId, 8, 'review', 're_verdict', 'review_verdict', {
        event: 'review_complete', reviewed_msg_id: `${traceId}-004`,
        reviewed_payload_type: 'lecture_note',
      }, { verdict: { decision: 'approve', rule_hits: [] } }),
    )
  } else {
    rows.push(
      envelope(traceId, 5, 'review', 'verdict', 'review_verdict', {
        event: 'review_complete', reviewed_msg_id: `${traceId}-004`,
        reviewed_payload_type: 'lecture_note',
      }, { verdict: { decision: 'approve', rule_hits: [] } }),
    )
  }
  rows.push(
    envelope(traceId, rows.length + 1, 'task', 'produce', 'quiz_set', {
      event: 'product_ready', knowledge_point: '计划量与实际量口径',
      difficulty: 'basic', question: '查询计划量与实际完成量',
    }),
  )
  const offset = rows.length + 1
  rows.push(
    envelope(traceId, offset, 'verification', 'produce', 'sql_result', {
      event: 'query_completed', question: '查询计划量与实际完成量',
      columns: ['plan_qty', 'actual_qty'],
      rows: [{ plan_qty: '1855.06', actual_qty: actual }], row_count: 1,
      generated_sql: 'SELECT SUM(plan_qty) AS plan_qty, SUM(actual_qty) AS actual_qty FROM fact_production_progress',
    }),
  )
  let pathOffset = offset + 1
  if (options.collision) {
    rows.push(
      envelope(traceId, offset + 1, 'system', 'system', 'control', {
        event: 'probe_outcome', answer_result: 'correct', target_misconception: 'M-01',
      }),
      envelope(traceId, offset + 2, 'system', 'system', 'control', {
        action: 'state_transition', transition_id: 'T16',
        from_state: 'S8_PROBE', to_state: 'S9_PATH_UPDATE',
      }),
    )
    pathOffset = offset + 3
  }
  rows.push(
    envelope(traceId, pathOffset, 'system', 'system', 'learning_path_update', {
      event: 'path_updated', has_next: false, learning_goal_achieved: true,
      completed_nodes: ['岗前测评', '岗位微课', '数据实操', '反证追问'],
      current_node: '培养目标达成', difficulty_action: 'step_up',
      summary: `${title}完成培养。`,
    }),
    envelope(traceId, pathOffset + 1, 'system', 'system', 'control', {
      action: 'state_transition', transition_id: 'T20',
      from_state: 'S9_PATH_UPDATE', to_state: 'S10_DONE',
    }),
  )
  return rows.map((row) => JSON.stringify(row)).join('\n')
}
