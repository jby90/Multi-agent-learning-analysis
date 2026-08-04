import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  InteractiveApi,
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
    submitPretest: vi.fn(async () => sessionState({
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


describe('LivePractice', () => {
  afterEach(() => {
    sessionStorage.clear()
    vi.useRealTimers()
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
            output_columns: ['process_code', 'completion_rate'],
            filter_columns: ['ship_no', 'period_date'],
            group_by_columns: ['process_code'],
            time_values: ['2025-05'],
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

    const support = wrapper.get('.sql-progressive-support')
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
    expect(wrapper.get('.training-report-metrics').text()).toContain('岗前评测正确')
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

    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('.task-inline-result').text()).toContain('按工序查询完成率')
    expect(wrapper.get('.task-inline-result').text()).toContain('YCL')
    expect(wrapper.classes()).toContain('has-inline-result')
  })

  it('binds profile cards to approved human-facing profile data only', () => {
    const wrapper = mount(LivePractice, {
      props: { api: fakeApi(), pollIntervalMs: 0 },
    })

    expect(wrapper.get('#profile-picker-title').text())
      .toBe('从你的岗位出发，建立真正用得上的数字化能力')
    expect(wrapper.findAll('.profile-choice')).toHaveLength(3)
    expect(wrapper.text()).toContain('计算机/信息类背景校招生，会SQL和数据分析工具，不懂船舶工序与口径')
    expect(wrapper.text()).toContain('船舶工艺背景转数字化岗，精通预处理/托盘工艺，不会数据工具')
    expect(wrapper.text()).toContain('高职毕业一线班组长，现场熟，理论与数据双弱')
    expect(wrapper.text()).toContain('SQL基础')
    expect(wrapper.text()).toContain('现场生产经验')
    expect(wrapper.text()).not.toContain('重讲工序与口径、少讲SQL')
    expect(wrapper.text()).not.toContain('步骤化短句、每步带检查点')
    expect(wrapper.text()).not.toContain('重点补足')
  })

  it('collects all five learner choices before submitting the real pretest', async () => {
    const api = fakeApi()
    const wrapper = mount(LivePractice, {
      props: { api, pollIntervalMs: 0 },
    })

    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
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
    expect(wrapper.text()).toContain('打开岗位微课')
    expect(wrapper.get('.live-practice-heading h2').text()).toBe('微课准备')
    expect(wrapper.find('.live-practice-heading p').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('每一步由你亲自完成')
    expect(wrapper.emitted('state')?.at(-1)?.[0]).toMatchObject({
      state: 'S2_KNOWLEDGE',
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

    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
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

    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
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
    expect(wrapper.text()).toContain('领取实操任务')
  })

  it('walks the learner through SQL and a reviewed free-text correction', async () => {
    const api = fakeApi()
    vi.mocked(api.submitPretest).mockResolvedValue(sessionState({
      state: 'S2_KNOWLEDGE',
      awaiting: 'advance',
    }))
    vi.mocked(api.advance)
      .mockResolvedValueOnce(sessionState({ state: 'S3_TASK', awaiting: 'advance' }))
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
    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
    await flushPromises()
    for (const [index, question] of questions.entries()) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
      if (index < questions.length - 1) {
        await wrapper.get('.pretest-page-actions .primary-action').trigger('click')
      }
    }
    await wrapper.get('button[type="submit"]').trigger('submit')
    await flushPromises()

    await wrapper.get('button[aria-label="打开岗位微课"]').trigger('click')
    await flushPromises()
    await wrapper.get('button[aria-label="领取实操任务"]').trigger('click')
    await flushPromises()
    await wrapper.get('textarea[aria-label="输入查询语句"]').setValue('SELECT plan_qty FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()
    await wrapper.get('button[aria-label="判断查询结论"]').trigger('click')
    await flushPromises()
    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('计划量就是已经完成的数量。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()
    expect(wrapper.text()).toContain('第 2 / 最多 4 轮')
    expect(wrapper.text()).toContain('你的回答')
    await wrapper.get('textarea[aria-label="输入你的判断"]')
      .setValue('实际完成量才表示真正做了多少。')
    await wrapper.get('button[aria-label="提交本轮判断"]').trigger('click')
    await flushPromises()
    await wrapper.get('button[aria-label="查看下一步训练"]').trigger('click')
    await flushPromises()
    expect(wrapper.get('[data-testid="learning-notice"]').text())
      .toBe('根据本次作答表现，已为你提高一档难度。')
    await wrapper.get('textarea[aria-label="输入查询语句"]')
      .setValue('SELECT workshop_code, complete_rate FROM fact_production_progress')
    await wrapper.get('button[aria-label="运行查询"]').trigger('click')
    await flushPromises()
    await wrapper.get('button[aria-label="完成本次训练"]').trigger('click')
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

    expect(wrapper.get('button[aria-label="查看下一步训练"]').text())
      .toBe('查看下一步训练')
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

    await wrapper.get('button[aria-label="重新开始训练"]').trigger('click')

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

    const rejection = wrapper.get('[data-testid="sandbox-rejection"]')
    expect(rejection.get('strong').text()).toBe('只能做数据查询 · 查询被拦下')
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

    expect(wrapper.get('[data-testid="sandbox-rejection"] strong').text())
      .toBe('安全规则 · 查询被拦下')
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

    expect(wrapper.get('[data-testid="query-feedback"]').text())
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

    expect(wrapper.get('[data-testid="query-feedback"]').text())
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

    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
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
      expected: '本题的查询未通过数据安全检查，请调整后重试。',
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

    await wrapper.get('button[aria-label="选择新入职生产计划员"]').trigger('click')
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
      .toContain('不能只回答“是/否”')
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
    expect(wrapper.findAll('.follow-up-history li')).toHaveLength(2)
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

    expect(wrapper.get('.follow-up-task-anchor').text())
      .toContain('查询2025年5月至7月三道工序月完成率')
    expect(wrapper.get('.follow-up-task-anchor').text())
      .not.toContain('题目中的问题')
    expect(wrapper.get('.follow-up-current').text())
      .toContain('三道工序的最低完成率分别出现在哪个月')
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
    expect(wrapper.text()).toContain('第 2 / 最多 4 轮')
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
    expect(wrapper.get('[role="status"]').text())
      .toBe('正在根据你的回答准备并检查下一步提示…')

    finishSubmission(active)
    await submitting
    await flushPromises()
  })
})
