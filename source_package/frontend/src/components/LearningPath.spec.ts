/// <reference types="node" />

import { readFileSync } from 'node:fs'
import path from 'node:path'

import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import catalog from 'virtual:knowledge-catalog'
import type { InteractiveState } from '../lib/interactiveApi'
import { buildTraceView } from '../lib/traceModel'
import { parseTraceJsonl } from '../lib/traceParser'
import type { TraceMessage, TraceView } from '../types/trace'
import LearningPath from './LearningPath.vue'


const update: TraceMessage = {
  msgId: 'path-001',
  traceId: 'path',
  step: 1,
  agent: 'system',
  role: 'system',
  payloadType: 'learning_path_update',
  content: {
    summary: '反证查询完成后已完成本轮岗位训练',
    completed_nodes: ['岗前测评', '岗位微课', '数据实操', '反证追问'],
    current_node: '培养目标达成',
    target_misconception: 'M-01',
    difficulty_action: 'step_up',
  },
  evidence: [],
  claims: [],
  timestamp: '2026-07-16T02:00:00+00:00',
  rejectedByBus: false,
  busErrors: [],
}

function pathView(path: TraceMessage): TraceView {
  return {
    visibleMessages: [path],
    currentState: 'S9_PATH_UPDATE',
    knowledgeDimensions: [],
    path,
    debateGroups: [],
  }
}

describe('LearningPath', () => {
  it('turns a corrected misconception into a prominent achievement card', () => {
    const wrapper = mount(LearningPath, { props: { view: pathView(update), catalog: [] } })

    const achievement = wrapper.get('.achievement-card')
    expect(achievement.text()).toContain('本轮达成')
    expect(achievement.text()).toContain('已修正 计划量与实际量的区分')
    expect(achievement.text()).not.toContain('已能用真实生产数据区分')
    expect(wrapper.text()).toContain('数据验证')
    expect(wrapper.text()).not.toMatch(/M-01|反证/)
    expect(wrapper.find('.path-heading h2').exists()).toBe(false)
    expect(wrapper.find('.path-note').exists()).toBe(false)
    expect(wrapper.find('.learning-path .difficulty-action').exists()).toBe(false)
  })

  it('uses learner wording when the path adds a simpler explanation', () => {
    const wrapper = mount(LearningPath, {
      props: {
        view: pathView({
          ...update,
          content: { ...update.content, difficulty_action: 'step_down' },
        }),
        catalog: [],
      },
    })

    expect(wrapper.text()).not.toContain('已完成补充讲解')
    expect(wrapper.text()).not.toContain('降维')
    expect(wrapper.find('.difficulty-action').exists()).toBe(false)
  })

  it('leaves an unstarted path quiet instead of explaining the obvious', () => {
    const wrapper = mount(LearningPath, {
      props: {
        view: {
          visibleMessages: [],
          currentState: 'S0_INIT',
          knowledgeDimensions: [],
          debateGroups: [],
        },
        catalog: [],
      },
    })

    expect(wrapper.find('.path-empty').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('培养路径会随测评、微课与实操结果逐步更新')
  })

  it('keeps the full summary and appends a data-derived future node', () => {
    const fileName = 'demo-planner_new-20260716133542.jsonl'
    const source = readFileSync(path.resolve(process.cwd(), '..', 'traces', fileName), 'utf8')
    const document = parseTraceJsonl(source, fileName)
    const view = buildTraceView(document, document.messages.length)
    const wrapper = mount(LearningPath, {
      props: { view, catalog },
    })

    expect(wrapper.get('.path-heading h2').text()).toBe(
      '刚才你把计划量当成了实际完成量——你自己查出的数据（计划量1855.06、实际完成量1156.87）纠正了这一点。下一步：完成率计算。',
    )
    expect(wrapper.get('.path-heading h2').text()).not.toContain('完成岗前测评3/5与岗位微课')
    expect(wrapper.get('.path-node.is-planned').text()).toContain('下一步：完成率计算')
    expect(wrapper.get('.path-node.is-planned').text()).toContain('应用档')
    expect(wrapper.findAll('.path-node').length).toBeGreaterThanOrEqual(5)
  })

  it('prefers the current live artifact over an older trace path summary', () => {
    const state: InteractiveState = {
      session_id: 'live-route',
      trace_id: 'interactive-live-route',
      trace_path: 'traces/interactive-live-route.jsonl',
      state: 'S3_TASK',
      awaiting: 'advance',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages: [],
      artifact: {
        payload: {
          type: 'lecture_note',
          content: { knowledge_point: '异常衰减规律', difficulty: 'basic' },
        },
      },
      interaction: null,
      current_difficulty: 'basic',
    }
    const stale = pathView({
      ...update,
      content: {
        ...update.content,
        summary: '下一步：三道工序与传导关系。',
        current_node: '三道工序与传导关系',
      },
    })

    const wrapper = mount(LearningPath, {
      props: { view: stale, catalog, state },
    })

    expect(wrapper.text()).toContain('当前训练：异常衰减规律')
    expect(wrapper.text()).not.toContain('下一步：三道工序与传导关系')
  })

  it('shows the provisional probe route instead of the preliminary fallback', () => {
    const state: InteractiveState = {
      session_id: 'probe-route',
      trace_id: 'interactive-probe-route',
      trace_path: 'traces/interactive-probe-route.jsonl',
      state: 'S1_DIAGNOSIS',
      awaiting: 'diagnostic_probe',
      mode: 'live',
      profile: { profile_id: 'planner_new', title: '新入职生产计划员' },
      messages: [],
      artifact: {
        payload: {
          type: 'diagnosis_report',
          content: { selected_knowledge_point: '三道工序与传导关系' },
        },
      },
      interaction: {
        kind: 'supplemental_diagnosis',
        title: '补充诊断',
        message: '等待探针',
        questions: [],
        provisional_route: { knowledge_point: '偏差率与风险等级' },
      },
    }

    const wrapper = mount(LearningPath, {
      props: { view: pathView(update), catalog, state },
    })

    expect(wrapper.text()).toContain('当前训练：偏差率与风险等级')
    expect(wrapper.text()).not.toContain('当前训练：三道工序与传导关系')
  })
})
