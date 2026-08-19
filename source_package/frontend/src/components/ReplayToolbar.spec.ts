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

    await wrapper.setProps({ entryMode: 'live', hasSession: false, authRole: 'admin' })
    // 实训顶栏"训练数据"块已按需求删除：live 模式不再展示数据范围。
    expect(wrapper.find('[aria-label="当前训练数据范围"]').exists()).toBe(false)
    expect(wrapper.text()).not.toContain('H2601')
    expect(wrapper.find('.session-commandbar').exists()).toBe(false)
    expect(wrapper.find('.audience-switch').exists()).toBe(false)

    await wrapper.setProps({ hasSession: true })
    expect(wrapper.find('.audience-switch').exists()).toBe(true)
  })

  it('slot-hides the audience switch in comparison view (button positions stay put)', async () => {
    // 优化1：三画像下学员/协同切换占位隐藏（visibility）——按钮不位移、不可点；
    // 单画像恢复可见。此前摘除式隐藏会让整排按钮左右移动。
    const wrapper = mountToolbar()
    await wrapper.setProps({ entryMode: 'replay', hasSession: true, comparison: false, authRole: 'admin' })
    const audience = wrapper.get('.audience-switch')
    expect(audience.classes()).not.toContain('is-slot-hidden')

    await wrapper.setProps({ comparison: true })
    expect(audience.classes()).toContain('is-slot-hidden')

    await wrapper.setProps({ comparison: false })
    expect(audience.classes()).not.toContain('is-slot-hidden')
  })

  it('shows the entry switch for admins only (guests treated as students)', async () => {
    // 优化20：入口切换仅 admin 可见——游客（null）与学员一致固定实操通道
    const wrapper = mountToolbar()
    expect(wrapper.find('.entry-switch').exists()).toBe(false)

    await wrapper.setProps({ authRole: 'admin' })
    expect(wrapper.find('.entry-switch').exists()).toBe(true)

    await wrapper.setProps({ authRole: 'student' })
    expect(wrapper.find('.entry-switch').exists()).toBe(false)
  })

  it('no longer hosts import/export controls (moved into the user menu)', async () => {
    // 0818 需求 1：导入/导出移入头像下拉——工具栏不再承载
    const wrapper = mountToolbar()
    expect(wrapper.find('[data-testid="trace-transfer"]').exists()).toBe(false)
    expect(wrapper.find('button[aria-label="导出当前会话"]').exists()).toBe(false)
    expect(wrapper.find('input[aria-label="导入会话记录"]').exists()).toBe(false)
  })
})
