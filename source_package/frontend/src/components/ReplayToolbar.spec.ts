import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import ReplayToolbar from './ReplayToolbar.vue'


function mountToolbar() {
  return mount(ReplayToolbar, {
    props: {
      traces: [{ fileName: 'demo.jsonl', label: '岗位培养会话' }],
      selectedFile: 'demo.jsonl',
      playing: false,
      cursor: 1,
      total: 3,
      speed: 1,
      keyframes: [],
      comparison: false,
      canCompare: false,
      entryMode: 'replay',
      viewMode: 'student',
    },
  })
}


describe('ReplayToolbar trace transfer', () => {
  it('keeps the live landing navigation focused until a training session exists', async () => {
    const wrapper = mountToolbar()

    await wrapper.setProps({ entryMode: 'live', hasSession: false })
    expect(wrapper.text()).toContain('实时实操 · 数据截至 2025-07-31')
    expect(wrapper.find('.session-commandbar').exists()).toBe(false)
    expect(wrapper.find('.audience-switch').exists()).toBe(false)

    await wrapper.setProps({ hasSession: true })
    expect(wrapper.find('.audience-switch').exists()).toBe(true)
  })

  it('emits the actual JSONL file selected by the learner', async () => {
    const wrapper = mountToolbar()
    const input = wrapper.get('input[aria-label="导入会话记录"]')
    const file = new File(['{}'], 'training.jsonl', { type: 'application/x-ndjson' })
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })

    await input.trigger('change')

    expect(wrapper.emitted('import')?.[0]?.[0]).toBe(file)
  })

  it('accepts a dropped JSONL file and exposes export as a real button event', async () => {
    const wrapper = mountToolbar()
    const file = new File(['{}'], 'dropped.jsonl', { type: 'application/x-ndjson' })

    await wrapper.get('[data-testid="trace-transfer"]').trigger('drop', {
      dataTransfer: { files: [file] },
    })
    await wrapper.get('button[aria-label="导出当前会话"]').trigger('click')

    expect(wrapper.emitted('import')?.[0]?.[0]).toBe(file)
    expect(wrapper.emitted('export')).toHaveLength(1)
  })
})
