import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import type { InteractiveResourceBundle } from '../lib/interactiveApi'
import ResourceBundleStrip from './ResourceBundleStrip.vue'


const bundle: InteractiveResourceBundle = {
  bundle_id: 'rb-1234567890abcdef',
  contract_id: 'lc-123',
  evidence_bundle_id: 'eb-123',
  branches: [
    {
      branch_id: 'knowledge',
      status: 'ready',
      required: true,
      payload_type: 'lecture_note',
      difficulty: 'basic',
    },
    {
      branch_id: 'practice',
      status: 'ready',
      required: false,
      payload_type: 'practice_guide',
      difficulty: 'basic',
    },
    {
      branch_id: 'assessment',
      status: 'ready',
      required: false,
      payload_type: 'quiz_set',
      difficulty: 'basic',
    },
  ],
}

describe('ResourceBundleStrip', () => {
  it('keeps orchestration evidence available behind a compact learner summary', () => {
    const wrapper = mount(ResourceBundleStrip, { props: { bundle } })

    expect(wrapper.get('summary').text()).toContain('微课、实操与测验已准备')
    expect(wrapper.get('summary').text()).toContain('3/3 已就绪')
    expect(wrapper.get('details').attributes('open')).toBeUndefined()
    expect(wrapper.text()).toContain('岗位微课')
    expect(wrapper.text()).toContain('实操任务')
    expect(wrapper.text()).toContain('分阶测验')
    expect(wrapper.text()).toContain('三项内容基于同一学习目标')
    expect(wrapper.findAll('article')).toHaveLength(3)
  })
})
