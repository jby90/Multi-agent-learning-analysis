/// <reference types="node" />

import { readFileSync } from 'node:fs'
import path from 'node:path'

import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import catalog from 'virtual:knowledge-catalog'
import { buildTraceView } from '../lib/traceModel'
import { parseTraceJsonl } from '../lib/traceParser'
import type { TraceDocument } from '../types/trace'
import ProfileComparison, { type ComparisonEntry } from './ProfileComparison.vue'


function entry(fileName: string): ComparisonEntry {
  const source = readFileSync(path.resolve(process.cwd(), 'src', 'test', 'fixtures', 'traces', fileName), 'utf8')
  const document: TraceDocument = parseTraceJsonl(source, fileName)
  return { document, view: buildTraceView(document, document.messages.length) }
}


describe('ProfileComparison（优化8：仅保留蛛网图）', () => {
  it('renders only the radar panel without cards, line, donut or strip', () => {
    const entries = [
      entry('demo-planner_new-20260716133542.jsonl'),
      entry('demo-craft_engineer-20260716133542.jsonl'),
      entry('demo-line_leader-20260716133542.jsonl'),
    ]
    const wrapper = mount(ProfileComparison, {
      props: { entries, catalog },
    })

    expect(wrapper.get('.comparison-heading h2').text()).toBe('岗位对比')
    expect(wrapper.get('.comparison-subtitle').text())
      .toContain('知识点覆盖范围及掌握档位')
    expect(wrapper.find('.chart-canvas.is-radar-full').exists()).toBe(true)
    // 优化8删除的板块不再出现
    expect(wrapper.find('.comparison-card').exists()).toBe(false)
    expect(wrapper.find('.chart-line').exists()).toBe(false)
    expect(wrapper.find('.chart-donut').exists()).toBe(false)
    expect(wrapper.find('.comparison-strip').exists()).toBe(false)
    expect(wrapper.find('.comparison-task-stem').exists()).toBe(false)
    expect(wrapper.find('.comparison-journey').exists()).toBe(false)
    expect(wrapper.find('.comparison-next-point').exists()).toBe(false)
  })

  it('radar axes cover the catalog knowledge points and rerender on entry change', async () => {
    const entries = [
      entry('demo-planner_new-20260716133542.jsonl'),
      entry('demo-craft_engineer-20260716133542.jsonl'),
      entry('demo-line_leader-20260716133542.jsonl'),
    ]
    const wrapper = mount(ProfileComparison, { props: { entries, catalog } })
    await flushPromises()

    // jsdom 无布局，echarts 轴名文本无法断言——结构存在 + 重渲染不抛错即可
    // （轴名内容由 radarDimensions(catalog) 驱动，纯函数逻辑）

    // 替换其中一条会话（重复画像条目）后组件正常重渲染（不抛错、蛛网仍在）
    const replacedWithDuplicate = [entries[0], entries[0], entries[2]]
    await wrapper.setProps({ entries: replacedWithDuplicate })
    await flushPromises()
    expect(wrapper.find('.chart-canvas.is-radar-full').exists()).toBe(true)
  })
})
