import { describe, expect, it } from 'vitest'

import {
  agentLabel,
  agentPurpose,
  abbreviateDimension,
  collaborationText,
  dataFieldLabel,
  messageSummary,
  isLearnerSafeText,
  misconceptionLabel,
  learnerTextOr,
  learnerText,
  publicQueryText,
  firstLectureGoal,
  roleLabel,
  ruleLabel,
  sandboxLabel,
  stateLabel,
  truthBadges,
} from './tracePresentation'
import type { TraceMessage } from '../types/trace'


function traceMessage(overrides: Partial<TraceMessage> = {}): TraceMessage {
  return {
    msgId: 'demo-a-007',
    traceId: 'demo-a',
    step: 7,
    agent: 'knowledge',
    role: 'produce',
    payloadType: 'lecture_note',
    content: {
      event: 'product_ready',
      knowledge_point: '完成率计算',
      lecture_md: '完成率用于观察执行情况。',
    },
    evidence: [],
    claims: [],
    timestamp: '2026-07-16T02:00:00+00:00',
    rejectedByBus: false,
    busErrors: [],
    ...overrides,
  }
}

describe('trace presentation mappings', () => {
  it('uses the first complete 本节目标 bullet instead of cutting markdown by length', () => {
    const lecture = [
      '### 完成率计算',
      '',
      '#### 本节目标',
      '- 理解完成率的定义与计算公式',
      '- 能够解释计划量与实际完成量的差异',
      '',
      '#### 核心概念',
      '这是一段很长的正文，不应被当作摘要。',
    ].join('\n')

    expect(firstLectureGoal(lecture)).toBe('理解完成率的定义与计算公式')
    expect(firstLectureGoal('### 标题\n\n正文')).toBeUndefined()
  })

  it('shortens radar labels without losing the full dimension text', () => {
    expect(abbreviateDimension('计划量与实际量口径')).toBe('计划/实际口径')
    expect(abbreviateDimension('工序间传导时滞')).toBe('工序传导')
    expect(abbreviateDimension('完成率计算')).toBe('完成率')
    expect(abbreviateDimension('异常衰减识别')).toBe('异常衰减')
  })

  it('translates every agent and role', () => {
    expect([
      'diagnosis', 'knowledge', 'task', 'verification', 'review', 'system',
    ].map(agentLabel)).toEqual([
      '学情诊断', '领域知识', '实操任务', '数据验证', '专业审核', '流程调度',
    ])
    expect([
      'produce', 'verdict', 'rebuttal', 're_verdict', 'probe', 'system',
    ].map(roleLabel)).toEqual([
      '生成内容', '初次审核', '补充说明', '再次审核', '验证任务', '流程调度',
    ])
  })

  it('translates all states, rules, and misconceptions', () => {
    expect(stateLabel('S0_INIT')).toBe('会话建立')
    expect(stateLabel('S10_DONE')).toBe('培养目标达成')
    expect(stateLabel('S_FAIL')).toBe('安全终止')
    expect(['R-01', 'R-02', 'R-03', 'R-04', 'R-05', 'R-06'].map(ruleLabel)).toEqual([
      '口径混淆',
      '引用不足',
      '难度错配',
      '结论未申报',
      '查询证据异常',
      '表达可读性问题',
    ])
    expect([
      'M-01', 'M-02', 'M-03', 'M-04', 'M-05',
    ].map(misconceptionLabel)).toEqual([
      '计划量与实际量的区分',
      '工序传导时滞的判断',
      '单月波动与长期趋势的区分',
      '汇总口径的区分',
      '异常衰减的判断',
    ])
  })

  it('translates every sandbox rule into a learner-facing teaching label', () => {
    expect([
      'S-01', 'S-02', 'S-03', 'S-04', 'S-05', 'S-06', 'S-07', 'S-08',
    ].map(sandboxLabel)).toEqual([
      '只能做数据查询',
      '一次只能查一条',
      '只能查授权的数据表',
      '只能查授权的字段',
      '含不允许的危险操作',
      '含不允许的危险操作',
      '查询语句为空或写法有误',
      '超出安全查询范围',
    ])
    expect(sandboxLabel(undefined)).toBeUndefined()
    expect(sandboxLabel('S-99')).toBeUndefined()
  })

  it('fails closed for inherited object keys in public lookup tables', () => {
    for (const inheritedKey of ['__proto__', 'constructor', 'toString']) {
      expect(dataFieldLabel(inheritedKey)).toBe('数据字段')
      expect(sandboxLabel(inheritedKey)).toBeUndefined()
    }
  })

  it('translates dynamic learner copy instead of trusting trace wording', () => {
    expect(learnerText(
      'M-01反证查询由系统编排触发，完成数据撞脸后进入反证追问。',
    )).toBe(
      '计划量与实际量的区分验证查询由流程调度触发，完成数据验证后进入数据验证。',
    )
  })

  it.each([
    'T01',
    'T21',
    'T22',
    'T999',
    'T0',
    'T1',
    'T00',
    'T-03',
    'T-FS02',
    'T-08-DECAY-A',
    't-03',
    't22',
    'S0_INIT',
    'S4_VERIFY',
    'S10_DONE',
    'S11',
    'S42_FUTURE',
    's42',
    'no_matching_transition',
    'msg_id',
    'rule_hits',
    'rebuttal',
    'verdict',
    'safe_rejected',
    'external_unavailable',
    'system_error',
    'outcome=completed',
    'routing_predicted_family',
    'family',
    'misconception',
    'difficulty',
    'route',
    'state',
    'diagnostic',
    'Q4',
    'q7',
    '数据验证智能体',
    '审核Agent驳回R-04',
    '状态机发生转移',
    '意图路由偏差',
  ])('fails closed when dynamic public copy contains engineering token %s', (token) => {
    const source = `教学提示：${token}`

    expect(learnerText(source)).toBe('当前内容暂时无法展示，请稍后再试。')
    expect(collaborationText(source)).toBe('当前内容暂时无法展示，请稍后再试。')
  })

  it('does not confuse sandbox teaching rules with state-machine codes', () => {
    expect(learnerText('S-01 只允许查询数据')).toBe('只能做数据查询 只允许查询数据')
    expect(collaborationText('S-04 只允许查询授权字段')).toBe(
      '只能查授权的字段 只允许查询授权字段',
    )
  })
  it.each([
    'm-01',
    'r-99',
    'traceId',
    'sessionId',
    'ruleHits',
    'templateId',
    'HTTP 409',
    '/api/sessions',
    'orchestrator.interactive_session',
    '审核智能体',
    'm\u200b-01',
  ])('normalizes and blocks learner-visible protocol text %s', (token) => {
    const rendered = learnerText(`教学提示：${token}`)

    expect(rendered).toBe('当前内容暂时无法展示，请稍后再试。')
    expect(isLearnerSafeText(rendered)).toBe(true)
  })

  it('guards a caller-provided fallback before it reaches the learner', () => {
    expect(learnerTextOr('S4_VERIFY', 'HTTP 500 traceId'))
      .toBe('当前内容暂时无法展示，请稍后再试。')
  })

  it('keeps normal teaching SQL with lowercase aliases visible', () => {
    const query = [
      'SELECT t1.plan_qty AS q7_total',
      'FROM fact_production_progress AS t1',
      'JOIN fact_production_progress AS s42 ON s42.ship_no = t1.ship_no',
    ].join(' ')

    expect(publicQueryText(query)).toBe(query)
  })

  it('keeps domain snake-case evidence visible through approved Chinese labels', () => {
    expect(learnerText(
      '从 fact_production_progress 读取 risk_level、delay_days 与 shortfall_intensity。',
    )).toBe('从 生产进度表 读取 风险等级、延迟天数 与 缺口强度。')
  })

  it('still blocks explicit internal protocol fields after narrowing snake-case checks', () => {
    expect(learnerText('内部记录 trace_id 与 transition_id'))
      .toBe('当前内容暂时无法展示，请稍后再试。')
  })

  it.each([
    'T0',
    'T1',
    'T00',
    'T22_FUTURE',
    'S0',
    'S42_FUTURE',
    'Q7',
  ])('hides uppercase internal code %s in displayed SQL', (code) => {
    expect(publicQueryText(`SELECT '${code}'`)).toBe('查询内容已隐藏')
  })

  it('describes only the five trace agents that have learner-facing purposes', () => {
    expect(agentPurpose('diagnosis')).toBe('识别知识盲区')
    expect(agentPurpose('knowledge')).toBe('匹配并组织讲义')
    expect(agentPurpose('task')).toBe('生成岗位练习')
    expect(agentPurpose('verification')).toBe('用查询结果核对结论')
    expect(agentPurpose('review')).toBe('按规则驳回或放行')
    expect(agentPurpose('system')).toBe('')
    expect(agentPurpose('unknown')).toBe('')
  })

  it.each([
    ['raw未明确，以现场口径为准', '以现场实际口径为准'],
    ['raw 未明确', '现有资料未明确'],
    ['raw数据', '现有资料'],
    ['raw 数据', '现有资料'],
    ['raw', '现有资料'],
    ['chunk / chunks', '资料片段 / 资料片段'],
    ['SEC-001 与 KB-003', '资料来源 与 资料来源'],
    ['R-01 / R-02 / R-03 / R-04 / R-05 / R-06', '口径混淆 / 引用不足 / 难度错配 / 结论未申报 / 查询证据异常 / 表达可读性问题'],
    ['审核驳回 · R-02引用不足', '审核驳回 · 引用不足'],
    ['审核驳回 · R-02 引用不足', '审核驳回 · 引用不足'],
    [
      'S-01 / S-02 / S-03 / S-04 / S-05 / S-06 / S-07 / S-08',
      '只能做数据查询 / 一次只能查一条 / 只能查授权的数据表 / 只能查授权的字段 / 含不允许的危险操作 / 含不允许的危险操作 / 查询语句为空或写法有误 / 超出安全查询范围',
    ],
    ['S-01只能做数据查询', '只能做数据查询'],
  ])('turns source terminology into learner copy: %s', (input, expected) => {
    expect(learnerText(input)).toBe(expected)
    expect(collaborationText(input)).toBe(expected)
  })

  it('keeps review reasons while removing source and rule identifiers', () => {
    expect(collaborationText('SEC-001 支撑 R-02')).toBe('资料来源 支撑 引用不足')
  })

  it('keeps internal field names out of the default summary', () => {
    const summary = messageSummary(traceMessage())

    expect(summary).toBe(
      '依据学员盲区选取知识点，从知识库调取内容并逐句核对引用',
    )
    expect(summary).not.toMatch(
      /payload\.type|msg_id|sentence_ref|lecture_note|knowledge|product_ready/,
    )
  })

  it.each([
    [
      'refuse_out_of_scope',
      '这个问题不在本次训练的数据范围内，请换一个与岗位任务相关的问题。',
    ],
    [
      'sandbox_rejected',
      '本题的查询未通过数据安全检查，请调整后重试。',
    ],
    [
      'template_authority_rejected',
      '本题的查询未通过数据安全检查，请调整后重试。',
    ],
    [
      'query_empty',
      '本次查询没有返回数据，请调整查询条件后重试。',
    ],
    [
      'query_timeout',
      '服务暂时不可用，请稍后再试。',
    ],
    [
      'query_failed',
      '服务暂时不可用，请稍后再试。',
    ],
  ])('summarizes %s without presenting a zero-row success', (event, expected) => {
    const summary = messageSummary(traceMessage({
      agent: 'verification',
      payloadType: 'sql_result',
      content: {
        event,
        question: '运行岗位查询',
        row_count: 0,
        rule_id: 'S-04',
        student_message: 'no_matching_transition at S4_VERIFY; msg_id=secret',
      },
    }))

    expect(summary).toBe(expected)
    expect(summary).not.toContain('返回0行结果')
    expect(summary).not.toMatch(/no_matching_transition|S4_VERIFY|msg_id/iu)
  })

  it('cleans mechanism wording from dynamic collaboration summaries', () => {
    const summary = messageSummary(traceMessage({
      payloadType: 'quiz_set',
      content: { question: '请运行反证查询，完成数据撞脸。' },
    }))

    expect(summary).toBe('请运行验证查询，完成数据验证。')
  })

  it('keeps the collaboration path card terse instead of repeating the event log', () => {
    const summary = messageSummary(traceMessage({
      payloadType: 'learning_path_update',
      content: {
        summary: '新入职生产计划员完成岗前测评3/5与岗位微课；验证查询显示计划量1855.06、实际完成量1156.87，进入下一阶段培养。',
      },
    }))

    expect(summary).toBe('培养路径已更新')
    expect(summary).not.toContain('完成岗前测评')
  })

  it('translates difficulty fields inside collaboration review reasons', () => {
    expect(collaborationText(
      '画像特征 vs 产物难度特征：产物难度为basic，而学员起始难度为applied，存在难度差距（difficulty_gap=1）。',
    )).toBe(
      '画像特征与产物难度特征：产物难度为基础档，而学员起始难度为应用档，存在难度差距（难度相差一级）。',
    )
  })

  it('derives honest cached and template fallback badges', () => {
    expect(truthBadges(traceMessage({ content: { cached: true } }))).toEqual([
      { label: '历史记录', tone: 'cached' },
    ])
    expect(truthBadges(traceMessage({
      content: { generated_by: 'template_fallback' },
    }))).toEqual([
      { label: '预备内容', tone: 'fallback' },
    ])
    expect(truthBadges(traceMessage({
      content: { cached: true, generated_by: 'template_fallback' },
    }))).toHaveLength(2)
  })
})
