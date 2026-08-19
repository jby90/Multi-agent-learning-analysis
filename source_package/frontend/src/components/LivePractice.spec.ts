import { flushPromises, mount, type VueWrapper } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  InteractiveApi,
  InteractiveDiagnosticProbe,
  InteractivePretestQuestion,
  InteractiveState,
} from '../lib/interactiveApi'
import { InteractiveApiError } from '../lib/interactiveApi'
import { isLearnerSafeText } from '../lib/tracePresentation'
import type { TraceMessage } from '../types/trace'
import LivePractice from './LivePractice.vue'


function sessionState(overrides: Partial<InteractiveState> = {}): InteractiveState {
  return {
    session_id: 'session-live',
    trace_id: 'interactive-session-live',
    trace_path: 'traces/interactive-session-live.jsonl',
    state: 'S1_DIAGNOSIS',
    awaiting: 'pretest',
    mode: 'live',
    profile: {
      profile_id: 'planner_new',
      title: '新入职生产计划员',
    },
    messages: [],
    artifact: null,
    interaction: null,
    ...overrides,
  }
}

const questions: InteractivePretestQuestion[] = Array.from({ length: 5 }, (_, index) => ({
  question_id: `PT-${index + 1}`,
  knowledge_point: `知识点${index + 1}`,
  stem: `第${index + 1}题应选择哪个岗位口径？`,
  options: { A: '选项A', B: '选项B', C: '选项C', D: '选项D' },
}))

function fakeApi(): InteractiveApi {
  return {
    createSession: vi.fn(async () => sessionState()),
    getState: vi.fn(async () => sessionState()),
    getPretest: vi.fn(async () => questions),
    getDiagnosticProbes: vi.fn(async () => []),
    submitPretest: vi.fn(async () => sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    })),
    submitDiagnosticProbes: vi.fn(async () => sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    })),
    advance: vi.fn(async () => sessionState()),
    continueLearning: vi.fn(async () => sessionState({
      session_id: 'session-next',
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
      interaction: {
        kind: 'learning_notice',
        message: '已沿用本轮画像与测评结果，下一知识点：完成率计算。',
      },
    })),
    submitSql: vi.fn(async () => sessionState()),
    submitFollowUp: vi.fn(async () => sessionState()),
    getLearningRecords: vi.fn(async () => ({ guest: true, records: [] })),
    getLearningSummary: vi.fn(async () => ({ profiles: [] })),
  }
}

function sqlResultMessage(): TraceMessage {
  return {
    msgId: 'sql-result-report',
    traceId: 'interactive-session-live',
    step: 8,
    agent: 'verification',
    role: 'produce',
    payloadType: 'sql_result',
    content: {
      question: '按工序查询完成率',
      columns: ['process_code', 'complete_rate'],
      rows: [{ process_code: 'YCL', complete_rate: '0.6236' }],
    },
    evidence: [],
    claims: [],
    timestamp: '2026-07-30T00:00:00Z',
    rejectedByBus: false,
    busErrors: [],
  }
}



async function enterProfileSelectPage(wrapper: VueWrapper): Promise<void> {
  const cta = wrapper.find('button.profile-picker-cta')
  if (cta.element instanceof HTMLButtonElement) {
    await cta.trigger('click')
  }
}

/** 闭环一两步式：点岗位卡打开关注点面板，再点"开始训练"真正建会话。 */
async function chooseProfileAndStart(wrapper: VueWrapper, profileTitle: string): Promise<void> {
  await wrapper.get(`button[aria-label="选择${profileTitle}"]`).trigger('click')
  await wrapper.get('.profile-focus-panel .profile-picker-cta').trigger('click')
}

