import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import { buildTraceView } from '../lib/traceModel'
import { parseTraceJsonl } from '../lib/traceParser'
import { demoTrace } from '../test/traceFixtures'
import ResourcePanel from './ResourcePanel.vue'


function resourceView(body: string, claim: string) {
  const document = parseTraceJsonl(
    demoTrace(
      'demo-resource',
      'planner_new',
      '新入职生产计划员',
      claim,
      '1156.87',
    ),
    'demo-resource.jsonl',
  )
  const lecture = document.messages.find((message) => message.payloadType === 'lecture_note')
  if (!lecture) throw new Error('fixture lecture missing')
  lecture.content.lecture_md = body
  lecture.content.knowledge_point = '计划量与实际量口径'
  lecture.content.coverage = ['计划量与实际量口径']
  return buildTraceView(document, document.messages.length)
}

function taskView(
  payloadType: 'practice_guide' | 'quiz_set',
  content: Record<string, unknown>,
) {
  const view = resourceView(
    '# 岗位微课\n\n按题面要求逐步完成本轮练习。',
    '按题面要求逐步完成本轮练习。',
  )
  if (!view.lecture) throw new Error('fixture lecture missing')
  view.task = {
    ...view.lecture,
    msgId: `demo-resource-${payloadType}`,
    payloadType,
    content,
  }
  view.visibleMessages.push(view.task)
  return view
}


