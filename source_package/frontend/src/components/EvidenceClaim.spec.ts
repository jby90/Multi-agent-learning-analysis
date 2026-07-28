import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import EvidenceClaim from './EvidenceClaim.vue'


describe('EvidenceClaim', () => {
  it('underlines a factual claim and exposes its source on hover or focus', () => {
    const wrapper = mount(EvidenceClaim, {
      props: {
        text: '计划量与实际量必须分开理解。',
        knowledgePoint: '完成率计算',
        matchesBlindSpot: true,
        claims: [{ text: '计划量与实际量必须分开理解。', kind: 'fact' }],
        evidence: [{
          kind: 'kb_chunk',
          ref: 'KB-002',
          quote: '**计划量**是`plan_qty`记录的工作量。',
          supportsClaim: '计划量与实际量必须分开理解。',
        }],
      },
    })

    expect(wrapper.find('.claim-anchor').attributes('tabindex')).toBe('0')
    expect(wrapper.find('[role="tooltip"]').text()).toBe(
      '出自《完成率计算》知识点与你的盲区匹配',
    )
    expect(wrapper.find('[role="tooltip"]').text()).not.toMatch(/KB-002|资料|检索|命中|plan_qty/)
    expect(wrapper.get('.evidence-marker').text()).toBe('1')
    expect(wrapper.get('.evidence-marker').attributes('aria-label')).toBe(
      '查看《完成率计算》知识出处',
    )
  })

  it('adds the purple truth label to speculation', () => {
    const wrapper = mount(EvidenceClaim, {
      props: {
        text: '后续可能出现传导影响。',
        claims: [{ text: '后续可能出现传导影响。', kind: 'speculation' }],
        evidence: [],
      },
    })

    expect(wrapper.find('.speculation-badge').text()).toBe('推断')
  })
})
