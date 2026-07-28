import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type {
  InteractiveApi,
  InteractivePretestQuestion,
  InteractiveState,
} from '../lib/interactiveApi'
import { InteractiveApiError } from '../lib/interactiveApi'
import { isLearnerSafeText } from '../lib/tracePresentation'
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
    submitSql: vi.fn(async () => sessionState()),
    submitFollowUp: vi.fn(async () => sessionState()),
  }
}


describe('LivePractice', () => {
  afterEach(() => {
    sessionStorage.clear()
    vi.useRealTimers()
  })

  it('binds profile cards to approved human-facing profile data only', () => {
    const wrapper = mount(LivePractice, {
      props: { api: fakeApi(), pollIntervalMs: 0 },
    })

    expect(wrapper.text()).toContain('计算机/信息类背景校招生，会SQL和数据分析工具，不懂船舶工序与口径')
    expect(wrapper.text()).toContain('船舶工艺背景转数字化岗，精通预处理/托盘工艺，不会数据工具')
    expect(wrapper.text()).toContain('高职毕业一线班组长，现场熟，理论与数据双弱')
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

    expect(wrapper.findAll('fieldset.pretest-question')).toHaveLength(5)
    for (const question of questions) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
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
    expect(wrapper.get('.live-practice-heading h2').text()).toBe('实操')
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
    for (const question of questions) {
      await wrapper.get(`input[name="${question.question_id}"][value="B"]`).setValue(true)
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
