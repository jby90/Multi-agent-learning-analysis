import { mount } from '@vue/test-utils'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { isLearnerSafeText } from '../lib/tracePresentation'
import DataCollisionMoment from './DataCollisionMoment.vue'


describe('DataCollisionMoment', () => {
  afterEach(() => {
    vi.useRealTimers()
  })

  it('flips from the mistaken number to the verified number and finishes within three seconds', async () => {
    vi.useFakeTimers()
    const wrapper = mount(DataCollisionMoment, {
      props: {
        misconception: 'M-01',
        wrongLabel: '计划量',
        wrongValue: '1855.06',
        correctLabel: '实际完成量',
        correctValue: '1156.87',
      },
    })

    expect(wrapper.get('[data-testid="collision-moment"]').attributes('data-stage')).toBe('mistaken')
    expect(wrapper.text()).toContain('1855.06')
    expect(wrapper.get('h2').text()).toBe('数据验证')
    expect(wrapper.text()).not.toContain('真实数据校正')
    expect(wrapper.text()).not.toContain('数据撞脸')
    expect(wrapper.text()).not.toContain('1156.87')
    expect(wrapper.findAll('.digit-tile')).toHaveLength(0)

    await vi.advanceTimersByTimeAsync(800)
    expect(wrapper.get('[data-testid="collision-moment"]').attributes('data-stage')).toBe('verified')
    expect(wrapper.text()).toContain('1156.87')
    expect(wrapper.findAll('.digit-tile')).toHaveLength(7)
    expect(wrapper.get('.collision-confirmed-label').text())
      .toBe('数据证实 · 实际完成量')
    expect(wrapper.find('.collision-result-card > .collision-confirmed-label').exists()).toBe(true)
    expect(wrapper.find('.collision-flip > .collision-confirmed-label').exists()).toBe(false)
    expect(wrapper.find('.collision-back > .collision-confirmed-label').exists()).toBe(false)
    expect(wrapper.get('.collision-number.is-mistaken').classes()).toContain('is-struck')
    expect(wrapper.get('.collision-correction').text()).toContain('已修正 计划量与实际量的区分')
    expect(wrapper.get('.collision-correction').text()).not.toContain('M-01')

    await vi.advanceTimersByTimeAsync(2200)
    expect(wrapper.find('[data-testid="collision-moment"]').exists()).toBe(false)
    expect(wrapper.emitted('complete')).toHaveLength(1)
  })

  it('lets the learner skip the signature animation immediately', async () => {
    vi.useFakeTimers()
    const wrapper = mount(DataCollisionMoment, {
      props: {
        misconception: 'M-01',
        wrongLabel: '计划量',
        wrongValue: '1855.06',
        correctLabel: '实际完成量',
        correctValue: '1156.87',
      },
    })

    await wrapper.get('button[aria-label="跳过数据验证动画"]').trigger('click')

    expect(wrapper.find('[data-testid="collision-moment"]').exists()).toBe(false)
    expect(wrapper.emitted('complete')).toHaveLength(1)
  })

  it('fails closed when dynamic comparison fields contain implementation text', async () => {
    vi.useFakeTimers()
    const wrapper = mount(DataCollisionMoment, {
      props: {
        misconception: 'M-99',
        wrongLabel: 'msg_id',
        wrongValue: 'HTTP 500',
        correctLabel: 'ruleHits',
        correctValue: 'S4_VERIFY',
      },
    })

    await vi.advanceTimersByTimeAsync(800)

    expect(isLearnerSafeText(wrapper.text())).toBe(true)
    expect(wrapper.text()).toContain('当前内容暂时无法展示，请稍后再试。')
    expect(wrapper.text()).toContain('—')
  })
})
