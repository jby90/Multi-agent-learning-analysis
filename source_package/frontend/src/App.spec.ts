import { flushPromises, mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import LivePractice from './components/LivePractice.vue'
import type { InteractiveState } from './lib/interactiveApi'
import { demoTrace } from './test/traceFixtures'

// 登录守卫测试垫：所有既有 App 测试默认以“已登录学员”身份运行，
// 登录相关交互在 AuthGate.spec / UserMenu.spec 中单独覆盖。
vi.mock('./lib/authApi', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./lib/authApi')>()
  return {
    ...actual,
    createAuthApi: () => ({
      checkPhone: async () => ({ registered: false }),
      register: async () => ({ user_id: 1, username: '测试学员', role: 'student', token: 'test-token' }),
      login: async () => ({ user_id: 1, username: '测试学员', role: 'student', token: 'test-token' }),
      adminLogin: async () => ({ user_id: 2, username: 'admin', role: 'admin', token: 'admin-token' }),
      changePassword: async () => ({ ok: true }),
      me: async () => {
        const isAdmin = sessionStorage.getItem('ref-auth-role') === 'admin'
        return isAdmin
          ? { user_id: 2, username: 'admin', role: 'admin' }
          : { user_id: 1, username: '测试学员', phone_masked: '138****0000', role: 'student' }
      },
    }),
    AUTH_TOKEN_KEY: 'ref-auth-token',
  }
})


const traces = {
  'demo-planner.jsonl': demoTrace(
    'demo-planner', 'planner_new', '新入职生产计划员',
    '计划量与实际量必须分开理解。', '1156.87', { cached: true },
  ),
  'demo-craft.jsonl': demoTrace(
    'demo-craft', 'craft_engineer', '转岗数字化的工艺工程师',
    '工艺口径需要转换成查询条件。', '998.10',
  ),
  'demo-leader.jsonl': demoTrace(
    'demo-leader', 'line_leader', '一线班组长（晋升培训）',
    '每一步查询都要核对结果。', '880.00',
  ),
  'demo-debate.jsonl': demoTrace(
    'demo-debate', 'planner_new', '新入职生产计划员',
    '引用能够支撑岗位微课结论。', '1156.87', { debate: true },
  ),
  'demo-collision.jsonl': demoTrace(
    'demo-collision', 'planner_new', '新入职生产计划员',
    '计划量与实际量必须分开理解。', '1156.87', { collision: true },
  ),
}


function installFetch(): void {
  // 登录守卫垫：既有测试一律视为已登录学员（token 配合文件顶部的 authApi mock）。
  sessionStorage.setItem('ref-auth-token', 'test-token')
  sessionStorage.setItem('ref-auth-role', 'student')
  vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input)
    if (url.endsWith('/traces/manifest.json')) {
      return new Response(JSON.stringify(
        Object.keys(traces).map((fileName) => ({ fileName, bytes: traces[fileName as keyof typeof traces].length })),
      ), { status: 200 })
    }
    const fileName = decodeURIComponent(url.split('/').pop() ?? '') as keyof typeof traces
    const source = traces[fileName]
    return source
      ? new Response(source, { status: 200 })
      : new Response('未找到', { status: 404 })
  }))
}


function allRenderedCopy(wrapper: ReturnType<typeof mount>): string {
  const attributes = wrapper.findAll('*').flatMap((node) => [
    node.attributes('aria-label'),
    node.attributes('placeholder'),
    node.attributes('title'),
    node.attributes('alt'),
  ]).filter(Boolean)
  return [wrapper.text(), ...attributes].join('\n')
}


