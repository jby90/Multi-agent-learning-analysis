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

const traceDirectory = path.resolve(process.cwd(), 'src', 'test', 'fixtures', 'traces')
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

    expect(coverage).toMatchObject({ covered: 1, total: 5 })
    expect(coverage?.items.find((item) => item.name === '三道工序与传导关系')?.covered).toBe(true)
    expect(coverage?.items.find((item) => item.name === '偏差率与风险等级')?.covered).toBe(false)
  })

  it('accumulates coverage across difficulty units so the count never regresses', () => {
    // craft 会话：应用档微课铺垫责任单元/异常衰减（3/5），进阶微课换成
    // 铺垫三道工序（+1）；累计语义保证换单元时覆盖数只增不减。
    const full = resourceCoverage(craft)
    expect(full).toMatchObject({ covered: 4, total: 5 })

    const document = parseTraceJsonl(craftSource, 'demo-craft_engineer-20260716133542.jsonl')
    const lectures = document.messages.filter(
      (message) => message.payloadType === 'lecture_note',
    )
    expect(lectures.length).toBeGreaterThanOrEqual(2)
    const switchStep = lectures[1].step
    const beforeSwitch = buildTraceView(document, switchStep - 1)
    const afterSwitch = buildTraceView(document, switchStep)
    expect(resourceCoverage(beforeSwitch)).toMatchObject({ covered: 3, total: 5 })
    expect(resourceCoverage(afterSwitch)).toMatchObject({ covered: 4, total: 5 })
    expect(resourceCoverage(afterSwitch).covered)
      .toBeGreaterThanOrEqual(resourceCoverage(beforeSwitch).covered)
  })

  it('traces the applied-to-advanced stages and preserves an explicit step-down action', () => {
    const journey = difficultyJourney(craft, catalog)

    expect(journey?.points.map((point) => point.level)).toEqual([
      'applied', 'advanced', 'advanced', 'advanced', 'advanced',
    ])

    const practiceTask = [...craft.visibleMessages].reverse().find(
      (message) => (message.payloadType === 'quiz_set' || message.payloadType === 'practice_guide')
        && typeof message.content.knowledge_point === 'string'
        && (message.content.event === undefined || message.content.event === 'product_ready'),
    )
    expect(practiceTask).toBeDefined()
    const stepDownReview = {
      ...craft.visibleMessages[0],
      msgId: 'spec-step-down-review',
      step: 950,
      payloadType: 'review_verdict',
      content: { reviewed_msg_id: practiceTask?.msgId },
      verdict: {
        decision: 'approve' as const,
        ruleHits: [],
        difficultyAction: 'step_down' as const,
      },
    }
    const stepDownView: TraceView = {
      ...craft,
      visibleMessages: [...craft.visibleMessages, stepDownReview],
    }

    expect(difficultyJourney(stepDownView, catalog).points.some(
      (point) => point.action === 'step_down',
    )).toBe(true)
  })

  it('ignores advisory step-down verdicts on conclusion and follow-up products', () => {
    const baseJourney = difficultyJourney(craft, catalog)
    const basePractice = baseJourney.points.find(
      (point) => point.stage === 'practice',
    )
    const baseValidation = baseJourney.points.find(
      (point) => point.stage === 'validation',
    )
    expect(basePractice).toBeDefined()
    expect(baseValidation).toBeDefined()

    const conclusionTask = { ...craft.visibleMessages[0] }
    conclusionTask.msgId = 'spec-conclusion-task'
    conclusionTask.step = 900
    conclusionTask.payloadType = 'quiz_set'
    conclusionTask.content = {
      event: 'assessment_ready',
      knowledge_point: '三道工序与传导关系',
      difficulty: 'advanced',
    }
    const followUpTask = { ...craft.visibleMessages[0] }
    followUpTask.msgId = 'spec-follow-up-task'
    followUpTask.step = 901
    followUpTask.payloadType = 'quiz_set'
    followUpTask.content = {
      event: 'follow_up_question_ready',
      knowledge_point: '三道工序与传导关系',
      difficulty: 'advanced',
    }
    const advisoryVerdict = (reviewedMsgId: string, step: number) => ({
      ...craft.visibleMessages[0],
      msgId: `spec-verdict-${step}`,
      step,
      payloadType: 'review_verdict',
      content: { reviewed_msg_id: reviewedMsgId },
      verdict: {
        decision: 'approve_with_fix',
        ruleHits: [],
        difficultyAction: 'step_down',
      },
    })
    const taintedView: TraceView = {
      ...craft,
      visibleMessages: [
        ...craft.visibleMessages,
        conclusionTask,
        advisoryVerdict('spec-conclusion-task', 902),
        followUpTask,
        advisoryVerdict('spec-follow-up-task', 903),
      ],
    }

    const journey = difficultyJourney(taintedView, catalog)
    const practice = journey.points.find((point) => point.stage === 'practice')
    const validation = journey.points.find((point) => point.stage === 'validation')

    expect(practice?.level).toBe(basePractice?.level)
    expect(validation?.level).toBe(baseValidation?.level)
    expect(validation?.action).toBe(baseValidation?.action)
    expect(journey.points.some((point) => point.action === 'step_down')).toBe(false)
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

  it('derives next points from remaining blind spots and prerequisites', () => {
    const plans = [planner, craft, leader].map((view) => nextLearningPlan(view, catalog))

    expect(plans.map((plan) => plan?.knowledgePoint)).toEqual([
      '偏差率与风险等级',
      '偏差率与风险等级',
      '三道工序与传导关系',
    ])
    // 下一知识点尚未开始，难度用目录基准档（basic），不再跟随当前单元实时难度。
    expect(plans[0]?.difficulty).toBe('basic')
    // craft 会话累计覆盖了跨工序归因、三道工序、责任单元、异常衰减，
    // 只剩偏差率与风险等级，与 planner 同向；leader 的微课没有前置铺垫，
    // 下一学习点与另外两个岗位不同。
    expect(plans[2]?.knowledgePoint).not.toBe(plans[0]?.knowledgePoint)
  })

  it('fills the fixed correction template exclusively from trace evidence', () => {
    expect(learningSummary(leader, catalog)).toBe(
      '刚才你把计划量当成了实际完成量——你自己查出的数据（计划量1855.06、实际完成量1156.87）纠正了这一点。下一步：三道工序与传导关系。',
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
      '你完成了三道工序与传导关系的学习与实操。下一步：偏差率与风险等级。',
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