describe('ResourcePanel', () => {
  it('paginates the lesson and omits task/result duplicates in resident mode', async () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '# 岗位微课\n\n#### 本节目标\n\n理解计划量。\n\n#### 核心概念\n\n实际完成量表示真实产出。',
          '实际完成量表示真实产出。',
        ),
        lessonPager: true,
      },
    })

    expect(wrapper.get('[aria-label="微课分页阅读"]')).toBeTruthy()
    expect(wrapper.get('.resource-heading h2').text()).toBe('知识卡片')
    expect(wrapper.find('#task-resource').exists()).toBe(false)
    expect(wrapper.find('.sql-result').exists()).toBe(false)
    expect(wrapper.get('button[aria-label="下一张微课卡片"]')).toBeTruthy()

    const before = wrapper.get('.lesson-page-heading h3').text()
    await wrapper.get('button[aria-label="下一张微课卡片"]').trigger('click')
    expect(wrapper.get('.lesson-page-heading h3').text()).not.toBe(before)
  })

  it('reports lesson page state so the shell can change layout on the practice page', async () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('practice_guide', {
          difficulty: 'basic',
          question: '查询本月三道工序完成率。',
          guide_intro: '先确认月份和工序。',
          guide_steps: ['确认月份。', '选择三道工序。', '核对完成率。'],
          completion_criteria: ['结果包含三道工序。'],
        }),
        lessonPager: true,
      },
    })

    expect(wrapper.emitted('pageState')?.at(-1)?.[0]).toMatchObject({ kind: 'metrics' })
    const next = wrapper.get('button[aria-label="下一张微课卡片"]')
    while (next.attributes('disabled') === undefined) await next.trigger('click')

    expect(wrapper.emitted('pageState')?.at(-1)?.[0]).toMatchObject({
      kind: 'task',
      isLast: true,
    })
  })

  it('uses the expanded lesson space for a navigable content overview', async () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '# 岗位微课\n\n#### 学习目标\n\n理解计划量与实际量。\n\n#### 业务判断\n\n根据完成率识别计划偏差。',
          '根据完成率识别计划偏差。',
        ),
        lessonPager: true,
      },
    })

    expect(wrapper.find('[aria-label="本节内容概览"]').exists()).toBe(false)
    await wrapper.get('button[aria-label="最大化微课"]').trigger('click')
    const overview = wrapper.get('[aria-label="本节内容概览"]')
    expect(overview.text()).toContain('学习目标')
    expect(overview.text()).toContain('业务判断')

    await overview.findAll('button')[1]!.trigger('click')
    expect(wrapper.get('.lesson-page-heading h3').text()).toBe('业务判断')
  })

  it('does not create an empty lesson page for a standalone document heading', async () => {
    const view = taskView('practice_guide', {
      difficulty: 'basic',
      question: '查询三道工序完成率。',
      guide_intro: '先确认口径。',
      guide_steps: ['确认月份。'],
      completion_criteria: ['返回工序和完成率。'],
    })
    if (!view.lecture) throw new Error('fixture must include a lecture')
    view.lecture.content.lecture_md = '# 微课总标题\n## 学习目标\n掌握三道工序关系。'

    const wrapper = mount(ResourcePanel, {
      props: { view, lessonPager: true },
    })

    expect(wrapper.get('.lesson-page-heading h3').text()).toBe('本节关键数据')
    await wrapper.get('button[aria-label="下一张微课卡片"]').trigger('click')
    expect(wrapper.get('.lesson-page-heading h3').text()).toBe('学习目标')
    expect(wrapper.get('.lesson-page-copy').text()).toContain('掌握三道工序关系')
  })

  it('places progressive hints and the round summary inside the practice guide', async () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('practice_guide', {
          difficulty: 'applied',
          question: '按工序查询三道工序完成率。',
          guide_intro: '先明确查询范围。',
          guide_steps: ['确认船号。', '确认月份。', '按工序汇总。'],
          completion_criteria: ['结果包含工序和完成率。'],
          query_authority: {
            output_columns: ['process_code', 'completion_rate'],
            filter_columns: ['ship_no', 'period_date'],
            group_by_columns: ['process_code'],
            time_values: ['2025-05'],
          },
        }),
        lessonPager: true,
        guidanceFeedback: '字段和值已经能够支撑判断。',
        guidanceNextStepReason: '可以进入结果解释。',
      },
    })
    const next = wrapper.get('button[aria-label="下一张微课卡片"]')
    while (next.attributes('disabled') === undefined) await next.trigger('click')

    await wrapper.get('.practice-coaching-heading button').trigger('click')
    expect(wrapper.get('.practice-coaching').text()).toContain('先确认题目对象')
    expect(wrapper.get('.practice-round-summary').text()).toContain('字段和值已经能够支撑判断')
    expect(wrapper.get('.practice-round-summary').text()).toContain('进入下一步的理由')
  })

  it('keeps the current lesson card while entering and leaving focus mode', async () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '# 岗位微课\n\n#### 本节目标\n\n理解计划量。\n\n#### 核心概念\n\n实际完成量表示真实产出。',
          '实际完成量表示真实产出。',
        ),
        lessonPager: true,
      },
    })

    await wrapper.get('button[aria-label="下一张微课卡片"]').trigger('click')
    const currentCard = wrapper.get('.lesson-page-heading h3').text()

    await wrapper.get('.content-focus-toggle').trigger('click')
    expect(wrapper.get('.resource-panel').classes()).toContain('is-focus-mode')
    expect(wrapper.get('.lesson-page-heading h3').text()).toBe(currentCard)
    expect(wrapper.get('.content-focus-toggle').attributes('aria-label'))
      .toBe('退出微课专注模式')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await wrapper.vm.$nextTick()

    expect(wrapper.get('.resource-panel').classes()).not.toContain('is-focus-mode')
    expect(wrapper.get('.lesson-page-heading h3').text()).toBe(currentCard)
    wrapper.unmount()
  })

  it('keeps the personalized practice question and difficulty in resident lesson pages', async () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('quiz_set', {
          difficulty: 'applied',
          misconception: 'M-01',
          question: '作为新入职的生产计划员，请核对H2601在2025-05的计划量与实际完成量。',
        }),
        lessonPager: true,
      },
    })

    const next = wrapper.get('button[aria-label="下一张微课卡片"]')
    while (next.attributes('disabled') === undefined) {
      await next.trigger('click')
    }

    expect(wrapper.get('.lesson-page-heading h3').text()).toBe('练习题')
    expect(wrapper.get('.lesson-page-task h3').text())
      .toContain('作为新入职的生产计划员')
    expect(wrapper.get('.lesson-page-task .task-difficulty-ladder').attributes('aria-label'))
      .toBe('题目难度：应用，三级分阶中的第二级')
    expect(wrapper.get('.lesson-page-task .probe-notice').text())
      .toContain('本题针对：计划量与实际量的区分')
  })

  it('renders level-four markdown headings without leaking hash markers', () => {
    const claim = '计划量与实际量必须分开理解。'
    const wrapper = mount(ResourcePanel, {
      props: { view: resourceView(`# 岗位微课\n\n#### 本节目标\n\n${claim}`, claim) },
    })

    expect(wrapper.findAll('.safe-markdown h3').map((node) => node.text()))
      .toContain('本节目标')
    expect(wrapper.get('.resource-heading h2').text()).toBe('微课 / 实操')
    expect(wrapper.text()).not.toContain('微课、任务与数据实操')
    expect(wrapper.text()).not.toContain('讲义中的事实结论均可通过脚注查看原始依据')
    expect(wrapper.text()).not.toContain('#### 本节目标')
    expect(wrapper.get('.professional-review-badge').text()).toContain('已通过专业审核')
  })

  it('keeps naturally rewritten claims visibly linked to their evidence', () => {
    const claim = '计划量与实际量必须分开理解。'
    const wrapper = mount(ResourcePanel, {
      props: { view: resourceView('# 岗位微课\n\n两种数量口径要分别理解。', claim) },
    })

    expect(wrapper.get('.claim-register .claim-anchor').text()).toBe(claim)
    expect(wrapper.get('.claim-register h4').text()).toBe('数据出处')
    expect(wrapper.get('.claim-register .evidence-popover').text())
      .toContain('出自《计划量与实际量口径》知识点')
  })

  it('renders safe inline markdown without showing syntax delimiters', () => {
    const claim = '计划量与实际量必须分开理解。'
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '# 岗位微课\n\n**计划量**与`actual_qty`需要分开核对。',
          claim,
        ),
      },
    })

    expect(wrapper.text()).toContain('计划量与实际完成量需要分开核对。')
    expect(wrapper.text()).not.toContain('actual_qty')
    expect(wrapper.text()).not.toContain('**')
    expect(wrapper.text()).not.toContain('`')
  })

  it('keeps all source terminology out of D3-shaped lecture copy and evidence UI', async () => {
    const view = resourceView(
      [
        '# raw未明确，以现场口径为准',
        '',
        'raw 数据中的安全结论。',
        '- chunk / chunks 来自 SEC-001 与 KB-003。',
      ].join('\n'),
      '安全结论',
    )
    if (!view.lecture) throw new Error('fixture lecture missing')
    view.lecture.content.knowledge_point = 'raw'
    view.lecture.evidence = [{
      kind: 'kb_chunk',
      ref: 'SEC-001',
      quote: 'KB-003 raw 数据',
      supportsClaim: '安全结论',
    }]

    const wrapper = mount(ResourcePanel, { props: { view } })
    const visibleCopy = () => [
      wrapper.text(),
      ...wrapper.findAll('[aria-label]').map((node) => node.attributes('aria-label') ?? ''),
    ].join(' ')

    expect(wrapper.text()).toContain('以现场实际口径为准')
    expect(wrapper.text()).toContain('现有资料中的安全结论')
    expect(wrapper.text()).toContain('资料片段 / 资料片段 来自 资料来源 与 资料来源')
    expect(visibleCopy()).not.toMatch(
      /\braw\b|\bchunks?\b|\b(?:SEC|KB)-[A-Za-z0-9_-]+\b/iu,
    )

    await wrapper.get('.claim-anchor').trigger('focus')
    expect(wrapper.get('.evidence-popover').text()).toContain('出自《现有资料》知识点')
    expect(visibleCopy()).not.toMatch(
      /\braw\b|\bchunks?\b|\b(?:SEC|KB)-[A-Za-z0-9_-]+\b/iu,
    )
  })

  it('groups the lecture into readable cards and promotes trace-backed numbers', () => {
    const claim = '计划量与实际量必须分开理解。'
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '### 核心概念\n计划量：1855.06\n### 数据示例\n实际完成量：1156.87',
          claim,
        ),
      },
    })

    expect(wrapper.findAll('.lecture-section-card')).toHaveLength(2)
    expect(wrapper.findAll('.instrument-number').map((node) => node.text()))
      .toEqual(expect.arrayContaining(['1855.06', '1156.87']))
  })

  it('uses the resulting percentage rather than the numerator for a completion-rate metric', () => {
    const claim = '完成率要使用加权比值。'
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '### 数据示例\n完成率：1156.87 ÷ 1855.06 ≈ 62.36%',
          claim,
        ),
      },
    })

    const completionMetric = wrapper.findAll('.lecture-key-numbers article')
      .find((node) => node.text().includes('完成率'))
    expect(completionMetric?.get('.instrument-number').text()).toBe('62.36%')
  })

  it('does not turn a normal range boundary into a second completion-rate card', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          [
            '### 数据示例',
            '完成率：1156.87 ÷ 1855.06 ≈ 62.36%',
            '正常月份完成率一般在 88%—103% 区间波动。',
          ].join('\n'),
          '完成率要使用加权比值。',
        ),
      },
    })

    const metrics = wrapper.findAll('.lecture-key-numbers article')
    const completionMetrics = metrics.filter((node) => node.get('span').text() === '完成率')
    expect(completionMetrics).toHaveLength(1)
    expect(completionMetrics[0]?.get('.instrument-number').text()).toBe('62.36%')
  })

  it('keeps the measured rate when a normal range appears on the same line', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: resourceView(
          '### 数据示例\n完成率：62.36%；正常区间为 88%—103%。',
          '完成率要使用加权比值。',
        ),
      },
    })

    const completionMetric = wrapper.findAll('.lecture-key-numbers article')
      .find((node) => node.get('span').text() === '完成率')
    expect(completionMetric?.get('.instrument-number').text()).toBe('62.36%')
  })

  it('describes the targeted misconception without exposing its mechanism code', () => {
    const view = resourceView('# 岗位微课\n\n计划量与实际量要分开。', '计划量与实际量要分开。')
    if (!view.lecture) throw new Error('fixture lecture missing')
    view.task = {
      ...view.lecture,
      payloadType: 'quiz_set',
      content: {
        event: 'counter_evidence_ready',
        misconception: 'M-01',
        question: '分别查询计划量与实际完成量。',
      },
    }
    const wrapper = mount(ResourcePanel, { props: { view } })

    expect(wrapper.get('.probe-notice').text())
      .toContain('本题针对：计划量与实际量的区分')
    expect(wrapper.get('.probe-notice').text()).not.toMatch(/M-01|反证/)
  })

  it('renders task difficulty as an explicit three-level staircase', () => {
    const view = resourceView('# 岗位微课\n\n计划量与实际量要分开。', '计划量与实际量要分开。')
    if (!view.lecture) throw new Error('fixture lecture missing')
    view.task = {
      ...view.lecture,
      msgId: 'demo-resource-task',
      payloadType: 'quiz_set',
      content: {
        event: 'product_ready',
        knowledge_point: '计划量与实际量口径',
        difficulty: 'basic',
        question: '分别查询计划量与实际完成量。',
      },
    }
    view.visibleMessages.push(view.task)

    const wrapper = mount(ResourcePanel, { props: { view } })
    const ladder = wrapper.get('.task-difficulty-ladder')

    expect(ladder.attributes('aria-label')).toBe('题目难度：基础，三级分阶中的第一级')
    expect(ladder.findAll('.task-difficulty-step').map((item) => item.text()))
      .toEqual(['基础', '应用', '进阶'])
    expect(ladder.get('.is-active').text()).toBe('基础')
  })

  it('shows a diagnosis-routed task only in teaching language', () => {
    const view = resourceView(
      '# 岗位微课\n\n三道工序按生产顺序衔接。',
      '三道工序按生产顺序衔接。',
    )
    if (!view.lecture) throw new Error('fixture lecture missing')
    view.task = {
      ...view.lecture,
      msgId: 'demo-routed-task',
      payloadType: 'practice_guide',
      content: {
        event: 'product_ready',
        template_id: 'T-03-A',
        diagnostic_template_ids: { applied: 'T-03-A' },
        family: 'Q6',
        route: 'diagnostic',
        state: 'S3_TASK',
        knowledge_point: '三道工序与传导关系',
        difficulty: 'applied',
        question: '按工序顺序查询三道工序完成率。',
        guide_intro: '先确认三道工序的生产顺序。',
        guide_steps: [
          '确认三道工序的生产顺序。',
          '分别查询各工序的完成率。',
          '按生产顺序核对查询结果。',
        ],
        completion_criteria: ['结果覆盖三道工序并保持顺序一致。'],
      },
    }
    view.visibleMessages.push(view.task)

    const wrapper = mount(ResourcePanel, { props: { view } })
    const publicCopy = [
      wrapper.text(),
      ...wrapper.findAll('[aria-label]').map((node) => node.attributes('aria-label') ?? ''),
    ].join(' ')

    expect(publicCopy).toContain('按工序顺序查询三道工序完成率')
    expect(publicCopy).toContain('题目难度：应用')
    expect(publicCopy).not.toMatch(
      /T-03-A|Q6|diagnostic_template_ids|\bfamily\b|\broute\b|\bstate\b|S3_TASK/u,
    )
  })

  it('renders a production-domain practice guide with its prompt, steps, criteria, and difficulty', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('practice_guide', {
          difficulty: 'applied',
          question: '分析H2601船在2025-05的计划量与实际完成量。',
          guide_intro: '先明确统计范围，再逐项核对数据口径。',
          guide_steps: [
            '确认船号、月份和工序范围。',
            '分别汇总计划量与实际完成量。',
            '对比两项数据并记录偏差方向。',
          ],
          completion_criteria: [
            '结果包含计划量与实际完成量。',
            '能够说明偏差方向。',
          ],
        }),
      },
    })

    const guide = wrapper.get('.practice-guide-card')
    expect(guide.get('.resource-eyebrow').text()).toContain('实操指南')
    expect(guide.get('.guide-question').text())
      .toContain('分析H2601船在2025-05的计划量与实际完成量')
    expect(guide.get('.guide-intro').text()).toContain('先明确统计范围')
    expect(guide.findAll('ol.guide-steps > li').map((item) => item.text())).toEqual([
      '确认船号、月份和工序范围。',
      '分别汇总计划量与实际完成量。',
      '对比两项数据并记录偏差方向。',
    ])
    expect(guide.findAll('ul.guide-criteria > li').map((item) => item.text())).toEqual([
      '结果包含计划量与实际完成量。',
      '能够说明偏差方向。',
    ])
    expect(guide.get('.task-difficulty-ladder').attributes('aria-label'))
      .toContain('题目难度：应用')
  })

  it('renders a first-segment guide from safe structured guide markdown', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('practice_guide', {
          difficulty: 'advanced',
          question: '核对首段制造各月份的计划执行情况。',
          guide_md: [
            '## 实操目标',
            '核对首段制造各月份的计划执行情况。',
            '## 开始前',
            '先确认统计月份和责任单元。',
            '## 操作步骤',
            '1. 确认要核对的月份范围。',
            '2. 按责任单元整理计划与实际数据。',
            '3. 比较各月差异并标记需要复核的月份。',
            '## 完成标准',
            '- 至少完成三个月份的核对。',
            '- 结论能够对应到具体月份。',
          ].join('\n'),
        }),
      },
    })

    const guide = wrapper.get('.practice-guide-card')
    expect(guide.get('.guide-question').text()).toContain('核对首段制造各月份')
    expect(guide.get('.guide-intro').text()).toContain('先确认统计月份和责任单元')
    expect(guide.findAll('ol.guide-steps > li')).toHaveLength(3)
    expect(guide.findAll('ul.guide-criteria > li').map((item) => item.text())).toEqual([
      '至少完成三个月份的核对。',
      '结论能够对应到具体月份。',
    ])
    expect(guide.text()).not.toContain('##')
  })

  it('fails closed for every polluted guide item in visible copy and aria text', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('practice_guide', {
          difficulty: 'basic',
          question: 'practice_guide guide_md guide_intro template_id query_authority',
          guide_intro: '由 Agent、LLM、智能体和状态机给出内部提示。',
          guide_steps: [
            'T-03 Q6 family msg_id standard_sql SELECT secret_answer FROM answer_key',
            'T01 S10 no_matching_transition rule_hits rebuttal verdict',
            'safe_rejected external_unavailable system_error step_up step_down injected_for_demo',
          ],
          completion_criteria: [
            '答案键 quiz_answer_key expected_rows expected_points',
          ],
        }),
      },
    })
    const guide = wrapper.get('.practice-guide-card')
    const publicCopy = [
      wrapper.text(),
      ...wrapper.findAll('[aria-label]').map((node) => node.attributes('aria-label') ?? ''),
    ].join(' ')

    expect(guide.get('.guide-question').text()).toContain('实操题面暂不可用')
    expect(guide.get('.guide-intro').text()).toContain('请根据题面逐步完成操作')
    expect(guide.findAll('ol.guide-steps > li')).toHaveLength(3)
    expect(guide.findAll('ol.guide-steps > li').every(
      (item) => item.text().includes('本步骤包含内部信息，已隐藏'),
    )).toBe(true)
    expect(guide.get('ul.guide-criteria > li').text()).toContain('请按题面要求核对完成情况')
    expect(publicCopy).not.toMatch(
      /T-?0?3|\bT(?:0?[1-9]|1\d|20)\b|\bS(?:[0-9]|10)\b|\bQ6\b|no_matching_transition|msg_id|rule_hits|rebuttal|verdict|safe_rejected|external_unavailable|system_error|practice_guide|guide_md|guide_intro|template_id|query_authority|standard_sql|\bfamily\b|step_up|step_down|injected_for_demo|\bAgent\b|\bLLM\b|智能体|状态机|SELECT|answer_key|答案键|expected_rows|expected_points/iu,
    )
  })

  it('shows one honest notice instead of inventing steps when guide structure is missing', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('practice_guide', {
          difficulty: 'basic',
          question: '核对本月计划执行情况。',
          guide_intro: '先确认统计范围。',
          guide_steps: ['只提供了一条不完整步骤。'],
          completion_criteria: [],
        }),
      },
    })

    const guide = wrapper.get('.practice-guide-card')
    expect(guide.get('.guide-unavailable').text())
      .toBe('这份实操指南暂时无法展示，请稍后再试。')
    expect(guide.find('.guide-question').exists()).toBe(false)
    expect(guide.find('.guide-intro').exists()).toBe(false)
    expect(guide.find('.guide-steps').exists()).toBe(false)
    expect(guide.find('.guide-criteria').exists()).toBe(false)
  })

  it('accepts only an atomic complete guide shape', () => {
    const completeMarkdown = [
      '## 实操目标',
      '核对本月计划执行情况。',
      '## 开始前',
      '先确认统计范围。',
      '## 操作步骤',
      '1. 确认月份。',
      '2. 整理数据。',
      '3. 核对结果。',
      '## 完成标准',
      '- 结果覆盖题面要求。',
    ].join('\n')
    const incompleteGuides = [
      {
        difficulty: 'basic',
        question: '核对本月计划执行情况。',
        guide_steps: ['确认月份。', '整理数据。', '核对结果。'],
        completion_criteria: ['结果覆盖题面要求。'],
      },
      {
        difficulty: 'basic',
        guide_md: completeMarkdown.replace('## 开始前\n先确认统计范围。\n', ''),
      },
      {
        difficulty: 'basic',
        question: '核对本月计划执行情况。',
        guide_intro: '先确认统计范围。',
        guide_steps: ['确认月份。', '整理数据。', '核对结果。'],
        guide_md: completeMarkdown,
      },
      {
        difficulty: 'expert',
        question: '核对本月计划执行情况。',
        guide_intro: '先确认统计范围。',
        guide_steps: ['确认月份。', '整理数据。', '核对结果。'],
        completion_criteria: ['结果覆盖题面要求。'],
      },
    ]

    for (const content of incompleteGuides) {
      const wrapper = mount(ResourcePanel, {
        props: { view: taskView('practice_guide', content) },
      })
      const guide = wrapper.get('.practice-guide-card')
      expect(guide.get('.guide-unavailable').text())
        .toBe('这份实操指南暂时无法展示，请稍后再试。')
      expect(guide.find('.guide-steps').exists()).toBe(false)
      expect(guide.find('.guide-criteria').exists()).toBe(false)
    }
  })

  it('keeps quiz sets as practice questions without rendering an empty guide shell', () => {
    const wrapper = mount(ResourcePanel, {
      props: {
        view: taskView('quiz_set', {
          difficulty: 'basic',
          question: '分别查询计划量与实际完成量。',
        }),
      },
    })

    const quiz = wrapper.get('.quiz-card')
    expect(quiz.get('.resource-eyebrow').text()).toContain('练习题')
    expect(quiz.get('h3').text()).toBe('分别查询计划量与实际完成量。')
    expect(wrapper.find('.practice-guide-card').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('实操指南')
  })
})