describe('LivePractice', () => {
  afterEach(() => {
    sessionStorage.clear()
    vi.useRealTimers()
  })

  it('splits the profile entry into a welcome page and a select page', async () => {
    const api = fakeApi()
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    expect(wrapper.get('#profile-picker-title').text())
      .toBe('从岗位任务出发，练出上手就能用的数据能力')
    expect(wrapper.find('.profile-picker-cta').exists()).toBe(true)
    expect(wrapper.find('.profile-choice-grid').exists()).toBe(false)

    await wrapper.get('button.profile-picker-cta').trigger('click')
    expect(wrapper.find('.profile-choice-grid').exists()).toBe(true)
    // 闭环一两步式：关注点下拉移入选定岗位后的面板，选择页本身不再直接展示
    expect(wrapper.find('#experience-focus').exists()).toBe(false)
    expect(wrapper.find('.profile-picker-back').exists()).toBe(true)
    expect(wrapper.find('.profile-picker-hero').exists()).toBe(false)

    await wrapper.get('button.profile-picker-back').trigger('click')
    expect(wrapper.find('.profile-picker-hero').exists()).toBe(true)
    expect(wrapper.find('.profile-choice-grid').exists()).toBe(false)
    wrapper.unmount()
  })

  it('refreshes the next diagnostic probe and clears the previous answer', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const diagnosticState = sessionState({
      awaiting: 'diagnostic_probe',
      interaction: {
        kind: 'supplemental_diagnosis',
        title: '补充诊断',
        message: '需要完成两道校准探针。',
        questions: [],
      },
    })
    const basic: InteractiveDiagnosticProbe = {
      probe_id: 'DP-02-B',
      knowledge_point: '偏差率与风险等级',
      difficulty: 'basic',
      stem: '基础探针',
    }
    const applied: InteractiveDiagnosticProbe = {
      probe_id: 'DP-02-A',
      knowledge_point: '偏差率与风险等级',
      difficulty: 'applied',
      stem: '应用探针',
    }
    vi.mocked(api.getState).mockResolvedValue(diagnosticState)
    vi.mocked(api.getDiagnosticProbes)
      .mockResolvedValueOnce([basic])
      .mockResolvedValueOnce([applied])
    vi.mocked(api.submitDiagnosticProbes).mockResolvedValue(diagnosticState)

    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()
    await wrapper.get('.diagnostic-probe-question textarea').setValue('基础探针答案')
    await wrapper.get('.diagnostic-probe-form').trigger('submit')
    await flushPromises()

    expect(api.getDiagnosticProbes).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('应用探针')
    expect(wrapper.text()).not.toContain('基础探针答案')
    expect(wrapper.get('.diagnostic-probe-question textarea').element)
      .toHaveProperty('value', '')
    // 优化5：道数进度与小题属性行已删——界面不再出现这些字样
    expect(wrapper.find('.pretest-progress-copy').exists()).toBe(false)
    expect(wrapper.find('.diagnostic-probe-question small').exists()).toBe(false)
  })

  it('keeps the single-probe view minimal without step counters (优化5)', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const diagnosticState = sessionState({
      awaiting: 'diagnostic_probe',
      interaction: {
        kind: 'supplemental_diagnosis',
        title: '补充诊断',
        probe_step: 1,
        probe_total: 1,
        provisional_route: { knowledge_point: '计划量与实际量口径' },
        message: '岗前测评未暴露明确错题，请再回答1-2道小题帮助确认学习起点。',
        questions: [],
      },
    })
    vi.mocked(api.getState).mockResolvedValue(diagnosticState)
    vi.mocked(api.getDiagnosticProbes).mockResolvedValue([{
      probe_id: 'AP-02',
      knowledge_point: '完成率计算',
      difficulty: 'applied',
      stem: '应用探针题目',
    }])
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    // 起点行改口径；题号/属性行/进度条全部不再渲染
    expect(wrapper.text()).toContain('训练关注点：计划量与实际量口径')
    expect(wrapper.text()).not.toContain('初步判断的起点')
    expect(wrapper.find('.pretest-progress-copy').exists()).toBe(false)
    const legend = wrapper.get('.diagnostic-probe-question legend').text()
    expect(legend).toContain('应用探针题目')
    expect(legend).not.toMatch(/^\d/)
  })

  it('scopes focus options to the chosen profile domain (6/7/5, all in-scope)', async () => {
    const api = fakeApi()
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await enterProfileSelectPage(wrapper)

    const expectations: Array<[number, string, string[]]> = [
      [0, '新入职生产计划员', ['三道工序与传导关系', '计划量与实际量口径', '传导时滞分析', '异常衰减规律', '责任单元定位', '跨工序归因方法']],
      [1, '转岗数字化的工艺工程师', ['计划量与实际量口径', '完成率计算', '偏差率与风险等级', '月度聚合方法', '异常识别标准', '责任单元定位', '跨工序归因方法']],
      [2, '一线班组长（晋升培训）', ['计划量与实际量口径', '完成率计算', '偏差率与风险等级', '异常识别标准', '责任单元定位']],
    ]
    for (const [cardIndex, title, scope] of expectations) {
      await wrapper.findAll('.profile-choice')[cardIndex]!.trigger('click')
      const panel = wrapper.get('.profile-focus-panel')
      expect(panel.text()).toContain(title)
      // 0819 bug4：下拉说明小字已删——领域过滤仍由选项列表验证（下方断言）
      const options = wrapper.findAll('#experience-focus option')
        .map((option) => option.text())
        .filter((text) => text !== '由岗前测评自动诊断')
      expect(options).toHaveLength(scope.length)
      // 每个选项的知识点段都必须落在该画像领域内
      for (const point of scope) {
        expect(options.some((text) => text.includes(point))).toBe(true)
      }
      await wrapper.get('.profile-picker-heading .profile-picker-back').trigger('click')
    }

    // 先选 leader 域外标签（如归因）再切岗，应被清空回退"自动诊断"
    await wrapper.findAll('.profile-choice')[0]!.trigger('click')
    await wrapper.get('#experience-focus').setValue('decay_pattern_review')
    await wrapper.get('.profile-picker-heading .profile-picker-back').trigger('click')
    await wrapper.findAll('.profile-choice')[2]!.trigger('click')
    const leaderValues = wrapper.findAll('#experience-focus option')
      .map((option) => (option.element as HTMLOptionElement).value)
    // leader 域内无"异常衰减"标签——下拉不含它，且当前选择回退默认
    expect(leaderValues).not.toContain('decay_pattern_review')
    expect((wrapper.get('#experience-focus').element as HTMLSelectElement).value).toBe('')
    wrapper.unmount()
  })

  it('passes the optional learner experience focus into session creation', async () => {
    const api = fakeApi()
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })

    await enterProfileSelectPage(wrapper)
    await wrapper.findAll('.profile-choice')[0]!.trigger('click')
    // 两步式：选定岗位后出现关注点面板（选项已按 planner 域过滤）
    expect(wrapper.get('.profile-focus-panel').text()).toContain('新入职生产计划员')
    await wrapper.get('#experience-focus').setValue('decay_pattern_review')
    await wrapper.get('.profile-focus-panel .profile-picker-cta').trigger('click')
    await flushPromises()

    expect(api.createSession).toHaveBeenCalledWith(
      'planner_new',
      ['decay_pattern_review'],
      // 0818 需求 5/6：建会话携带登录 token（游客为 null）
      null,
    )
  })

  it('shows progressive teacher hints without revealing a complete SQL answer', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S4_VERIFY',
      awaiting: 'sql',
      messages: [{
        agent: 'task',
        payload: { content: {
          contextualized_stem: '按工序比较三道工序完成率',
          knowledge_point: '三道工序与传导关系',
          difficulty: 'basic',
          query_authority: {
            output_columns: ['process_code', 'complete_rate'],
            filter_columns: ['ship_no', 'process_code', 'period_date'],
            group_by_columns: ['process_code'],
            time_values: ['2025-05', '2025-06', '2025-07'],
          },
        } },
      }],
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    expect(wrapper.text()).toContain('查询输错时会留在本题')
    await wrapper.get('.sql-teacher-hint button').trigger('click')
    await wrapper.get('.sql-teacher-hint button').trigger('click')
    expect(wrapper.get('.sql-teacher-hint').text()).toContain('工序、完成率')
    await wrapper.get('.sql-teacher-hint button').trigger('click')
    await wrapper.get('.sql-teacher-hint button').trigger('click')
    expect(wrapper.get('.sql-teacher-hint').text()).toContain('工序与月份一一对应')
    expect(wrapper.get('.sql-teacher-hint').text()).not.toContain('SELECT process_code')
  })

  it('renders server-side progressive support with its attempt number', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      sql_support: {
        attempt: 3,
        level: 'structured_hint',
        hint: '先核对输出字段 plan_qty、actual_qty 和筛选字段 ship_no、period_date。',
        will_step_down: false,
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    const support = wrapper.get('.sql-hint-banner')
    expect(support.attributes('data-level')).toBe('structured_hint')
    expect(support.text()).toContain('第 3 次提示')
    expect(support.text()).toContain('计划量')
    expect(support.text()).toContain('日期')
    expect(support.text()).not.toContain('plan_qty')
    expect(support.text()).not.toContain('period_date')
  })

  it('shows answer feedback and the reason before advancing', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S9_PATH_UPDATE',
      awaiting: 'advance',
      interaction: {
        kind: 'next_learning_step',
        message: '正在安排下一步训练。',
        feedback: '回答有效：已经引用查询数据。',
        next_step_reason: '完成两轮核对并达到当前要求。',
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    expect(wrapper.get('.answer-feedback-card').text()).toContain('回答有效')
    expect(wrapper.get('.answer-feedback-card').text()).toContain('进入下一步的理由')
  })

  it('renders a deterministic report after the round is completed', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S10_DONE',
      awaiting: 'done',
      outcome: 'completed',
      training_report: {
        title: '本轮训练报告',
        knowledge_point: '三道工序与传导关系',
        query_count: 2,
        follow_up_rounds: 3,
        completed_correction: true,
        achievement: '完成了数据实操、证据核对和结论修正。',
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    expect(wrapper.get('.training-report').text()).toContain('三道工序与传导关系')
    expect(wrapper.get('.training-report').text()).toContain('2 次')
    expect(wrapper.get('.training-report').text()).toContain('3 轮')
    expect(wrapper.get('.training-report').text()).toContain('已完成')
  })

  it('explains a safe stop with its reason and bounded review count', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S6_DEBATE',
      awaiting: 'done',
      outcome: 'safe_rejected',
      interaction: {
        kind: 'review_notice',
        message: '这份内容多次未通过专业审核，本次学习已安全结束。',
      },
      termination: {
        reason_code: 'evidence_insufficient',
        review_attempts: 3,
        review_limit: 3,
      },
    }))

    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    const explanation = wrapper.get('[data-testid="termination-explanation"]')
    expect(explanation.text()).toContain('证据不足')
    expect(explanation.text()).toContain('已完成 3/3 轮质量审核')
  })

  it('keeps the report focused and exposes the next knowledge point from the report', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S10_DONE',
      awaiting: 'done',
      outcome: 'completed',
      interaction: null,
      training_report: {
        title: '本轮训练报告',
        knowledge_point: '三道工序与传导关系',
        pretest_score: { correct: 3, total: 5, rate: 0.6 },
        query_count: 2,
        follow_up_rounds: 2,
        completed_correction: true,
        achievement: '本轮训练已经完成。',
        next_knowledge_point: '计划量与实际量口径',
      },
    }))
    const sqlResult = sqlResultMessage()
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, sqlResult },
    })
    await flushPromises()

    expect(wrapper.find('.task-inline-result').exists()).toBe(false)
    expect(wrapper.get('.training-report-metrics').text()).toContain('岗前测评正确')
    expect(wrapper.get('.training-report-metrics').text()).toContain('3/5')
    expect(wrapper.get('.training-report-next').text()).toContain('计划量与实际量口径')
    expect(wrapper.find('button[aria-label="开始下一知识点"]').exists()).toBe(true)
  })

  it('keeps the latest verified query result inside the task station', async () => {
    const api = fakeApi()
    api.createSession = vi.fn(async () => sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    const sqlResult: TraceMessage = {
      msgId: 'sql-result-1',
      traceId: 'interactive-session-live',
      step: 8,
      agent: 'verification',
      role: 'produce',
      payloadType: 'sql_result',
      content: {
        question: '按工序查询完成率',
        columns: ['process_code', 'complete_rate'],
        rows: [{ process_code: 'YCL', complete_rate: '0.6236' }],
      },
      evidence: [],
      claims: [],
      timestamp: '2026-07-30T00:00:00Z',
      rejectedByBus: false,
      busErrors: [],
    }
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, sqlResult },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(wrapper.get('.task-inline-result').text()).toContain('完成率')
    expect(wrapper.get('.task-inline-result').text()).not.toContain('按工序查询')
    expect(wrapper.get('.task-inline-result').text()).toContain('YCL')
    expect(wrapper.classes()).toContain('has-inline-result')
  })

  it('hides the stale query result during the remediation pause', async () => {
    const api = fakeApi()
    api.createSession = vi.fn(async () => sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
      artifact: {
        msg_id: 'artifact-t17-control',
        payload: {
          type: 'control',
          content: { action: 'state_transition', transition_id: 'T17' },
        },
      } as unknown as InteractiveState['artifact'],
      interaction: {
        kind: 'learning_notice',
        message: '四次理解核对未达成掌握目标，当前已是基础档，系统将更换证据与讲解角度后再练习一次。',
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, sqlResult: sqlResultMessage() },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    // 需求④⑤：四次未过停驻页显示中央双行提示（学习通知不再露出），结果表隐藏
    expect(wrapper.get('[data-testid="generation-pending"]').text())
      .toContain('四次理解核对未达成掌握目标，')
    expect(wrapper.get('[data-testid="generation-pending"]').text())
      .toContain('系统将更换证据与讲解角度后再练习一次。')
    expect(wrapper.get('[data-testid="generation-pending"]').text())
      .toContain('即将进入学习，请稍候')
    expect(wrapper.find('[data-testid="learning-notice"]').exists()).toBe(false)
    expect(wrapper.find('.task-inline-result').exists()).toBe(false)
    expect(wrapper.classes()).not.toContain('has-inline-result')
  })

  it('hides the stale query result at the claim gate where the artifact is the lecture', async () => {
    const api = fakeApi()
    api.createSession = vi.fn(async () => sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      artifact: {
        msg_id: 'artifact-new-lecture',
        payload: { type: 'lecture_note', content: { knowledge_point: '三道工序与传导关系' } },
      } as unknown as InteractiveState['artifact'],
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, sqlResult: sqlResultMessage() },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(wrapper.find('.task-inline-result').exists()).toBe(false)
    expect(wrapper.classes()).not.toContain('has-inline-result')
  })

  it('keeps the verified query result visible while a reviewed artifact is pending', async () => {
    const api = fakeApi()
    api.createSession = vi.fn(async () => sessionState({
      state: 'S7_STUDENT',
      awaiting: 'advance',
      artifact: {
        msg_id: 'artifact-approved',
        payload: { type: 'sql_result', content: { event: 'query_completed' } },
      } as unknown as InteractiveState['artifact'],
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, sqlResult: sqlResultMessage() },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(wrapper.find('.task-inline-result').exists()).toBe(true)
    expect(wrapper.classes()).toContain('has-inline-result')
  })

  it('binds profile cards to approved human-facing profile data only', async () => {
    const wrapper = mount(LivePractice, {
      props: { api: fakeApi(), pollIntervalMs: 0 },
    })

    expect(wrapper.get('#profile-picker-title').text())
      .toBe('从岗位任务出发，练出上手就能用的数据能力')
    await enterProfileSelectPage(wrapper)
    expect(wrapper.findAll('.profile-choice')).toHaveLength(3)
    expect(wrapper.text()).toContain('初入船厂计划岗位的新人，熟悉办公与数据工具的使用，正在建立船舶生产口径的概念')
    expect(wrapper.text()).toContain('由工艺现场转岗数字化的工程师，深耕预处理与托盘工艺，数据分析能力正在起步')
    expect(wrapper.text()).toContain('常年带队的一线班组长，现场经验丰富，需要夯实数据与理论基础')
    // 0819 bug3：画像卡下方特长小椭圆已删除
    expect(wrapper.text()).not.toContain('SQL基础')
    expect(wrapper.text()).not.toContain('现场生产经验')
    expect(wrapper.text()).not.toContain('重讲工序与口径、少讲SQL')
    expect(wrapper.text()).not.toContain('步骤化短句、每步带检查点')
    expect(wrapper.text()).not.toContain('重点补足')
  })

  it('collects all five learner choices before submitting the real pretest', async () => {
    const api = fakeApi()
    vi.mocked(api.advance).mockResolvedValue(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      messages: [{
        msg_id: 'lecture-deferred',
        agent: 'knowledge',
        payload: { type: 'lecture_note', content: { lecture_deferred: true } },
      }],
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(wrapper.findAll('fieldset.pretest-question')).toHaveLength(1)
    expect(wrapper.text()).toContain('1/ 5')
    for (const [index, question] of questions.entries()) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
      if (index < questions.length - 1) {
        await wrapper.get('.pretest-page-actions .primary-action').trigger('click')
      }
    }
    await wrapper.get('button[type="submit"]').trigger('submit')
    await flushPromises()

    expect(api.submitPretest).toHaveBeenCalledWith('session-live', {
      'PT-1': 'B',
      'PT-2': 'B',
      'PT-3': 'B',
      'PT-4': 'B',
      'PT-5': 'B',
    })
    // 需求④⑤：提交后直达链自动推进（S2→S3，同态确认后即停），不再出现"打开岗位微课"按钮
    expect(api.advance).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).not.toContain('打开岗位微课')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({
      state: 'S3_TASK',
      awaiting: 'advance',
    })
  })

  it('gives long option copy a dedicated wrapping hook', async () => {
    const api = fakeApi()
    vi.mocked(api.getPretest).mockResolvedValue(questions.map((question, index) => (
      index === 0
        ? {
            ...question,
            stem: '计算某船某月某工序的完成率时，以下哪个口径正确？',
            options: {
              ...question.options,
              A: '完成数据当月每日完成率的平均值',
            },
          }
        : question
    )))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    const firstQuestion = wrapper.get('fieldset.pretest-question')
    expect(firstQuestion.findAll('.option-copy')).toHaveLength(4)
    expect(firstQuestion.get('.option-copy').text())
      .toBe('完成数据当月每日完成率的平均值')
  })

  it('moves through the pretest one question at a time without a page-length form', async () => {
    const api = fakeApi()
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(wrapper.findAll('fieldset.pretest-question')).toHaveLength(1)
    expect(wrapper.get('fieldset.pretest-question').text()).toContain('第1题')
    expect(wrapper.get('.pretest-page-actions .primary-action').attributes('disabled')).toBeDefined()

    await wrapper.get('input[name="PT-1"][value="B"]').setValue(true)
    await wrapper.get('.pretest-page-actions .primary-action').trigger('click')

    expect(wrapper.get('fieldset.pretest-question').text()).toContain('第2题')
    expect(wrapper.get('.pretest-progress-copy').text()).toContain('2/ 5')
  })

  it('ignores a repeated advance click while the first request is running', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    }))
    let finishAdvance!: (value: InteractiveState) => void
    vi.mocked(api.advance).mockReturnValue(new Promise((resolve) => {
      finishAdvance = resolve
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    const button = wrapper.get('button[aria-label="打开岗位微课"]')
    await button.trigger('click')
    await button.trigger('click')

    expect(api.advance).toHaveBeenCalledTimes(1)
    // 需求①：等待态为中央友好提示句，按钮隐藏
    expect(wrapper.get('[data-testid="generation-pending"]').text()).toContain('正在为你准备专属讲义')
    expect(wrapper.find('button[aria-label="打开岗位微课"]').exists()).toBe(false)
    finishAdvance(sessionState({ state: 'S3_TASK', awaiting: 'advance' }))
    await flushPromises()
  })

  it('reconciles a late successful state after the advance response fails', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const before = sessionState({ state: 'S2_KNOWLEDGE', awaiting: 'advance' })
    const recovered = sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      messages: [{ msg_id: 'lecture-ready' }],
      artifact: { artifact_id: 'lecture-ready' },
    })
    vi.mocked(api.getState)
      .mockResolvedValueOnce(before)
      .mockResolvedValueOnce(recovered)
    vi.mocked(api.advance).mockRejectedValue(new InteractiveApiError(
      '当前步骤暂时无法继续，请稍后再试。',
      'system_error',
    ))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="打开岗位微课"]').trigger('click')
    await flushPromises()

    expect(api.getState).toHaveBeenCalledTimes(2)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    // S3 按钮已隐藏（directPathIdle 对 S3/S9 无条件 true）
    expect(wrapper.text()).not.toContain('领取实操任务')
  })

  it('walks the learner through SQL and a reviewed free-text correction', async () => {
    const api = fakeApi()
    vi.mocked(api.submitPretest).mockResolvedValue(sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    }))
    vi.mocked(api.advance)
      .mockResolvedValueOnce(sessionState({
        state: 'S3_TASK',
        awaiting: 'advance',
        messages: [{
          msg_id: 'lecture-deferred',
          agent: 'knowledge',
          payload: { type: 'lecture_note', content: { lecture_deferred: true } },
        }],
      }))
      .mockResolvedValueOnce(sessionState({ state: 'S7_STUDENT', awaiting: 'sql' }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'follow_up',
        interaction: {
          kind: 'free_text_follow_up',
          prompt: '根据你查出的数据，怎样判断真实完成情况？',
          round: 1,
          max_rounds: 4,
          turns: [],
        },
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'sql',
        interaction: {
          kind: 'learning_notice',
          message: '根据本次作答表现，已为你提高一档难度。',
        },
      }))
      .mockResolvedValueOnce(sessionState({ state: 'S10_DONE', awaiting: 'done' }))
    vi.mocked(api.submitSql)
      .mockResolvedValueOnce(sessionState({ state: 'S9_PATH_UPDATE', awaiting: 'advance' }))
      .mockResolvedValueOnce(sessionState({
        state: 'S9_PATH_UPDATE',
        awaiting: 'advance',
        interaction: {
          kind: 'learning_notice',
          message: '根据本次作答表现，已为你提高一档难度。',
        },
      }))
    vi.mocked(api.submitFollowUp)
      .mockResolvedValueOnce(sessionState({
        state: 'S8_PROBE',
        awaiting: 'follow_up',
        interaction: {
          kind: 'free_text_follow_up',
          prompt: '对照计划量与实际完成量，哪一个说明真正做了多少？',
          round: 2,
          max_rounds: 4,
          turns: [{
            round: 1,
            question: '根据你查出的数据，怎样判断真实完成情况？',
            answer: '计划量就是已经完成的数量。',
          }],
        },
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S9_PATH_UPDATE',
        awaiting: 'advance',
        interaction: {
          kind: 'data_collision',
          misconception: 'M-01',
          wrong_label: '计划量',
          wrong_value: '1855.06',
          correct_label: '实际完成量',
          correct_value: '1156.87',
        },
      }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()
    for (const [index, question] of questions.entries()) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
      if (index < questions.length - 1) {
        await wrapper.get('.pretest-page-actions .primary-action').trigger('click')
      }
    }
    await wrapper.get('button[type="submit"]').trigger('submit')
    await flushPromises()

    // 需求④⑤：提交后直达链自动推进（S2→S3→S7 sql 全部自动，无"领取实操任务"按钮页）
    expect(wrapper.find('button[aria-label="领取实操任务"]').exists()).toBe(false)
    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue('SELECT plan_qty FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()
    // 需求⑤：查询通过后自动直达提问，"判断查询结论"按钮已删除
    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('计划量就是已经完成的数量。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('.follow-up-current span').text()).toBe('第 2 轮')
    expect(wrapper.text()).toContain('你的回答')
    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('实际完成量才表示真正做了多少。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()
    // S9 按钮已隐藏——直接调 advance 消费 mock 链下一响应
    await wrapper.getComponent(LivePractice).vm.advance()
    await flushPromises()
    expect(wrapper.get('[data-testid="learning-notice"]').text())
      .toBe('根据本次作答表现，已为你提高一档难度。')
    await wrapper.get('textarea[aria-label="输入查询语句"]')
      .setValue('SELECT workshop_code, complete_rate FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(api.advance).toHaveBeenCalledTimes(5)
    expect(api.submitSql).toHaveBeenNthCalledWith(
      1,
      'session-live',
      'SELECT plan_qty FROM fact_production_progress',
    )
    expect(api.submitSql).toHaveBeenNthCalledWith(
      2,
      'session-live',
      'SELECT workshop_code, complete_rate FROM fact_production_progress',
    )
    expect(api.submitFollowUp).toHaveBeenNthCalledWith(
      1,
      'session-live',
      '计划量就是已经完成的数量。',
      expect.any(String),
    )
    expect(api.submitFollowUp).toHaveBeenNthCalledWith(
      2,
      'session-live',
      '实际完成量才表示真正做了多少。',
      expect.any(String),
    )
    expect(wrapper.get('.live-complete').text()).toBe('训练完成')
    expect(wrapper.text()).not.toContain('本次修正与数据证据已写入培养记录')
  })

  it.each([
    '这份内容多次未通过专业审核，本次学习已安全结束。',
    '这份内容需要进一步确认，本次学习已暂停。',
    '这份内容未通过专业审核，本次学习已安全结束。',
  ])('shows a review stop as teaching language instead of completion: %s', async (message) => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S_FAIL',
      awaiting: 'done',
      outcome: 'safe_rejected',
      interaction: {
        kind: 'review_notice',
        message,
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.get('.live-complete').text()).toBe(message)
    expect(wrapper.get('.live-complete').text()).not.toContain('训练完成')
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it('shows an actual difficulty increase only in learner-facing language', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      interaction: {
        kind: 'learning_notice',
        message: '根据本次作答表现，已为你提高一档难度。',
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="learning-notice"]').text())
      .toBe('根据本次作答表现，已为你提高一档难度。')
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it('keeps the approved task prompt visible above the SQL editor', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      messages: [{
        agent: 'task',
        payload: {
          type: 'quiz_set',
          content: {
            contextualized_stem: '查询H2601船2025年5月YCL工序的计划量与实际完成量。',
          },
        },
      }],
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.get('.sql-task-brief').text())
      .toContain('查询H2601船2025年5月YCL工序的计划量与实际完成量。')
    wrapper.get('textarea[aria-label="输入查询语句"]')
  })

  it('guards a difficulty notice received from the interactive service', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      interaction: {
        kind: 'learning_notice',
        message: 'step_up template_id=T-01-A state=S7_STUDENT',
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.get('[data-testid="learning-notice"]').text())
      .toBe('当前内容暂时无法展示，请稍后再试。')
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it('offers the next learning step without exposing progression codes', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S9_PATH_UPDATE',
      awaiting: 'advance',
      interaction: {
        kind: 'next_learning_step',
        message: '回答正确，正在为你安排下一步训练。',
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.find('button[aria-label="查看下一步训练"]').exists()).toBe(false)
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it('continues a completed curriculum at the next knowledge point', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S10_DONE',
      awaiting: 'done',
      outcome: 'completed',
      interaction: {
        kind: 'next_learning_step',
        message: '下一知识点：完成率计算',
        knowledge_point: '完成率计算',
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('下一知识点：完成率计算')
    await wrapper.get('button[aria-label="开始下一知识点"]').trigger('click')
    await flushPromises()

    expect(api.continueLearning).toHaveBeenCalledWith('session-live')
    expect(sessionStorage.getItem('ref-interactive-session')).toBe('session-next')
    expect(wrapper.get('[data-testid="learning-notice"]').text())
      .toContain('下一知识点：完成率计算')
    expect(wrapper.find('button[aria-label="打开岗位微课"]').exists()).toBe(true)
  })

  it('resumes a stored session and polls server-owned state', async () => {
    vi.useFakeTimers()
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState)
      .mockResolvedValueOnce(sessionState({ state: 'S7_STUDENT', awaiting: 'sql' }))
      .mockResolvedValueOnce(sessionState({ state: 'S10_DONE', awaiting: 'done' }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 1000 },
    })
    await flushPromises()

    expect(api.getState).toHaveBeenCalledWith('session-live')
    expect(wrapper.find('textarea[aria-label="输入查询语句"]').exists()).toBe(true)

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(api.getState).toHaveBeenCalledTimes(2)
    expect(wrapper.get('.live-complete').text()).toBe('训练完成')
    wrapper.unmount()
  })

  it('clears the SQL draft when polling switches to a different learning task', async () => {
    vi.useFakeTimers()
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const taskMessage = (knowledgePoint: string, templateId: string) => ({
      agent: 'task',
      role: 'produce',
      msg_id: `task-${templateId}`,
      payload: { content: {
        event: 'product_ready',
        knowledge_point: knowledgePoint,
        template_id: templateId,
        difficulty: 'basic',
        question: `完成${knowledgePoint}查询`,
      } },
    })
    vi.mocked(api.getState)
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT', awaiting: 'sql',
        messages: [taskMessage('计划量与实际量口径', 'T-01')],
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT', awaiting: 'sql',
        messages: [
          taskMessage('计划量与实际量口径', 'T-01'),
          taskMessage('月度聚合方法', 'T-03'),
        ],
      }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 1000 } })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]')
      .setValue('SELECT actual_qty FROM fact_production_progress')
    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect((wrapper.get('textarea[aria-label="输入查询语句"]').element as HTMLTextAreaElement).value)
      .toBe('')
    wrapper.unmount()
  })

  it('keeps and retries a stored session after a transient polling failure', async () => {
    vi.useFakeTimers()
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState)
      .mockRejectedValueOnce(new InteractiveApiError(
        '服务暂时不可用，请稍后再试。',
        'external_unavailable',
      ))
      .mockResolvedValueOnce(sessionState({ state: 'S7_STUDENT', awaiting: 'sql' }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 1000 },
    })
    await flushPromises()

    expect(sessionStorage.getItem('ref-interactive-session')).toBe('session-live')
    expect(wrapper.get('[role="alert"]').text()).toBe('服务暂时不可用，请稍后再试。')

    await vi.advanceTimersByTimeAsync(1000)
    await flushPromises()

    expect(api.getState).toHaveBeenCalledTimes(2)
    expect(sessionStorage.getItem('ref-interactive-session')).toBe('session-live')
    expect(wrapper.find('textarea[aria-label="输入查询语句"]').exists()).toBe(true)
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('clears a stored session only when polling confirms it no longer exists', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockRejectedValue(new InteractiveApiError(
      '实操通道暂时不可用，请稍后再试。',
      undefined,
      404,
    ))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(sessionStorage.getItem('ref-interactive-session')).toBeNull()
    await enterProfileSelectPage(wrapper)
    expect(wrapper.find('button[aria-label="选择新入职生产计划员"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('lets the learner restart a restored session from the profile picker', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="重新选择岗位"]').trigger('click')

    expect(sessionStorage.getItem('ref-interactive-session')).toBeNull()
    expect(wrapper.find('button[aria-label="选择新入职生产计划员"]').exists()).toBe(true)
    expect(wrapper.find('textarea[aria-label="输入查询语句"]').exists()).toBe(false)
  })

  it('shows the sandbox teaching label without exposing its rule id', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      artifact: {
        payload: {
          type: 'sql_result',
          content: {
            event: 'sandbox_rejected',
            rule_id: 'S-01',
            rule_reason: 'only SELECT statements are allowed',
            student_message: '只允许查询数据，请使用 SELECT。',
          },
        },
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue('DELETE FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    const rejection = wrapper.get('.sql-hint-banner')
    expect(rejection.get('strong').text()).toBe('查询提示')
    expect(rejection.text()).not.toMatch(/S-0\d/u)
    expect(rejection.text()).toContain('只允许查询数据，请使用 SELECT')
    expect(rejection.text()).not.toContain('only SELECT statements are allowed')
    expect(wrapper.get('textarea').element.value).toBe('DELETE FROM fact_production_progress')
  })

  it('uses a safe learner-facing fallback when a sandbox rule id is missing', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      artifact: {
        payload: {
          type: 'sql_result',
          content: {
            event: 'sandbox_rejected',
            student_message: '查询未通过安全检查，请修改后重试。',
          },
        },
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue('DELETE FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('.sql-hint-banner strong').text())
      .toBe('查询提示')
  })

  it('keeps a timed-out raw query available for a smaller retry', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT', awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      artifact: {
        payload: {
          type: 'sql_result',
          content: {
            event: 'query_timeout',
            student_message: '查询超时，请缩小查询范围。',
          },
        },
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue('SELECT plan_qty FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('.sql-hint-banner').text())
      .toContain('查询超时，请缩小查询范围')
    expect(wrapper.get('textarea').element.value)
      .toBe('SELECT plan_qty FROM fact_production_progress')
  })

  it('removes backend implementation language from a failed-query message', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT', awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      artifact: {
        payload: {
          type: 'sql_result',
          content: {
            event: 'query_failed',
            student_message: '只读查询未能执行，请稍后重试。',
          },
        },
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]')
      .setValue('SELECT plan_qty FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('.sql-hint-banner').text())
      .toContain('查询未能执行，请稍后重试')
    expect(wrapper.text()).not.toContain('只读')
  })

  it('never exposes a raw service error to the learner', async () => {
    const api = fakeApi()
    vi.mocked(api.createSession).mockRejectedValue(
      new Error('trace_id is invalid at orchestrator.interactive_session:500'),
    )
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toBe('实操通道暂时不可用。')
    expect(wrapper.text()).not.toMatch(/trace_id|orchestrator|interactive_session/)
  })

  it.each([
    {
      event: 'refuse_out_of_scope',
      outcome: 'safe_rejected',
      expected: '这个问题不在本次训练的数据范围内，请换一个与岗位任务相关的问题。',
    },
    {
      event: 'sandbox_rejected',
      outcome: 'safe_rejected',
      expected: '本题的查询未通过数据安全检查，请调整后重试。',
    },
    {
      event: 'template_authority_rejected',
      outcome: 'safe_rejected',
      expected: '查询结构与本题目标尚未完全对应，请核对对象、月份、筛选条件和分组维度后重试。',
    },
    {
      event: 'query_empty',
      outcome: 'safe_rejected',
      expected: '本次查询没有返回数据，请调整查询条件后重试。',
    },
    {
      event: 'query_timeout',
      outcome: 'external_unavailable',
      expected: '服务暂时不可用，请稍后再试。',
    },
    {
      event: 'query_failed',
      outcome: 'external_unavailable',
      expected: '服务暂时不可用，请稍后再试。',
    },
  ])('maps $event to teaching language and keeps the query available', async ({
    event,
    outcome,
    expected,
  }) => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      artifact: {
        payload: {
          type: 'sql_result',
          content: {
            event,
            ...(event === 'sandbox_rejected' ? { rule_id: 'S-04' } : {}),
            student_message: 'T21 S4_VERIFY no_matching_transition msg_id rule_hits',
          },
        },
      },
      ...({ outcome } as Record<string, unknown>),
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    const sql = 'SELECT plan_qty FROM fact_production_progress'
    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue(sql)
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain(expected)
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
    expect(wrapper.get('textarea').element.value).toBe(sql)
  })

  it.each([
    ['safe_rejected', '本题的查询未通过数据安全检查，请调整后重试。'],
    ['external_unavailable', '服务暂时不可用，请稍后再试。'],
    ['system_error', '当前步骤暂时无法继续，请稍后再试。'],
  ])('maps an outcome-only %s state to public copy', async (outcome, expected) => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      ...({ outcome } as Record<string, unknown>),
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toContain(expected)
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it.each([
    ['safe_rejected', '本题的查询未通过数据安全检查，请调整后重试。'],
    ['external_unavailable', '服务暂时不可用，请稍后再试。'],
    ['system_error', '当前步骤暂时无法继续，请稍后再试。'],
  ] as const)('renders a typed %s request failure only as public copy', async (outcome, expected) => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockRejectedValue(new InteractiveApiError(expected, outcome))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]')
      .setValue('SELECT plan_qty FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toBe(expected)
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it.each([
    [
      'safe_rejected',
      '本题的查询未通过数据安全检查，请调整后重试。',
    ],
    [
      'external_unavailable',
      '服务暂时不可用，请稍后再试。',
    ],
    [
      'system_error',
      '当前步骤暂时无法继续，请稍后再试。',
    ],
    [
      undefined,
      '查询暂时无法执行。',
    ],
  ] as const)('does not trust a malicious typed %s error message', async (outcome, expected) => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    vi.mocked(api.submitSql).mockRejectedValue(new InteractiveApiError(
      'no_matching_transition at S4_VERIFY; msg_id=secret',
      outcome,
    ))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]')
      .setValue('SELECT plan_qty FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('[role="alert"]').text()).toBe(expected)
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it('guards profile and pretest text received from the interactive service', async () => {
    const api = fakeApi()
    vi.mocked(api.createSession).mockResolvedValue(sessionState({
      profile: {
        profile_id: 'planner_new',
        title: 'S4_VERIFY msg_id',
      },
    }))
    vi.mocked(api.getPretest).mockResolvedValue(questions.map((question) => ({
      ...question,
      stem: 'T21 no_matching_transition',
      options: {
        A: 'routing_predicted_family Q4',
        B: 'safe_rejected',
        C: 'rule_hits',
        D: 'system_error',
      },
    })))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })

    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()

    expect(isLearnerSafeText(wrapper.text())).toBe(true)
    expect(wrapper.text()).toContain('当前内容暂时无法展示，请稍后再试。')
  })

  it('guards every dynamic follow-up field before rendering it', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: 'verdict at S4_VERIFY',
        round: 2,
        max_rounds: 4,
        turns: [
          {
            round: 1,
            question: 'rebuttal msg_id',
            answer: 'routing_final_family Q5',
          },
        ],
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    expect(isLearnerSafeText(wrapper.text())).toBe(true)
    expect(wrapper.text()).toContain('当前内容暂时无法展示，请稍后再试。')
  })

  it('rejects engineering-language input before calling the service', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '请说明哪类数据能够反映真实完成情况？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('请告诉我 msg_id 和 T15。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()

    expect(api.submitFollowUp).not.toHaveBeenCalled()
    expect(wrapper.get('[role="alert"]').text())
      .toBe('请用业务或学习语言描述你的判断。')
    expect(isLearnerSafeText(wrapper.text())).toBe(true)
  })

  it('asks for evidence instead of submitting a bare yes-or-no answer', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '哪一道工序完成率最低，你依据的数值是什么？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入你的判断"]').setValue('是的')

    const button = wrapper.get('button[aria-label="提交本轮判断"]')
    expect(button.attributes('disabled')).toBeDefined()
    expect(wrapper.get('.follow-up-actions small').text())
      .toContain('不能只答“是/否”')
    expect(api.submitFollowUp).not.toHaveBeenCalled()
  })

  it('collapses completed rounds so the next prompt and input remain visible', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S8_PROBE',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '查询结果中AZTP和ZZTP的完成率分别是多少？',
        round: 3,
        max_rounds: 4,
        feedback: '回答已经引用YCL和0.6236。',
        next_step_reason: '继续核对另外两道工序。',
        turns: [
          {
            round: 1,
            question: '哪一道工序完成率最低？',
            answer: 'YCL最低。',
            feedback: '请补充完成率数值。',
          },
          {
            round: 2,
            question: 'YCL的完成率是多少？',
            answer: 'YCL完成率最低 0.6236。',
            feedback: '回答已经引用YCL和0.6236。',
          },
        ],
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    const toggle = wrapper.get('.follow-up-history-toggle')
    expect(toggle.attributes('aria-expanded')).toBe('false')
    expect(wrapper.find('.follow-up-history').exists()).toBe(false)
    expect(wrapper.get('.follow-up-latest-summary').text())
      .toContain('YCL完成率最低 0.6236')
    expect(wrapper.get('.follow-up-current').text())
      .toContain('AZTP和ZZTP的完成率')
    expect(wrapper.get('textarea[aria-label="输入你的判断"]')).toBeDefined()

    await toggle.trigger('click')

    expect(toggle.attributes('aria-expanded')).toBe('true')
    // 需求③：翻页式——单条展示 + 页码指示（默认最新一条）
    expect(wrapper.findAll('.follow-up-history li')).toHaveLength(0)
    expect(wrapper.get('.follow-up-history').text()).toContain('YCL完成率最低 0.6236')
    expect(wrapper.get('.follow-up-history-pager-actions span').text()).toBe('2 / 2')
    await wrapper.get('button[aria-label="上一条记录"]').trigger('click')
    expect(wrapper.get('.follow-up-history').text()).toContain('哪一道工序完成率最低？')
    expect(wrapper.get('.follow-up-history-pager-actions span').text()).toBe('1 / 2')
    expect(wrapper.get('.follow-up-current').text())
      .toContain('AZTP和ZZTP的完成率')
  })

  it('keeps the original task visible while a reviewed follow-up is active', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S8_PROBE',
      awaiting: 'follow_up',
      messages: [
        {
          agent: 'task',
          role: 'produce',
          payload: { content: {
            contextualized_stem: '查询2025年5月至7月三道工序月完成率，筛查疑似传导。',
            knowledge_point: '传导时滞分析',
            difficulty: 'advanced',
          } },
        },
        {
          agent: 'task',
          role: 'probe',
          payload: { content: {
            event: 'follow_up_question_ready',
            question: '根据当前查询结果，你会怎样回答题目中的问题？',
          } },
        },
      ],
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '三道工序的最低完成率分别出现在哪个月？',
        task_prompt: '查询2025年5月至7月三道工序月完成率，筛查疑似传导。',
        focus: '识别工序、月份和完成率之间的对应关系',
        round: 2,
        max_rounds: 4,
        turns: [],
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    // 需求⑦：追问区不再展示任务锚块
    expect(wrapper.find('.follow-up-task-anchor').exists()).toBe(false)
    expect(wrapper.get('.follow-up-current').text())
      .toContain('三道工序的最低完成率分别出现在哪个月')
    expect(wrapper.get('.follow-up-current').text()).toContain('第 2 轮')
  })

  it('reconciles a follow-up that completed after its response failed', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const before = sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '哪一道工序完成率最低，你依据的数值是什么？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    })
    const recovered = sessionState({
      state: 'S8_PROBE',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '为什么不能只凭工序先后判断传导？',
        round: 2,
        max_rounds: 4,
        turns: [{
          round: 1,
          question: '哪一道工序完成率最低，你依据的数值是什么？',
          answer: 'YCL最低，完成率为62.36%。',
        }],
      },
    })
    vi.mocked(api.getState)
      .mockResolvedValueOnce(before)
      .mockResolvedValueOnce(recovered)
    vi.mocked(api.submitFollowUp).mockRejectedValue(new InteractiveApiError(
      '服务暂时不可用，请稍后再试。',
      'external_unavailable',
    ))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('YCL最低，完成率为62.36%。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()

    expect(api.getState).toHaveBeenCalledTimes(2)
    expect(wrapper.get('.follow-up-current span').text()).toBe('第 2 轮')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('reconciles a quality-interrupted follow-up that stays on the same round', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const before = sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '请引用查询结果说明判断。',
        round: 2,
        max_rounds: 4,
        turns: [{ round: 1, question: '上一题', answer: '上一答' }],
      },
    })
    const retained = sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '请引用查询结果说明判断。',
        round: 2,
        max_rounds: 4,
        turns: [{ round: 1, question: '上一题', answer: '上一答' }],
        feedback: '本轮新追问暂时未能通过质量检查，请按原问题重试。',
        next_step_reason: '质量门保持当前轮次，未丢失会话进度。',
        retry_required: true,
      },
    })
    vi.mocked(api.getState)
      .mockResolvedValueOnce(before)
      .mockResolvedValueOnce(retained)
    vi.mocked(api.submitFollowUp).mockRejectedValue(new InteractiveApiError(
      '服务暂时不可用，请稍后再试。',
      'external_unavailable',
    ))

    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()
    await wrapper.get('textarea[aria-label="输入你的判断"]').setValue('YCL完成率为0.6236。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('本轮新追问暂时未能通过质量检查')
    expect(wrapper.find('[role="alert"]').exists()).toBe(false)
  })

  it('keeps the draft and client turn id stable when submission is retried', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const followUpState = sessionState({
      state: 'S8_PROBE',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '换个角度看，实际完成情况应由哪类数据说明？',
        round: 2,
        max_rounds: 4,
        turns: [{
          round: 1,
          question: '请说明哪类数据能够反映真实完成情况？',
          answer: '应以实际完成量说明真实进度。',
        }],
      },
    })
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '请说明哪类数据能够反映真实完成情况？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    }))
    vi.mocked(api.submitFollowUp)
      .mockRejectedValueOnce(new InteractiveApiError(
        '服务暂时不可用，请稍后再试。',
        'external_unavailable',
      ))
      .mockResolvedValueOnce(followUpState)
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })
    await flushPromises()

    const input = wrapper.get('textarea[aria-label="输入你的判断"]')
    await input.setValue('应以实际完成量说明真实进度。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()
    expect(input.element).toHaveProperty(
      'value',
      '应以实际完成量说明真实进度。',
    )

    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()

    const firstId = vi.mocked(api.submitFollowUp).mock.calls[0][2]
    const secondId = vi.mocked(api.submitFollowUp).mock.calls[1][2]
    expect(secondId).toBe(firstId)
    expect(wrapper.find('textarea[aria-label="输入你的判断"]').exists()).toBe(true)
    expect(wrapper.get('textarea[aria-label="输入你的判断"]').element)
      .toHaveProperty('value', '')
  })

  it('does not let polling overwrite a follow-up while it is being submitted', async () => {
    vi.useFakeTimers()
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const active = sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '请说明哪类数据能够反映真实完成情况？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    })
    vi.mocked(api.getState).mockResolvedValue(active)
    let finishSubmission!: (value: InteractiveState) => void
    vi.mocked(api.submitFollowUp).mockReturnValue(new Promise((resolve) => {
      finishSubmission = resolve
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 1000 },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('应以实际完成量说明真实进度。')
    const submitting = wrapper.get(
      'button[aria-label="提交本轮判断"]',
    ).trigger('click')
    await vi.advanceTimersByTimeAsync(1000)

    expect(api.getState).toHaveBeenCalledTimes(1)
    expect(wrapper.get('.follow-up-progress').text())
      .toBe('正在根据你的回答生成并审核下一步内容…')

    finishSubmission(active)
    await submitting
    await flushPromises()
  })

  it('shows the lecture-deferred notice for pretest-verified points', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      interaction: {
        kind: 'lecture_deferred',
        message: '已由前测验证，直入实操（未通过将自动配发微课）',
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    const notice = wrapper.get('[data-testid="lecture-deferred-notice"]')
    expect(notice.text()).toContain('已由前测验证，直入实操')
    expect(wrapper.find('[data-testid="learning-notice"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('discards in-flight lecture generation after 重新选择岗位', async () => {
    // 资源生成中点击"重新选择岗位"（顶栏路径，无 busy 守卫）：迟到的讲义响应
    // 不得回填界面——页面停在岗位选择，自动链终止（applyState 会话守卫丢弃）。
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    let releaseLecture!: (value: InteractiveState) => void
    const lectureGate = new Promise<InteractiveState>((resolve) => {
      releaseLecture = resolve
    })
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    }))
    vi.mocked(api.advance).mockImplementationOnce(() => lectureGate)
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    // 生成讲义中（advance 未返回）点击"重新选择岗位"（顶栏等价于直调 resetSession）
    const advancing = (wrapper.vm as unknown as { advance: () => Promise<void> }).advance()
    await flushPromises()
    ;(wrapper.vm as unknown as { resetSession: () => void }).resetSession()
    await flushPromises()

    // 已回到岗位选择页（选择画像卡片可见）
    expect(wrapper.text()).toContain('哪一种经历最接近你')
    expect(sessionStorage.getItem('ref-interactive-session')).toBeNull()

    // 迟到的讲义响应返回——被 applyState 守卫丢弃，不显示、不恢复会话
    releaseLecture(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      messages: [{ msg_id: 'lecture-late', agent: 'knowledge', payload: { type: 'lecture_note', content: { lecture_md: '迟到讲义不应显示' } } }],
    }))
    await advancing
    await flushPromises()

    expect(sessionStorage.getItem('ref-interactive-session')).toBeNull()
    expect(wrapper.text()).not.toContain('迟到讲义不应显示')
    expect(wrapper.text()).toContain('哪一种经历最接近你')
    wrapper.unmount()
  })

  it('runs the data_present chain S3→S7 proxy→S9→follow-up without manual stops', async () => {
    // 闭环五后继：画像三延时代执行——进入练习一次点击应穿过 S7+advance（系统代执行窗口）
    // 直达追问，不再出现"查询题目"停驻与"继续训练"按钮
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const lineLeaderProfile = {
      profile_id: 'line_leader',
      title: '一线班组长（晋升培训）',
      practice_mode: 'data_present',
    }
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      profile: lineLeaderProfile,
    }))
    vi.mocked(api.advance)
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'advance',
        profile: lineLeaderProfile,
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S9_PATH_UPDATE',
        awaiting: 'advance',
        profile: lineLeaderProfile,
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'follow_up',
        profile: lineLeaderProfile,
        interaction: {
          kind: 'free_text_follow_up',
          prompt: '对照计划量与实际完成量，哪一个说明真正做了多少？',
          round: 1,
          max_rounds: 4,
          turns: [],
        },
      }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    await (wrapper.vm as unknown as { startPracticeChain: () => Promise<void> })
      .startPracticeChain()
    await flushPromises()

    // 一次点击三连推进：任务下发→代执行→追问，中间不停驻
    expect(api.advance).toHaveBeenCalledTimes(3)
    expect(wrapper.text()).not.toContain('继续训练')
    wrapper.unmount()
  })

  it('auto-continues a stuck S7 data_present proxy window via the watcher', async () => {
    // 链断兜底：S7+advance 恒定（代执行窗口卡住）时无手动按钮，pending 常显，
    // 反应式 watcher 1s 后自动续推
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
    }))
    vi.mocked(api.advance).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'advance',
    }))
    vi.useFakeTimers()
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    await (wrapper.vm as unknown as { startPracticeChain: () => Promise<void> })
      .startPracticeChain()
    await flushPromises()

    // 链在第二次同态（S7→S7）后断开：advance 已调用 2 次
    expect(api.advance).toHaveBeenCalledTimes(2)
    // 无手动按钮；S7 代执行窗口常显 pending（画像三岗位模式文案）
    expect(wrapper.text()).not.toContain('继续训练')
    const overlay = wrapper.get('[data-testid="generation-pending"]')
    expect(overlay.text()).toContain('岗位模式')

    // watcher 1s 续推：advance 第 3 次被调用
    await vi.advanceTimersByTimeAsync(1100)
    await flushPromises()
    expect(api.advance).toHaveBeenCalledTimes(3)
    wrapper.unmount()
  })

  it('waits for the learner again when a fresh lecture lands after practice was entered', async () => {
    // 0818 视频实录根因：hasEnteredPractice 跨会话/跨知识点泄漏——上一轮已进入
    // 练习后，新讲义落定（S3+advance）被 watcher 1s 自动推进，跳过讲义阅读。
    // 修复：resetSession 清标志 + 讲义消息 id 变化即重置——新讲义必须重新点击。
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const lectureA = {
      msg_id: 'lecture-a',
      agent: 'knowledge',
      payload: { type: 'lecture_note', content: { lecture_deferred: false } },
    }
    const lectureB = {
      msg_id: 'lecture-b',
      agent: 'knowledge',
      payload: { type: 'lecture_note', content: { lecture_deferred: false } },
    }
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      messages: [lectureA],
    }))
    vi.mocked(api.advance).mockResolvedValueOnce(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
      messages: [lectureA],
    }))
    vi.useFakeTimers()
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 100 } })
    await flushPromises()

    // 第一轮：点击进入练习（一次推进停在 S7+sql，画像一/二语义）
    await (wrapper.vm as unknown as { startPracticeChain: () => Promise<void> })
      .startPracticeChain()
    await flushPromises()
    expect(api.advance).toHaveBeenCalledTimes(1)

    // 新讲义落定（下一知识点/重开训练）：轮询带回 S3+advance + 新讲义消息
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S3_TASK',
      awaiting: 'advance',
      messages: [lectureB],
    }))
    await vi.advanceTimersByTimeAsync(250)
    await flushPromises()
    expect(wrapper.find('[data-testid="generation-pending"]').exists()).toBe(false)

    // watcher 窗口过后不自动推进：讲义阅读停驻，等待学员点击"进入练习"
    await vi.advanceTimersByTimeAsync(1600)
    await flushPromises()
    expect(api.advance).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('runs the data_present direct path through S7 proxy inside the pretest chain', async () => {
    // 0818 视频实录（前测全对直入实操）：直达链原只认 S2/S3/S9——画像三任务下发
    // 落 S7+advance 时链断裂、布局锁释放，闪现空白工作台中间页后靠 watcher 秒级
    // 续推。修复：直达链与练习链同状态机，S7 一并链内推进，一口气到追问。
    const api = fakeApi()
    const deferredLecture = {
      msg_id: 'lecture-deferred',
      agent: 'knowledge',
      payload: { type: 'lecture_note', content: { lecture_deferred: true } },
    }
    vi.mocked(api.submitPretest).mockResolvedValue(sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    }))
    vi.mocked(api.advance)
      .mockResolvedValueOnce(sessionState({
        state: 'S3_TASK',
        awaiting: 'advance',
        messages: [deferredLecture],
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'advance',
        messages: [deferredLecture],
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S9_PATH_UPDATE',
        awaiting: 'advance',
        messages: [deferredLecture],
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'follow_up',
        messages: [deferredLecture],
        interaction: {
          kind: 'free_text_follow_up',
          prompt: '对照计划量与实际完成量，哪一个说明真正做了多少？',
          round: 1,
          max_rounds: 4,
          turns: [],
        },
      }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()
    for (const [index, question] of questions.entries()) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
      if (index < questions.length - 1) {
        await wrapper.get('.pretest-page-actions .primary-action').trigger('click')
      }
    }
    await wrapper.get('button[type="submit"]').trigger('submit')
    await flushPromises()

    // 直达链一口气推进四步：讲义延期→任务下发(S7)→系统代执行(S9)→追问
    expect(api.advance).toHaveBeenCalledTimes(4)
    expect(wrapper.text()).not.toContain('继续训练')
    wrapper.unmount()
  })

  it('keeps one pending message through the direct chain without a next-question page', async () => {
    // 0818 实录：直达链 S9 结论生成阶段原会切成"正在为你准备下一问"，同一条
    // 等待被感知为额外中间页——链中（autoChainRunning）文案保持全程同句。
    const api = fakeApi()
    const deferredLecture = {
      msg_id: 'lecture-deferred',
      agent: 'knowledge',
      payload: { type: 'lecture_note', content: { lecture_deferred: true } },
    }
    vi.mocked(api.submitPretest).mockResolvedValue(sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    }))
    let releaseConclusion!: (value: InteractiveState) => void
    const conclusionGate = new Promise<InteractiveState>((resolve) => {
      releaseConclusion = resolve
    })
    vi.mocked(api.advance)
      .mockResolvedValueOnce(sessionState({
        state: 'S3_TASK',
        awaiting: 'advance',
        messages: [deferredLecture],
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S7_STUDENT',
        awaiting: 'advance',
        messages: [deferredLecture],
      }))
      .mockResolvedValueOnce(sessionState({
        state: 'S9_PATH_UPDATE',
        awaiting: 'advance',
        messages: [deferredLecture],
      }))
      .mockImplementationOnce(() => conclusionGate)
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await enterProfileSelectPage(wrapper)
    await chooseProfileAndStart(wrapper, '新入职生产计划员')
    await flushPromises()
    for (const [index, question] of questions.entries()) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
      if (index < questions.length - 1) {
        await wrapper.get('.pretest-page-actions .primary-action').trigger('click')
      }
    }
    await wrapper.get('button[type="submit"]').trigger('submit')
    await flushPromises()

    // 链卡在 S9 结论生成（第 4 次 advance 未返回）：等待文案仍是进入句，不切"下一问"
    const overlay = wrapper.get('[data-testid="generation-pending"]')
    expect(overlay.text()).toContain('正在为你准备练习题')
    expect(overlay.text()).not.toContain('下一问')

    releaseConclusion(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      messages: [deferredLecture],
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '对照计划量与实际完成量，哪一个说明真正做了多少？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    }))
    await flushPromises()
    expect(api.advance).toHaveBeenCalledTimes(4)
    wrapper.unmount()
  })

  it('shows the retry feedback in live mode when no turn was recorded', async () => {
    // 0818 实录：答"不知道"触发质量门保留——无 turn 记录、原题重出，
    // 评价兜底卡原先被 !operationOnly 屏蔽（live 恒 true），学员看不到任何反馈。
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '计划量和实际量分别表示什么，哪一个代表了应该完成的数量？',
        round: 1,
        max_rounds: 4,
        turns: [],
        feedback: '本轮新追问暂时未能通过质量检查，系统已保留上一道已审核题目；你可以结合查询结果重新作答，本次学习不会结束。',
        retry_required: true,
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, operationOnly: true },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('老师评价')
    expect(wrapper.text()).toContain('本轮新追问暂时未能通过质量检查')
    // 原题与输入区仍在
    expect(wrapper.text()).toContain('第 1 轮')
    expect(wrapper.find('textarea[aria-label="输入你的判断"]').exists()).toBe(true)
    wrapper.unmount()
  })

  it('keeps the answer history state purely manual across questions', async () => {
    // 0818 用户定稿：答题记录默认收起，展开/收起只随学员手点变化——
    // 答题/换题/轮询不得自动收起（此前的"换题自动收起"已撤销）。
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    const round1Turns = [{
      round: 1,
      question: '计划量和实际量分别表示什么？',
      answer: '不知道',
      feedback: '不会也没关系。跟着下一问的提示，先在表里找到对应的字段和值。',
      assessment: 'unknown',
    }]
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '查询结果中的1855.06对应计划量还是实际完成量？',
        round: 1,
        max_rounds: 4,
        turns: round1Turns,
      },
    }))
    vi.useFakeTimers()
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 100 } })
    await flushPromises()

    // 默认收起：只见切换钮，不见翻页记录
    expect(wrapper.find('.follow-up-history-pager').exists()).toBe(false)
    await wrapper.get('.follow-up-history-toggle').trigger('click')
    expect(wrapper.find('.follow-up-history-pager').exists()).toBe(true)

    // 轮询带回下一题（round 2）：展开态保持——状态不随答题/换题变化
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '下一题：1855.06 和 1156.87 哪个是实际完成量？',
        round: 2,
        max_rounds: 4,
        turns: [...round1Turns, {
          round: 2,
          question: '查询结果中的1855.06对应计划量还是实际完成量？',
          answer: '1855.06 对应计划量。',
          feedback: '回答有效：已通过本轮理解核对。',
          assessment: 'mastered',
        }],
      },
    }))
    await vi.advanceTimersByTimeAsync(250)
    await flushPromises()
    expect(wrapper.find('.follow-up-history-pager').exists()).toBe(true)

    // 仅手点收起才收起
    await wrapper.get('.follow-up-history-toggle').trigger('click')
    expect(wrapper.find('.follow-up-history-pager').exists()).toBe(false)
    wrapper.unmount()
  })

  it('holds the SQL page with the scaffold answer replacing the editor', async () => {
    // 定稿：五连错代执行后不跳提问页——右半边原输入框/按钮位换"标准答案与解析"，
    // 点"继续进入提问"才推进；追问页不再显示标答。
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'sql',
    }))
    const scaffoldResult: TraceMessage = {
      ...sqlResultMessage(),
      content: {
        ...sqlResultMessage().content,
        sql_source: 'system_proxy',
        scaffold: {
          standard_sql: 'SELECT plan_qty, actual_qty FROM fact_production_progress',
          analysis: '题目要求：按口径查询。\n查询需输出 计划量、实际完成量；\n对照上方查询结果逐列核对口径后，再回答提问。',
        },
      },
    }
    vi.mocked(api.submitSql).mockResolvedValue(sessionState({
      state: 'S9_PATH_UPDATE',
      awaiting: 'advance',
      artifact: {
        payload: {
          type: 'sql_result',
          content: { scaffold: { standard_sql: 'SELECT 1', analysis: '解析' } },
        },
      },
    }))
    vi.mocked(api.advance).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '对照查询结果，哪道工序完成率最低？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    }))
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0, sqlResult: scaffoldResult },
    })
    await flushPromises()

    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue('DELETE FROM x')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()

    // 停在 SQL 实操页：编辑器/运行按钮被标答覆盖，未自动进提问
    expect(api.advance).not.toHaveBeenCalled()
    expect(wrapper.find('textarea[aria-label="输入查询语句"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="运行查询"]').exists()).toBe(false)
    // 定稿：停留页删"查询结果"组与"查询练习"头行，标答置顶
    expect(wrapper.find('.sql-result-group').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('查询练习')
    const reveal = wrapper.get('[aria-label="标准答案与解析"]')
    expect(reveal.text()).toContain('标准答案与解析')
    expect(reveal.text()).toContain('SELECT plan_qty, actual_qty FROM fact_production_progress')
    expect(reveal.text()).toContain('查询需输出')

    // 点"继续进入提问"→ 进入提问页，标答不再出现
    await wrapper.get('button[aria-label="继续进入提问"]').trigger('click')
    await flushPromises()
    expect(api.advance).toHaveBeenCalled()
    expect(wrapper.text()).toContain('对照查询结果，哪道工序完成率最低？')
    expect(wrapper.find('[aria-label="标准答案与解析"]').exists()).toBe(false)
    wrapper.unmount()
  })

  it('styles the dont-know hint the same as the other guidance hints', async () => {
    // 0818：提示句样式统一——"没关系（不知道引导）"与其他引导句共用
    // needs-evidence 样式（同色同粗细）；字数计数器保持朴素样式。
    sessionStorage.setItem('ref-interactive-session', 'session-live')
    const api = fakeApi()
    vi.mocked(api.getState).mockResolvedValue(sessionState({
      state: 'S7_STUDENT',
      awaiting: 'follow_up',
      interaction: {
        kind: 'free_text_follow_up',
        prompt: '查询结果中的1855.06对应计划量还是实际完成量？',
        round: 1,
        max_rounds: 4,
        turns: [],
      },
    }))
    const wrapper = mount(LivePractice, { props: { api, pollIntervalMs: 0 } })
    await flushPromises()

    const hint = () => wrapper.get('.follow-up-actions small')

    // 输入"不知道"：引导句 + 黑色专属样式（非琥珀 needs-evidence）
    await wrapper.get('textarea[aria-label="输入你的判断"]').setValue('不知道')
    expect(hint().text()).toContain('没关系，直接提交也可以')
    expect(hint().classes()).toContain('dont-know-hint')
    expect(hint().classes()).not.toContain('needs-evidence')

    // 正常输入：字数计数器，无 needs-evidence 样式
    await wrapper.get('textarea[aria-label="输入你的判断"]').setValue('1855.06 是计划量，1156.87 是实际完成量')
    expect(hint().text()).toContain('/ 500 字')
    expect(hint().classes()).not.toContain('needs-evidence')
    wrapper.unmount()
  })
})
