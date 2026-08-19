import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'

import PasswordInput from './PasswordInput.vue'


describe('PasswordInput', () => {
  it('masks by default and reveals after toggling', async () => {
    const wrapper = mount(PasswordInput, {
      props: { modelValue: 'secret1', autocomplete: 'current-password' },
    })
    const input = wrapper.get('input')
    expect(input.attributes('type')).toBe('password')
    expect(input.attributes('autocomplete')).toBe('current-password')

    await wrapper.get('.pwd-toggle').trigger('click')
    expect(input.attributes('type')).toBe('text')
    expect(wrapper.get('.pwd-toggle').attributes('aria-label')).toBe('隐藏密码')
    expect(wrapper.get('.pwd-toggle').attributes('aria-pressed')).toBe('true')

    await wrapper.get('.pwd-toggle').trigger('click')
    expect(input.attributes('type')).toBe('password')
    expect(wrapper.get('.pwd-toggle').attributes('aria-label')).toBe('显示密码')
  })

  it('emits update:modelValue on typing', async () => {
    const wrapper = mount(PasswordInput, {
      props: { modelValue: '' },
    })
    await wrapper.get('input').setValue('abc123')
    expect(wrapper.emitted('update:modelValue')![0]).toEqual(['abc123'])
  })

  it('toggles are independent per instance', async () => {
    const wrapper = mount({
      components: { PasswordInput },
      template: '<div><PasswordInput model-value="a" /><PasswordInput model-value="b" /></div>',
    })
    const toggles = wrapper.findAll('.pwd-toggle')
    const inputs = wrapper.findAll('input')
    await toggles[0].trigger('click')
    expect(inputs[0].attributes('type')).toBe('text')
    expect(inputs[1].attributes('type')).toBe('password')
  })
})
