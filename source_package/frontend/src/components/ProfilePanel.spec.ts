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


const traceDirectory = path.resolve(process.cwd(), '..', 'traces')

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
  it('shows exact blind-spot coverage with links to the matched resources', () => {
    const wrapper = mountPanel(officialView('demo-planner_new-20260716133542.jsonl'))

    expect(wrapper.get('.panel-heading h2').text()).toBe('岗位画像')
    expect(wrapper.text()).not.toContain('岗位与岗前测评')
    const match = wrapper.get('[data-testid="resource-match"]')
    expect(match.text()).toContain('5项盲区，本次资源覆盖2项')
    expect(match.text()).toContain('完成率计算')
    expect(match.text()).toContain('尚未覆盖')
    expect(match.findAll('a').map((link) => link.attributes('href'))).toEqual(
      expect.arrayContaining(['#lecture-resource', '#task-resource']),
    )
    expect(match.text()).not.toMatch(/KB-\d+|检索|命中/)
    expect(wrapper.get('.profile-detail-content').attributes()).toMatchObject({
      role: 'region',
      'aria-label': '完整学习画像内容',
      tabindex: '0',
    })
  })

  it('exposes the actual five-stage difficulty journey and adjustment actions', () => {
    const wrapper = mountPanel(officialView('demo-craft_engineer-20260716133542.jsonl'))

    const journey = wrapper.get('[data-testid="difficulty-journey"]')
    expect(journey.get('header strong').text()).toBe('难度轨迹')
    expect(journey.text()).not.toContain('学习进度与难度变化')
    expect(journey.attributes('aria-label')).toContain(
      '测评应用，微课基础，实操基础，验证应用，进阶进阶',
    )
    expect(journey.text()).toContain('提升')
    expect(journey.text()).toContain('当前资源')
    expect(journey.get('.journey-status-note').text()).toBe('难度已提升')
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
})
