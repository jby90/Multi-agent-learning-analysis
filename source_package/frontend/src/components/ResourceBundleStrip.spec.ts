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
  it('presents the three contract-bound resources without exposing draft content', () => {
    const wrapper = mount(ResourceBundleStrip, { props: { bundle } })

    expect(wrapper.text()).toContain('三路教学资源已汇聚')
    expect(wrapper.text()).toContain('岗位微课')
    expect(wrapper.text()).toContain('实操任务')
    expect(wrapper.text()).toContain('分阶测验')
    expect(wrapper.text()).toContain('同一学习契约')
    expect(wrapper.text()).toContain('独立生成 · 确定性汇聚')
    expect(wrapper.findAll('article')).toHaveLength(3)
  })
})
