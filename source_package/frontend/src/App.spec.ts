import { flushPromises, mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import App from './App.vue'
import LivePractice from './components/LivePractice.vue'
import type { InteractiveState } from './lib/interactiveApi'
import { demoTrace } from './test/traceFixtures'


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
    expect(wrapper.text()).toContain('已通过专业审核')

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
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="进入实操通道"]').trigger('click')

    expect(wrapper.find('[aria-label="岗位实操通道"]').exists()).toBe(true)
    expect(wrapper.find('select[aria-label="选择回放会话"]').exists()).toBe(false)

    await wrapper.get('button[aria-label="进入会话回放"]').trigger('click')

    expect(wrapper.find('select[aria-label="选择回放会话"]').exists()).toBe(true)
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
    expect(wrapper.text()).toContain('实时实操')
  })

  it('uses the cultivation path as the only journey navigation', async () => {
    installFetch()
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
    expect(wrapper.get('.training-workbench-body')).toBeTruthy()
    expect(wrapper.get('[aria-label="常驻微课"]').text()).toContain('完成岗前诊断后生成')
    expect(wrapper.get('.learning-path').text()).toContain('培养路径')
    expect(wrapper.text()).not.toContain('训练步骤')
  })

  it('keeps the current task and paged lesson visible in one persistent workbench', async () => {
    installFetch()
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

    expect(wrapper.find('[role="tablist"]').exists()).toBe(false)
    expect(practice.isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="常驻微课"]').isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="微课分页阅读"]').isVisible()).toBe(true)
    expect(wrapper.get('.training-workbench-heading').text()).toContain('任务与微课同步工作台')

    practice.vm.$emit('state', { ...state, messages: [...messages] })
    await flushPromises()

    expect(practice.isVisible()).toBe(true)
    expect(wrapper.get('[aria-label="常驻微课"]').isVisible()).toBe(true)
  })

  it('clears restored live panels when the learner restarts', async () => {
    installFetch()
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
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    await wrapper.get('button[aria-label="三画像同屏"]').trigger('click')

    const comparison = wrapper.get('[data-testid="profile-comparison"]')
    expect(comparison.text()).toContain('新入职生产计划员')
    expect(comparison.text()).toContain('转岗数字化的工艺工程师')
    expect(comparison.text()).toContain('一线班组长（晋升培训）')
    expect(comparison.findAll('.comparison-card')).toHaveLength(3)
    expect(comparison.findAll('.comparison-next-point strong').map((item) => item.text()))
      .toEqual(['完成率计算', '月度聚合方法', '异常识别标准'])
    expect(wrapper.find('.learning-path').exists()).toBe(false)
  })

  it('offers real JSONL import and export controls in replay mode', async () => {
    installFetch()
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()

    expect(wrapper.get('button[aria-label="导出当前会话"]').attributes('disabled'))
      .toBeUndefined()
    expect(wrapper.get('input[aria-label="导入会话记录"]').attributes('accept'))
      .toBe('.jsonl,application/x-ndjson,application/json')
    expect(wrapper.get('[data-testid="trace-transfer"]').attributes('aria-label'))
      .toBe('导入或导出会话记录')
  })

  it('acknowledges an imported trace without exposing its source identifier', async () => {
    installFetch()
    const wrapper = mount(App, {
      global: { stubs: { DiagnosisRadar: true } },
    })
    await flushPromises()
    const input = wrapper.get('input[aria-label="导入会话记录"]')
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
