/// <reference types="node" />

import { readFileSync } from 'node:fs'
import path from 'node:path'

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import catalog from 'virtual:knowledge-catalog'
import { buildTraceView } from '../lib/traceModel'
import { parseTraceJsonl } from '../lib/traceParser'
import { contextualizedTaskStem, firstLectureGoal } from '../lib/tracePresentation'
import type { TraceDocument } from '../types/trace'
import ProfileComparison, { type ComparisonEntry } from './ProfileComparison.vue'


function entry(fileName: string): ComparisonEntry {
  const source = readFileSync(path.resolve(process.cwd(), '..', 'traces', fileName), 'utf8')
  const document: TraceDocument = parseTraceJsonl(source, fileName)
  return { document, view: buildTraceView(document, document.messages.length) }
}


function contextualizedEntry(
  fileName: string,
  contextualizedStem: string | undefined,
  lectureGoal: string,
): ComparisonEntry {
  const value = structuredClone(entry(fileName))
  if (value.view.profile) {
    value.view.profile.lecture_style = '这是给模型的指令，界面不得显示'
  }
  if (value.view.lecture) {
    value.view.lecture.content.lecture_md = [
      '### 岗位微课',
      '',
      '#### 本节目标',
      `- ${lectureGoal}`,
      '- 第二条目标不应作为摘要',
      '',
      '#### 核心概念',
      '正文不应被硬切为摘要。',
    ].join('\n')
  }
  const task = value.view.visibleMessages.find((message) => (
    message.agent === 'task'
    && message.role === 'produce'
    && message.content.event === 'product_ready'
    && ['quiz_set', 'practice_guide'].includes(message.payloadType)
  ))
  if (!task) throw new Error('fixture has no normal task product')
  task.content.standard_stem = '三个岗位完全相同的标准题面'
  if (contextualizedStem === undefined) {
    delete task.content.contextualized_stem
  } else {
    task.content.contextualized_stem = contextualizedStem
  }
  return value
}


describe('ProfileComparison', () => {
  it('shows distinct lecture points and future plans for all three roles', () => {
    const entries = [
      entry('demo-planner_new-20260716133542.jsonl'),
      entry('demo-craft_engineer-20260716133542.jsonl'),
      entry('demo-line_leader-20260716133542.jsonl'),
    ]
    const wrapper = mount(ProfileComparison, {
      props: { entries, catalog },
    })

    expect(wrapper.get('.comparison-heading h2').text()).toBe('岗位对比')
    expect(wrapper.text()).not.toContain('同一训练主线，不同岗位适配')
    expect(wrapper.findAll('.comparison-lecture-point').map((item) => item.text()))
      .toEqual(['三道工序与传导关系', '完成率计算', '计划量与实际量口径'])
    expect(wrapper.findAll('.comparison-next-point strong').map((item) => item.text()))
      .toEqual(['完成率计算', '月度聚合方法', '异常识别标准'])
    expect(wrapper.findAll('.comparison-next-point p')).toHaveLength(0)
    expect(wrapper.text()).not.toContain('继续补齐尚未覆盖的知识盲区')
  })

  it('matches every displayed role difference to the regenerated trace assets', () => {
    const entries = [
      entry('demo-planner_new-20260716133542.jsonl'),
      entry('demo-craft_engineer-20260716133542.jsonl'),
      entry('demo-line_leader-20260716133542.jsonl'),
    ]
    const sourceStems = entries.map(({ view }) => contextualizedTaskStem(view))
    const sourceGoals = entries.map(({ view }) => firstLectureGoal(view.lecture?.content.lecture_md))
    const wrapper = mount(ProfileComparison, { props: { entries, catalog } })

    expect(sourceStems.every(Boolean)).toBe(true)
    expect(new Set(sourceStems).size).toBe(3)
    expect(wrapper.findAll('.comparison-task-stem').map((item) => item.text()))
      .toEqual(sourceStems)
    expect(wrapper.findAll('.comparison-lecture-summary').map((item) => item.text()))
      .toEqual(sourceGoals)
  })

  it('renders only trace-backed goals and contextualized task stems', () => {
    const entries = [
      contextualizedEntry(
        'demo-planner_new-20260716133542.jsonl',
        '作为生产计划员，请核对H2601船2025-05的计划执行。',
        '了解三道关键工序的物流顺序',
      ),
      contextualizedEntry(
        'demo-craft_engineer-20260716133542.jsonl',
        '作为工艺工程师，请分析H2601船2025-05的完成率。',
        '理解完成率的定义与计算公式',
      ),
      contextualizedEntry(
        'demo-line_leader-20260716133542.jsonl',
        '作为一线班组长，请确认H2601船2025-05的现场完成量。',
        '明确计划量与实际完成量的定义差异',
      ),
    ]
    const wrapper = mount(ProfileComparison, { props: { entries, catalog } })

    expect(wrapper.findAll('.comparison-lecture-summary').map((item) => item.text()))
      .toEqual([
        '了解三道关键工序的物流顺序',
        '理解完成率的定义与计算公式',
        '明确计划量与实际完成量的定义差异',
      ])
    expect(wrapper.findAll('.comparison-task-stem').map((item) => item.text()))
      .toEqual([
        '作为生产计划员，请核对H2601船2025-05的计划执行。',
        '作为工艺工程师，请分析H2601船2025-05的完成率。',
        '作为一线班组长，请确认H2601船2025-05的现场完成量。',
      ])
    expect(wrapper.text()).not.toContain('这是给模型的指令')
    expect(wrapper.text()).not.toContain('三个岗位完全相同的标准题面')
    expect(wrapper.text()).not.toContain('正文不应被硬切为摘要')
  })

  it('updates with a replaced trace and removes differences when all columns use one trace', async () => {
    const planner = contextualizedEntry(
      'demo-planner_new-20260716133542.jsonl',
      '来自计划员trace的题面',
      '计划员目标',
    )
    const craft = contextualizedEntry(
      'demo-craft_engineer-20260716133542.jsonl',
      '来自工艺工程师trace的题面',
      '工艺工程师目标',
    )
    const leader = contextualizedEntry(
      'demo-line_leader-20260716133542.jsonl',
      '来自班组长trace的题面',
      '班组长目标',
    )
    const wrapper = mount(ProfileComparison, {
      props: { entries: [planner, craft, leader], catalog },
    })

    expect(wrapper.text()).toContain('来自工艺工程师trace的题面')
    const oneTrace = [structuredClone(planner), structuredClone(planner), structuredClone(planner)]
    await wrapper.setProps({ entries: oneTrace })

    expect(wrapper.findAll('.comparison-task-stem').map((item) => item.text()))
      .toEqual(Array(3).fill('来自计划员trace的题面'))
    expect(wrapper.text()).not.toContain('来自工艺工程师trace的题面')
    expect(wrapper.text()).not.toContain('来自班组长trace的题面')
  })

  it('shows an honest empty state and never falls back to standard_stem', () => {
    const missing = contextualizedEntry(
      'demo-planner_new-20260716133542.jsonl',
      undefined,
      '计划员目标',
    )
    const wrapper = mount(ProfileComparison, {
      props: { entries: [missing], catalog },
    })

    expect(wrapper.get('.comparison-task-stem').text()).toBe('暂无岗位实操任务')
    expect(wrapper.text()).not.toContain('三个岗位完全相同的标准题面')
  })
})
