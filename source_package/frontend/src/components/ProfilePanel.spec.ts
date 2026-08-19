/// <reference types="node" />

import { readFileSync } from 'node:fs'
import path from 'node:path'

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import catalog from 'virtual:knowledge-catalog'
import { buildTraceView } from '../lib/traceModel'
import { parseTraceJsonl } from '../lib/traceParser'
import type { TraceView } from '../types/trace'
import ProfilePanel from './ProfilePanel.vue'


const traceDirectory = path.resolve(process.cwd(), 'src', 'test', 'fixtures', 'traces')

function officialView(fileName: string): TraceView {
  const source = readFileSync(path.join(traceDirectory, fileName), 'utf8')
  const document = parseTraceJsonl(source, fileName)
  return buildTraceView(document, document.messages.length)
}

function mountPanel(view: TraceView) {
  return mount(ProfilePanel, {
    props: { view, catalog },
    global: { stubs: { DiagnosisRadar: true } },
  })
}


describe('ProfilePanel learning evidence', () => {
  it('filters the radar dimensions down to catalog knowledge points', () => {
    const view = officialView('demo-planner_new-20260716133542.jsonl')
    view.knowledgeDimensions = [
      ...catalog.map((entry) => entry.knowledgePoint),
      '年度统计总览及计划实际分布',
      '准时率字段口径',
    ]

    const wrapper = mountPanel(view)
    const radar = wrapper.findComponent({ name: 'DiagnosisRadar' })

    expect(radar.props('dimensions')).toHaveLength(catalog.length)
    expect(radar.props('dimensions')).not.toContain('年度统计总览及计划实际分布')
    expect(radar.props('dimensions')).not.toContain('准时率字段口径')
  })

  it('uses the authoritative live difficulty instead of the initial diagnosis', () => {
    const view = officialView('demo-planner_new-20260716133542.jsonl')
    const wrapper = mount(ProfilePanel, {
      props: { view, catalog, currentDifficulty: 'applied' },
      global: { stubs: { DiagnosisRadar: true } },
    })

    expect(wrapper.get('.assessment-summary').text()).toContain('当前档位应用档')
  })

  it('lists diagnosed points in the expanded profile without header or match panel', () => {
    const wrapper = mountPanel(officialView('demo-planner_new-20260716133542.jsonl'))

    // 需求⑤：删除"岗位画像"标题行；需求⑦：资源匹配板块已删除
    expect(wrapper.find('.panel-heading').exists()).toBe(false)
    expect(wrapper.find('[data-testid="resource-match"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('岗位与岗前测评')
    expect(wrapper.text()).not.toMatch(/KB-\d+|检索|命中/)
    expect(wrapper.text()).toContain('三道工序与传导关系')
    expect(wrapper.get('.profile-detail-content').attributes()).toMatchObject({
      role: 'region',
      'aria-label': '完整学习画像内容',
      tabindex: '0',
    })
  })

  it('exposes the actual difficulty journey and marks the mid-session upgrade frame', () => {
    const wrapper = mountPanel(officialView('demo-craft_engineer-20260716133542.jsonl'))

    const journey = wrapper.get('[data-testid="difficulty-journey"]')
    expect(journey.get('header strong').text()).toBe('难度轨迹')
    expect(journey.text()).not.toContain('学习进度与难度变化')
    expect(journey.attributes('aria-label')).toContain(
      '测评应用，微课进阶，实操进阶，验证进阶，进阶进阶',
    )
    expect(journey.text()).toContain('当前学习内容')
    expect(journey.get('.journey-status-note').text()).toBe('当前难度保持')

    // 中段「进阶训练」帧：T19 升档 (applied → advanced) 显式标注提升。
    // 进阶微课与任务作为伴随资源先于 T19 生成，帧内资源已在进阶档。
    const fileName = 'demo-craft_engineer-20260716133542.jsonl'
    const source = readFileSync(path.join(traceDirectory, fileName), 'utf8')
    const document = parseTraceJsonl(source, fileName)
    const stepUpPath = document.messages.find(
      (message) => message.payloadType === 'learning_path_update'
        && message.content.difficulty_action === 'step_up',
    )
    expect(stepUpPath).toBeDefined()
    const upgradeFrame = buildTraceView(document, stepUpPath?.step ?? 0)
    const upgradeJourney = mountPanel(upgradeFrame).get('[data-testid="difficulty-journey"]')
    expect(upgradeJourney.attributes('aria-label')).toContain('测评应用，微课进阶，实操进阶，验证进阶，进阶进阶')
    expect(upgradeJourney.text()).toContain('提升')
    expect(upgradeJourney.get('.journey-status-note').text()).toBe('难度已提升')
  })

  it.each([
    ['keep', '当前难度保持'],
    ['step_down', '已完成补充讲解'],
  ])('keeps the %s adjustment status inside the difficulty journey', (action, label) => {
    const view = officialView('demo-craft_engineer-20260716133542.jsonl')
    if (!view.path) throw new Error('fixture path missing')
    view.path.content.difficulty_action = action

    const journey = mountPanel(view).get('[data-testid="difficulty-journey"]')

    expect(journey.get('.journey-status-note').text()).toBe(label)
  })

  it('keeps diagnosis routing fields out of every profile display path', () => {
    const view = officialView('demo-planner_new-20260716133542.jsonl')
    if (!view.diagnosis || !view.lecture || !view.task) {
      throw new Error('fixture learning path missing')
    }
    view.diagnosis.content.blind_spots = ['route']
    view.knowledgeDimensions = ['route']
    view.lecture.content.knowledge_point = 'route'
    view.lecture.content.coverage = ['route']
    view.task.content.knowledge_point = 'route'

    const wrapper = mountPanel(view)
    const publicCopy = [
      wrapper.text(),
      ...wrapper.findAll('[aria-label]').map((node) => node.attributes('aria-label') ?? ''),
      wrapper.get('diagnosis-radar-stub').attributes('dimensions') ?? '',
    ].join(' ')

    expect(publicCopy).toContain('当前内容暂时无法展示，请稍后再试。')
    expect(publicCopy).not.toMatch(/\broute\b/iu)
  })

  it('shows the v4 learning plan with current and pending points (闭环四后继)', () => {
    const view = officialView('demo-planner_new-20260716133542.jsonl')
    // 注入 v4 形态的 knowledge_point_plan（真实 trace 为 v3，这里构造 v4 计划）
    const patched = {
      ...view,
      diagnosis: {
        ...view.diagnosis!,
        content: {
          ...view.diagnosis!.content,
          knowledge_point_plan: [
            { knowledge_point: '三道工序与传导关系', tier: 'correct', mastery_status: 'pending_training' },
            { knowledge_point: '计划量与实际量口径', tier: 'wrong', mastery_status: 'needs_training' },
            { knowledge_point: '传导时滞分析', tier: 'prerequisite_lift', mastery_status: 'needs_training' },
          ],
          pretest_score: { correct: 6, total: 6, rate: 1 },
        },
      },
    } as unknown as TraceView
    const wrapper = mount(ProfilePanel, {
      props: { view: patched, catalog, profileId: 'planner_new' },
      global: { stubs: { DiagnosisRadar: true } },
    })
    const items = wrapper.findAll('.learning-plan-list li')
    expect(items).toHaveLength(3)
    expect(items[0].classes()).toContain('is-current')
    expect(items[0].text()).toContain('三道工序与传导关系')
    expect(items[0].text()).toContain('进行中')
    expect(items[1].text()).toContain('待学习')
    expect(items[2].text()).toContain('补前置')
    // v4 计划存在时不显示盲区标题
    expect(wrapper.text()).not.toContain('知识盲区')
    wrapper.unmount()
  })

  it('falls back to blind spots when no v4 plan (v3/回放)', () => {
    const view = officialView('demo-planner_new-20260716133542.jsonl')
    const wrapper = mount(ProfilePanel, {
      props: { view, catalog },
      global: { stubs: { DiagnosisRadar: true } },
    })
    expect(wrapper.find('.learning-plan-list').exists()).toBe(false)
    expect(wrapper.text()).toContain('知识盲区')
    wrapper.unmount()
  })

  it('radar dimensions scope to the persona domain when profileId is set', () => {
    const view = officialView('demo-planner_new-20260716133542.jsonl')
    const wrapper = mount(ProfilePanel, {
      props: { view, catalog, profileId: 'planner_new' },
      global: { stubs: { DiagnosisRadar: true } },
    })
    const passed = (wrapper.findComponent({ name: 'DiagnosisRadar' }).props() as
      { dimensions?: string[] }).dimensions ?? []
    const scope = ['三道工序与传导关系', '计划量与实际量口径', '传导时滞分析', '异常衰减规律', '责任单元定位', '跨工序归因方法']
    expect(passed.length).toBeLessThanOrEqual(scope.length)
    for (const dim of passed) {
      expect(scope.some((point) => dim.includes(point.slice(0, 4)) || point.includes(dim.slice(0, 4)))).toBe(true)
    }
    wrapper.unmount()
  })
})
