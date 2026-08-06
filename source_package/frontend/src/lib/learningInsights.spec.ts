/// <reference types="node" />

import { readFileSync } from 'node:fs'
import path from 'node:path'

import { describe, expect, it } from 'vitest'

import catalog from 'virtual:knowledge-catalog'
import type { TraceView } from '../types/trace'
import {
  difficultyJourney,
  learningSummary,
  nextLearningPlan,
  resourceCoverage,
} from './learningInsights'
import { buildTraceView } from './traceModel'
import { parseTraceJsonl } from './traceParser'

const traceDirectory = path.resolve(process.cwd(), '..', 'traces')
const plannerSource = readFileSync(
  path.join(traceDirectory, 'demo-planner_new-20260716133542.jsonl'),
  'utf8',
)
const craftSource = readFileSync(
  path.join(traceDirectory, 'demo-craft_engineer-20260716133542.jsonl'),
  'utf8',
)
const leaderSource = readFileSync(
  path.join(traceDirectory, 'demo-line_leader-20260716133542.jsonl'),
  'utf8',
)


function fullView(source: string, fileName: string): TraceView {
  const document = parseTraceJsonl(source, fileName)
  return buildTraceView(document, document.messages.length)
}

const planner = fullView(plannerSource, 'demo-planner_new-20260716133542.jsonl')
const craft = fullView(craftSource, 'demo-craft_engineer-20260716133542.jsonl')
const leader = fullView(leaderSource, 'demo-line_leader-20260716133542.jsonl')


describe('learning insight derivation', () => {
  it('matches actual lecture and task coverage against diagnosed blind spots', () => {
    const coverage = resourceCoverage(planner)

    expect(coverage).toMatchObject({ covered: 2, total: 5 })
    expect(coverage?.items.find((item) => item.name === '完成率计算')?.covered).toBe(false)
    expect(coverage?.items.find((item) => item.name === '计划量与实际量口径')?.covered).toBe(true)
  })

  it('traces five difficulty stages and preserves an explicit step-down action', () => {
    const journey = difficultyJourney(craft, catalog)

    expect(journey?.points.map((point) => point.level)).toEqual([
      'applied', 'basic', 'basic', 'applied', 'advanced',
    ])

    const latestLearningTask = [...craft.visibleMessages].reverse().find(
      (message) => (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
        && typeof message.content.knowledge_point === 'string',
    )
    const latestTaskReview = [...craft.visibleMessages].reverse().find(
      (message) => message.payloadType === 'review_verdict'
        && message.content.reviewed_msg_id === latestLearningTask?.msgId,
    )
    expect(latestTaskReview).toBeDefined()
    const stepDownView: TraceView = {
      ...craft,
      visibleMessages: craft.visibleMessages.map((message) => (
        message.msgId === latestTaskReview?.msgId
          ? { ...message, verdict: { ...message.verdict!, difficultyAction: 'step_down' } }
          : message
      )),
    }

    expect(difficultyJourney(stepDownView, catalog).points.some(
      (point) => point.action === 'step_down',
    )).toBe(true)
  })

  it('does not draw validation before a SQL result exists', () => {
    const firstSqlResult = craft.visibleMessages.find(
      (message) => message.payloadType === 'sql_result',
    )
    expect(firstSqlResult).toBeDefined()
    const preSqlMessages = craft.visibleMessages.filter(
      (message) => message.step < (firstSqlResult?.step ?? 0),
    )
    const preSqlView: TraceView = {
      ...craft,
      visibleMessages: preSqlMessages,
      sqlResult: undefined,
      path: undefined,
    }

    expect(difficultyJourney(preSqlView, catalog).points.map(
      (point) => point.stage,
    )).not.toContain('validation')
  })

  it('uses an explicit path difficulty without applying the same step-up twice', () => {
    const view: TraceView = {
      ...craft,
      path: craft.path
        ? {
            ...craft.path,
            content: {
              ...craft.path.content,
              difficulty_action: 'step_up',
              difficulty: 'applied',
            },
          }
        : undefined,
    }

    const journey = difficultyJourney(view, catalog)

    expect(journey.points.at(-1)).toMatchObject({
      stage: 'advanced',
      level: 'applied',
      action: 'step_up',
    })
  })

  it('derives three distinct next points from remaining blind spots and prerequisites', () => {
    const plans = [planner, craft, leader].map((view) => nextLearningPlan(view, catalog))

    expect(plans.map((plan) => plan?.knowledgePoint)).toEqual([
      '完成率计算',
      '月度聚合方法',
      '异常识别标准',
    ])
    expect(plans[0]?.difficulty).toBe('applied')
    expect(new Set(plans.map((plan) => plan?.knowledgePoint)).size).toBe(3)
  })

  it('fills the fixed correction template exclusively from trace evidence', () => {
    expect(learningSummary(planner, catalog)).toBe(
      '刚才你把计划量当成了实际完成量——你自己查出的数据（计划量1855.06、实际完成量1156.87）纠正了这一点。下一步：完成率计算。',
    )
  })

  it('uses the fixed no-correction template when no correction event occurred', () => {
    const path = planner.visibleMessages.find(
      (message) => message.payloadType === 'learning_path_update'
        && message.content.current_node === '结论判断',
    )
    expect(path).toBeDefined()
    const noCorrection: TraceView = {
      ...planner,
      visibleMessages: planner.visibleMessages.filter((message) => message.step <= (path?.step ?? 0)),
      path,
    }

    expect(learningSummary(noCorrection, catalog)).toBe(
      '你完成了三道工序与传导关系的学习与实操。下一步：完成率计算。',
    )
  })

  it('does not invent a next point when every diagnosed blind spot is covered', () => {
    const coveredPoint = planner.lecture?.content.knowledge_point
    expect(typeof coveredPoint).toBe('string')
    const fullyCovered: TraceView = {
      ...planner,
      diagnosis: planner.diagnosis && {
        ...planner.diagnosis,
        content: {
          ...planner.diagnosis.content,
          blind_spots: [coveredPoint],
        },
      },
    }

    expect(nextLearningPlan(fullyCovered, catalog)).toBeUndefined()
    expect(learningSummary(fullyCovered, catalog)).toBeUndefined()
  })
})