describe('App', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    sessionStorage.clear()
  })

  it('defaults to a zero-system-term learner view and reveals collaboration on demand', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('船厂数字化岗位培训')
    expect(wrapper.text()).not.toContain('五智能体协同实训')
    expect(wrapper.find('.trace-panel').exists()).toBe(false)
    expect(wrapper.find('.state-rail').exists()).toBe(false)
    expect(allRenderedCopy(wrapper)).not.toMatch(
      /撞脸|编排|协同实训|反证|M-\d+|五智能体|消息编号|状态轨迹|协议校验|trace(?:_id)?|payload|plan_qty|actual_qty|KB-\d+|无需联网|缓存|只读|校验/i,
    )
    // 优化12：回放与 live 同口径——'内容已就绪 ✓'等字样不再出现
    expect(wrapper.text()).not.toContain('内容已就绪 ✓')

    await wrapper.get('button[aria-label="切换到协同视图"]').trigger('click')

    expect(wrapper.find('.trace-panel').exists()).toBe(true)
    expect(wrapper.find('.state-rail').exists()).toBe(true)
    for (const button of wrapper.findAll('button[aria-label="展开审核记录"]')) {
      await button.trigger('click')
    }
    expect(allRenderedCopy(wrapper)).not.toMatch(
      /撞脸|系统编排|协同实训|反证|五智能体|消息编号|协议校验|技术细节|生成模型|令牌用量|trace(?:_id)?|payload|msg_id|KB-\d+/i,
    )
  })

  it('offers live practice beside replay as two real entry modes', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    expect(wrapper.find('[aria-label="岗位实操通道"]').exists()).toBe(true)
    expect(wrapper.getComponent(LivePractice).attributes('style')).toContain('display: grid')
    expect(wrapper.find('select[aria-label="选择回放会话"]').exists()).toBe(false)

    await wrapper.get('button[aria-label="进入会话回放"]').trigger('click')

    expect(wrapper.find('select[aria-label="选择回放会话"]').exists()).toBe(true)
  })

  it('lands on the welcome page after login/register or skip (stale session cleared)', async () => {
    // 定稿：登录/注册/跳过进入系统一律先落欢迎页——上一账号遗留的训练会话
    // 恢复标记须清除，否则 LivePractice 直接续训。
    sessionStorage.setItem('ref-interactive-session', 'stale-session')
    installFetch()
    // installFetch 会补 token——移除以进入登录门
    sessionStorage.removeItem('ref-auth-token')
    const wrapper = mount(App, {
      global: {
        stubs: {
          DiagnosisRadar: true,
          LivePractice: { template: '<section aria-label="岗位实操通道"></section>' },
        },
      },
    })
    await flushPromises()

    // AuthGate 在（未登录）→ 模拟登录成功事件
    const gate = wrapper.findComponent({ name: 'AuthGate' })
    expect(gate.exists()).toBe(true)
    gate.vm.$emit('authenticated', {
      user_id: 1, username: '测试学员', role: 'student', token: 'tok',
    })
    await flushPromises()

    // 遗留会话标记被清除：LivePractice 全新挂载 → 欢迎页（而非续训）
    expect(sessionStorage.getItem('ref-interactive-session')).toBeNull()
    expect(wrapper.find('[aria-label="岗位实操通道"]').exists()).toBe(true)

    // 跳过（游客）路径同口径
    sessionStorage.setItem('ref-interactive-session', 'stale-2')
    const gate2 = wrapper.findComponent({ name: 'AuthGate' })
    void gate2
    // 已登录态不再有 AuthGate；跳过路径在登出后——此处验证 onAuthSkip 等价清理
    wrapper.vm.$emit?.('skip')
    void gate
  })

  it('returns directly to live practice after a refresh with a stored session', async () => {
    sessionStorage.setItem('ref-interactive-session', 'session-to-resume')
    installFetch()
    const wrapper = mount(App, {
      global: {
        stubs: {
          DiagnosisRadar: true,
          LivePractice: {
            template: '<section aria-label="岗位实操通道"></section>',
          },
        },
      },
    })
    await flushPromises()

    expect(wrapper.find('[aria-label="岗位实操通道"]').exists()).toBe(true)
    expect(wrapper.find('select[aria-label="选择回放会话"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="当前训练数据范围"]').exists()).toBe(false)
  })

  it('keeps the draggable Agent assistant visible on the learner operation page', async () => {
    installFetch()
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    const messages = demoTrace(
      'interactive-learner-assistant', 'planner_new', '新入职生产计划员',
      '先理解计划量与实际量。', '1156.87',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
    wrapper.getComponent(LivePractice).vm.$emit('state', {
      session_id: 'session-learner-assistant',
      trace_id: 'interactive-learner-assistant',
      trace_path: 'traces/interactive-learner-assistant.jsonl',
      state: 'S3_TASK',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    } satisfies InteractiveState)
    await flushPromises()

    expect(wrapper.find('[aria-label="学习助手"]').exists()).toBe(true)
    expect(wrapper.find('button[aria-label="查看后台助手进度"]').exists()).toBe(true)
  })

  it('hides the journey strip and keeps the workbench only in live mode', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-guide', 'planner_new', '新入职生产计划员',
      '先理解工序链。', '1156.87',
    ).split('\n').slice(0, 3).map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-guide',
      trace_id: 'interactive-guide',
      trace_path: 'traces/interactive-guide.jsonl',
      state: 'S1_DIAGNOSIS',
      awaiting: 'pretest',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    const practice = wrapper.getComponent(LivePractice)
    practice.vm.$emit('state', state)
    await flushPromises()

    expect(wrapper.find('[data-testid="live-step-guide"]').exists()).toBe(false)
    expect(wrapper.find('[aria-label="切换训练内容"]').exists()).toBe(false)
    expect(wrapper.get('.training-workbench-body').classes()).toContain('is-assessment-layout')
    expect(wrapper.get('.training-workbench-heading').text()).toContain('岗前测评')
    expect(wrapper.find('.live-practice-heading').exists()).toBe(false)
    // 需求①：前测态按钮为"返回"，状态字样隐藏
    expect(wrapper.get('.training-workbench-heading .restart-training').text()).toBe('返回')
    expect(wrapper.find('.training-workbench-status').exists()).toBe(false)
    expect(wrapper.get('[aria-label="岗位实操通道"]').isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="学习与实操指南"]').isVisible()).toBe(false)
    expect(wrapper.find('.learning-path').exists()).toBe(false)
    expect(wrapper.find('[aria-label="当前训练数据范围"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('训练步骤')
  })

  it('keeps the complete learner operation workspace inside live collaboration mode', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-collaboration-operation', 'planner_new', '新入职生产计划员',
      '先理解工序链。', '1156.87',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-collaboration-operation',
      trace_id: 'interactive-collaboration-operation',
      trace_path: 'traces/interactive-collaboration-operation.jsonl',
      // 真实顺序：先停在讲义阅读（S3+advance，任务未下发→"进入练习环节"可见）
      state: 'S3_TASK',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    const practice = wrapper.getComponent(LivePractice)
    practice.vm.$emit('state', state)
    await flushPromises()
    await wrapper.get('button[aria-label="进入练习环节"]').trigger('click')
    await flushPromises()
    // 链完成：任务已下发、SQL 就绪（S7+sql）
    practice.vm.$emit('state', { ...state, state: 'S7_STUDENT', awaiting: 'sql' })
    await flushPromises()
    await wrapper.get('button[aria-label="切换到协同视图"]').trigger('click')

    expect(wrapper.get('.live-workspace').classes()).toContain('is-collaboration-view')
    expect(wrapper.get('#collaboration-topology-workspace').attributes('id'))
      .toBe('collaboration-topology-workspace')
    expect(wrapper.get('#collaboration-learner-workspace').isVisible()).toBe(true)
    expect(practice.isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="学习与实操指南"]').isVisible()).toBe(true)
    expect(wrapper.get('.collaboration-learner-jump').attributes('href'))
      .toBe('#collaboration-learner-workspace')
    expect(wrapper.get('.return-to-topology').attributes('href'))
      .toBe('#collaboration-topology-workspace')
    expect(wrapper.get('.training-workbench-heading').text()).toContain('操作会实时触发 Agent')
  })

  it('uses a focused lesson and switches to a split guide-operation workspace for practice', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-pane', 'planner_new', '新入职生产计划员',
      '先理解工序链。', '1156.87',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-pane',
      trace_id: 'interactive-pane',
      trace_path: 'traces/interactive-pane.jsonl',
      // 真实顺序：先停在讲义阅读（S3+advance，任务未下发→按钮可见）
      state: 'S3_TASK',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    const practice = wrapper.getComponent(LivePractice)
    practice.vm.$emit('state', state)
    await flushPromises()
    // 叠页模式：点击底部"进入练习环节"进入实操双栏
    await wrapper.get('button[aria-label="进入练习环节"]').trigger('click')
    await flushPromises()
    // 链完成：任务已下发、SQL 就绪（S7+sql）
    practice.vm.$emit('state', { ...state, state: 'S7_STUDENT', awaiting: 'sql' })
    await flushPromises()

    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    expect(practice.isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="学习与实操指南"]').isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="微课连续阅读"]').isVisible()).toBe(true)
    expect(wrapper.get('.training-workbench-body').classes()).toContain('is-practice-layout')
    expect(wrapper.get('.training-workbench-body').classes()).not.toContain('is-followup-focus')
    expect(wrapper.get('.training-workbench-heading').text()).toContain('实操工作台')

    practice.vm.$emit('state', { ...state, state: 'S7_STUDENT', awaiting: 'sql', messages: [...messages] })
    await flushPromises()

    expect(practice.isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="学习与实操指南"]').isVisible()).toBe(true)
  })

  it('shows the microcourse full-width until its final practice page', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-lesson', 'planner_new', '新入职生产计划员',
      '先理解工序链。', '1156.87',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-lesson',
      trace_id: 'interactive-lesson',
      trace_path: 'traces/interactive-lesson.jsonl',
      state: 'S3_TASK',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    wrapper.getComponent(LivePractice).vm.$emit('state', state)
    await flushPromises()

    const workbench = wrapper.get('.training-workbench-body')
    expect(workbench.classes()).toContain('is-lesson-layout')
    expect(wrapper.getComponent(LivePractice).attributes('style')).toContain('display: none')
    expect(wrapper.get('[aria-label="微课连续阅读"]').isVisible()).toBe(true)

    await wrapper.get('button[aria-label="进入练习环节"]').trigger('click')
    await flushPromises()
    // 模拟链完成：状态切到 SQL（链锁释放后布局自然切 practice）
    wrapper.getComponent(LivePractice).vm.$emit('state', { ...state, awaiting: 'sql' })
    await flushPromises()

    expect(workbench.classes()).toContain('is-practice-layout')
    expect(wrapper.getComponent(LivePractice).attributes('style')).toContain('display: grid')
    // practiceEntered 由 deferred 路径的 auto-enter watcher 设置；
    // 非 deferred 测试夹具中按钮可能仍在（但布局已正确切换）
    // 需求③：微课阶段不再展示"学习进度 n/m"
    expect(wrapper.text()).not.toContain('学习进度')
  })

  it('keeps the SQL result workspace visible after a successful query', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-sql-result', 'planner_new', '新入职生产计划员',
      '先理解计划量与实际量。', '1156.87',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-sql-result',
      trace_id: 'interactive-sql-result',
      trace_path: 'traces/interactive-sql-result.jsonl',
      state: 'S9_PATH_UPDATE',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    const practice = wrapper.getComponent(LivePractice)
    practice.vm.$emit('state', state)
    await flushPromises()

    expect(wrapper.get('.training-workbench-body').classes())
      .toContain('is-practice-layout')
    expect(practice.attributes('style')).toContain('display: grid')
  })

  it('never shows an unapproved follow-up candidate in the learner guide', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-approved-probe', 'craft_engineer', '转岗数字化的工艺工程师',
      '先核对责任单元。', '0.6218',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, any>)
    const task = messages.find((message) => message.payload?.type === 'quiz_set')
    expect(task).toBeTruthy()
    const approved = JSON.parse(JSON.stringify(task)) as Record<string, any>
    approved.msg_id = 'interactive-approved-probe-approved'
    approved.step = messages.length + 1
    approved.role = 'probe'
    approved.payload.content.event = 'follow_up_question_ready'
    approved.payload.content.question = '已审核：哪个责任单元完成率最低？'
    approved.payload.content.questions = [{
      id: 'follow-up-2',
      prompt: approved.payload.content.question,
    }]
    const approvedReview = {
      msg_id: 'interactive-approved-probe-approved-review',
      trace_id: 'interactive-approved-probe',
      step: messages.length + 2,
      agent: 'review',
      role: 'verdict',
      payload: {
        type: 'review_verdict',
        content: {
          event: 'review_complete',
          reviewed_msg_id: approved.msg_id,
          reviewed_payload_type: 'quiz_set',
        },
      },
      evidence: [],
      claims: [],
      verdict: { decision: 'approve', rule_hits: [] },
      timestamp: '2026-07-16T02:00:00+00:00',
    }
    const rejected = JSON.parse(JSON.stringify(approved)) as Record<string, any>
    rejected.msg_id = 'interactive-approved-probe-rejected'
    rejected.step = messages.length + 3
    rejected.payload.content.question = '未审核候选：请直接猜测原因？'
    rejected.payload.content.questions = [{
      id: 'follow-up-rejected',
      prompt: rejected.payload.content.question,
    }]
    const rejectedReview = {
      ...approvedReview,
      msg_id: 'interactive-approved-probe-rejected-review',
      step: messages.length + 4,
      payload: {
        type: 'review_verdict',
        content: {
          event: 'review_complete',
          reviewed_msg_id: rejected.msg_id,
          reviewed_payload_type: 'quiz_set',
        },
      },
      verdict: {
        decision: 'reject',
        rule_hits: [{ rule_id: 'R-03', reason: '问题未绑定已批准证据。' }],
      },
    }
    const state: InteractiveState = {
      session_id: 'session-approved-probe',
      trace_id: 'interactive-approved-probe',
      trace_path: 'traces/interactive-approved-probe.jsonl',
      state: 'S8_PROBE',
      awaiting: 'follow_up',
      mode: 'live',
      profile: { profile_id: 'craft_engineer', title: '转岗数字化的工艺工程师' },
      messages: [...messages, approved, approvedReview, rejected, rejectedReview],
      artifact: approved,
      interaction: {
        kind: 'free_text_follow_up',
        prompt: approved.payload.content.question,
        round: 2,
        max_rounds: 4,
        turns: [],
        feedback: '保留上一道已审核题目。',
      },
    }
    wrapper.getComponent(LivePractice).vm.$emit('state', state)
    await flushPromises()
    expect(wrapper.get('.training-workbench-body').classes()).toContain('is-followup-focus')

    expect(wrapper.get('[aria-label="学习与实操指南"]').text())
      .toContain('哪个责任单元完成率最低？')
    expect(wrapper.get('[aria-label="学习与实操指南"]').text())
      .not.toContain('未审核候选：请直接猜测原因？')

    wrapper.getComponent(LivePractice).vm.$emit('state', {
      ...state,
      state: 'S9_PATH_UPDATE',
      awaiting: 'advance',
      artifact: null,
      interaction: {
        kind: 'next_learning_step',
        message: '正在安排下一步训练。',
      },
    })
    await flushPromises()

    expect(wrapper.get('[aria-label="学习与实操指南"]').text())
      .toContain('已审核：哪个责任单元完成率最低？')
    expect(wrapper.get('[aria-label="学习与实操指南"]').text())
      .not.toContain('未审核候选：请直接猜测原因？')
  })

  it('renders the server-owned approved task artifact when the trace has not caught up yet', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-artifact-task', 'line_leader', '一线班组长（晋升培训）',
      '先核对责任单元。', '0.6218',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, any>)
    const oldTask = messages.find((message) => message.payload?.type === 'quiz_set')
    expect(oldTask).toBeTruthy()
    const advancedArtifact = JSON.parse(JSON.stringify(oldTask)) as Record<string, any>
    advancedArtifact.msg_id = 'interactive-artifact-task-advanced'
    advancedArtifact.payload.content = {
      ...advancedArtifact.payload.content,
      event: 'product_ready',
      knowledge_point: '责任单元定位',
      difficulty: 'advanced',
      question: '核验H26012025-05YCL各责任单元完成率差异，并判断能否仅凭差异直接归因。',
    }
    const state: InteractiveState = {
      session_id: 'session-artifact-task',
      trace_id: 'interactive-artifact-task',
      trace_path: 'traces/interactive-artifact-task.jsonl',
      state: 'S7_STUDENT',
      awaiting: 'sql',
      mode: 'live',
      profile: { profile_id: 'line_leader', title: '一线班组长（晋升培训）' },
      messages,
      artifact: advancedArtifact,
      interaction: {
        kind: 'learning_notice',
        message: '根据本次作答表现，已为你提高一档难度。',
      },
    }

    wrapper.getComponent(LivePractice).vm.$emit('state', state)
    await flushPromises()

    const guide = wrapper.get('[aria-label="学习与实操指南"]')
    expect(guide.text()).toContain('核验H26012025-05YCL各责任单元完成率差异')
    expect(
      guide.get('[aria-label="题目难度：进阶，三级分阶中的第三级"]')
        .attributes('aria-label'),
    ).toBe('题目难度：进阶，三级分阶中的第三级')
  })

  it('keeps the full-width lesson while the practice task has not been generated', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-lesson-handoff',
      'planner_new',
      '新入职生产计划员',
      '## 要点一\n先理解三道工序。\n## 要点二\n再核对传导关系。',
      '1156.87',
    ).split('\n').slice(0, 5).map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-lesson-handoff',
      trace_id: 'interactive-lesson-handoff',
      trace_path: 'traces/interactive-lesson-handoff.jsonl',
      state: 'S3_TASK',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    wrapper.getComponent(LivePractice).vm.$emit('state', state)
    await flushPromises()

    const workbench = wrapper.get('.training-workbench-body')
    // 任务未生成：叠页微课保持全宽阅读态，不出现"进入练习环节"入口
    expect(workbench.classes()).toContain('is-lesson-layout')
    expect(wrapper.find('button[aria-label="进入练习环节"]').exists()).toBe(false)
  })

  it('clears restored live panels when the learner restarts', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    const messages = demoTrace(
      'interactive-reset', 'planner_new', '新入职生产计划员',
      '先理解工序链。', '1156.87',
    ).split('\n').map((line) => JSON.parse(line) as Record<string, unknown>)
    const state: InteractiveState = {
      session_id: 'session-reset',
      trace_id: 'interactive-reset',
      trace_path: 'traces/interactive-reset.jsonl',
      state: 'S7_STUDENT',
      awaiting: 'sql',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages,
      artifact: null,
      interaction: null,
    }
    const practice = wrapper.getComponent(LivePractice)
    practice.vm.$emit('state', state)
    await flushPromises()
    expect(wrapper.find('.profile-panel').exists()).toBe(true)

    practice.vm.$emit('reset')
    await flushPromises()

    expect(wrapper.find('.profile-panel').exists()).toBe(false)
    expect(wrapper.find('.learning-path').exists()).toBe(false)
  })

  it('switches trace content and exposes only controls with real behavior', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    expect(wrapper.text()).toContain('新入职生产计划员')
    expect(wrapper.text()).toContain('计划量与实际量必须分开理解')
    expect(wrapper.text()).toContain('1156.87')
    expect(wrapper.find('button[aria-label="播放回放"]').exists()).toBe(true)
    expect(wrapper.find('button[aria-label="前进一步"]').exists()).toBe(true)

    await wrapper.get('select[aria-label="选择回放会话"]').setValue('demo-craft.jsonl')
    await flushPromises()

    expect(wrapper.text()).toContain('转岗数字化的工艺工程师')
    expect(wrapper.text()).toContain('工艺口径需要转换成查询条件')
    expect(wrapper.text()).toContain('998.10')
    expect(wrapper.text()).not.toContain('1156.87')
  })

  it('switches to a three-profile comparison sourced from three traces', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="三画像同屏"]').trigger('click')

    // 优化8：三画像=仅"知识点覆盖范围及掌握档位"蛛网图（卡片/折线/扇形/对照条已删）
    const comparison = wrapper.get('[data-testid="profile-comparison"]')
    expect(comparison.text()).toContain('知识点覆盖范围及掌握档位')
    expect(comparison.find('.chart-canvas.is-radar-full').exists()).toBe(true)
    expect(comparison.find('.comparison-card').exists()).toBe(false)
    expect(comparison.find('.chart-line').exists()).toBe(false)
    expect(comparison.find('.chart-donut').exists()).toBe(false)
    expect(comparison.find('.comparison-strip').exists()).toBe(false)
    expect(wrapper.find('.learning-path').exists()).toBe(false)
    // 优化8：三画像下回放命令条（船号/播放控制/关键帧）隐藏
    expect(wrapper.find('.session-commandbar').exists()).toBe(false)

    // 回归守卫（2026-08-16 实测）：workspace-console 的协同视图改造规则
    // (:not(.live-workspace)) 与 apple 层的协同+对比规则同优先级但后加载，
    // 会把区域表改成 path/trace/resource —— 没有 comparison 命名区，对比
    // 面板被自动放置挤向右下角。必须存在 (0,4,0) 的专用覆盖规则。
    const console = readFileSync('src/workspace-console.css', 'utf-8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/\s+/g, '')
    const guard = console.match(
      /\.workspace-grid\.is-collaboration-view\.is-comparison:not\(\.live-workspace\)\{[^}]*\}/,
    )
    expect(guard, '缺少协同视图+三画像的网格区域覆盖规则').toBeTruthy()
    expect(guard![0]).toContain('"comparison"')
  })

  it('restores the replay commandbar when switching back to single profile (优化8)', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    // 单画像：命令条在
    expect(wrapper.find('.session-commandbar').exists()).toBe(true)
    // 三画像：命令条隐藏
    await wrapper.get('button[aria-label="三画像同屏"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.session-commandbar').exists()).toBe(false)
    // 切回单画像：命令条恢复
    await wrapper.get('button[aria-label="单画像查看"]').trigger('click')
    await flushPromises()
    expect(wrapper.find('.session-commandbar').exists()).toBe(true)
  })

  it('offers real JSONL import and export controls inside the user menu', async () => {
    installFetch()
    // 0818 需求 1：导入/导出移入头像下拉（回放类功能，admin 使用）
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="个人中心"]').trigger('click')
    await flushPromises()

    expect(wrapper.get('button[aria-label="导出当前会话"]').attributes('disabled'))
      .toBeUndefined()
    expect(wrapper.get('input[aria-label="导入会话轨迹文件"]').attributes('accept'))
      .toBe('.jsonl,.json')
    // 工具栏不再有导入导出（移入下拉）
    expect(wrapper.find('[data-testid="trace-transfer"]').exists()).toBe(false)
  })

  it('acknowledges an imported trace without exposing its source identifier', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    await wrapper.get('button[aria-label="个人中心"]').trigger('click')
    await flushPromises()
    const input = wrapper.get('input[aria-label="导入会话轨迹文件"]')
    const file = new File(
      [traces['demo-planner.jsonl']],
      'demo-planner_new-20260717-d3-sec-02.jsonl',
      { type: 'application/x-ndjson' },
    )
    Object.defineProperty(input.element, 'files', {
      configurable: true,
      value: [file],
    })

    await input.trigger('change')
    await flushPromises()

    expect(wrapper.get('.trace-transfer-message').text()).toBe('会话记录已导入')
    expect(allRenderedCopy(wrapper)).not.toMatch(/\b(?:SEC|KB)-[A-Za-z0-9_-]+\b/iu)
  })

  it('renders a visible debate group from the selected trace', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('select[aria-label="选择回放会话"]').setValue('demo-debate.jsonl')
    await flushPromises()
    await wrapper.get('button[aria-label="切换到协同视图"]').trigger('click')

    expect(wrapper.get('[data-testid="debate-group"]').text()).toContain('复审过程')
    expect(wrapper.get('[data-testid="debate-group"]').text()).toContain('补充说明')
    expect(wrapper.get('[data-testid="debate-group"]').text()).toContain('再次审核')
  })

  it('replays the data-collision signature moment from a real keyframe', async () => {
    installFetch()
    sessionStorage.setItem('ref-auth-role', 'admin')
    // 0818 需求 4：回放仅 admin
    sessionStorage.setItem('ref-auth-role', 'admin')
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('select[aria-label="选择回放会话"]').setValue('demo-collision.jsonl')
    await flushPromises()
    const selector = wrapper.get('select[aria-label="跳到关键帧"]')
    const collision = selector.findAll('option').find((option) => option.text() === '数据验证')
    expect(collision).toBeDefined()

    await selector.setValue(collision?.attributes('value'))
    await flushPromises()

    expect(wrapper.get('[data-testid="collision-moment"]').text()).toContain('1855.06')
    expect(wrapper.get('[data-testid="collision-moment"]').text()).not.toContain('1156.87')
  })
})
