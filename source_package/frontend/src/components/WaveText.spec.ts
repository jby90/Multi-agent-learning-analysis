import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import WaveText from './WaveText.vue'


describe('WaveText（优化15：等待提示波浪动画）', () => {
  it('逐字渲染并按序设置动画延时（含省略号），保留多行结构', () => {
    const wrapper = mount(WaveText, {
      props: { text: '请稍候…\n第二行' },
    })
    const chars = wrapper.findAll('.wave-char')
    expect(chars.map((node) => node.text()).join('')).toBe('请稍候…第二行')
    // 省略号也是字符，参与波浪
    expect(chars[3]!.text()).toBe('…')
    // 延时按序递增
    const first = Number(chars[0]!.attributes('style')?.match(/([\d.]+)ms/)?.[1] ?? -1)
    const second = Number(chars[1]!.attributes('style')?.match(/([\d.]+)ms/)?.[1] ?? -1)
    expect(second).toBeGreaterThan(first)
    // 多行：两行容器
    expect(wrapper.findAll('.wave-line')).toHaveLength(2)
    expect(wrapper.findAll('.wave-line.is-multiline')).toHaveLength(2)
  })

  it('空格不参与动画（is-space）', () => {
    const wrapper = mount(WaveText, { props: { text: 'a b' } })
    expect(wrapper.findAll('.wave-char.is-space')).toHaveLength(1)
  })
})
