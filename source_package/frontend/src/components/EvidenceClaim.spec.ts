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

    // 需求②：证据句为普通文本——无锚点/下划线/角标/弹层
    expect(wrapper.find('.claim-anchor').exists()).toBe(false)
    expect(wrapper.find('.evidence-marker').exists()).toBe(false)
    expect(wrapper.find('.evidence-popover').exists()).toBe(false)
    expect(wrapper.text()).toContain('计划量与实际量必须分开理解')
    expect(wrapper.text()).not.toMatch(/出自|盲区|KB-002|检索|命中|plan_qty/)
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
