import { flushPromises, mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

import AuthGate from './AuthGate.vue'
import {
  AUTH_TOKEN_KEY,
  AuthApiError,
  type AuthApi,
  type AuthSession,
} from '../lib/authApi'


function fakeApi(overrides: Partial<Record<string, () => Promise<unknown>>> = {}): AuthApi {
  return {
    checkPhone: vi.fn(async (_phone: string) => ({ registered: false })),
    register: vi.fn(async (_input: { phone: string; username: string; password: string; confirm: string }) => ({
      user_id: 1, username: '新学员', role: 'student' as const, token: 'tok-1',
    })),
    login: vi.fn(async (_phone: string, _password: string) => ({
      user_id: 1, username: '老学员', role: 'student' as const, token: 'tok-2',
    })),
    adminLogin: vi.fn(async (_username: string, _password: string) => ({
      user_id: 2, username: 'admin', role: 'admin' as const, token: 'admin-tok',
    })),
    changePassword: vi.fn(async (_input: { token: string; old_password: string; new_password: string; confirm: string }) => ({ ok: true })),
    me: vi.fn(async (_token: string) => ({ user_id: 1, username: 'x', role: 'student' as const })),
    ...overrides,
  }
}

function mountGate(api: ReturnType<typeof fakeApi>) {
  return mount(AuthGate, { props: { api } })
}


describe('AuthGate', () => {
  // 0819 bug7 补：右上角"跳过"仅在学员登录/注册页显示
  it('skip appears only on the student login/register view', async () => {
    const wrapper = mountGate(fakeApi())
    const skip = () => wrapper.find('button[aria-label="跳过登录，以游客身份进入"]')

    // 角色选择页：无跳过
    expect(skip().exists()).toBe(false)

    // 学员登录页：出现；切注册 tab 仍在
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    expect(skip().exists()).toBe(true)
    await wrapper.findAll('.auth-tabs button')[1].trigger('click')
    expect(skip().exists()).toBe(true)

    // 返回角色选择 → 管理员页：无跳过
    await wrapper.get('.auth-back').trigger('click')
    await wrapper.findAll('.auth-role-card')[1].trigger('click')
    expect(skip().exists()).toBe(false)
  })

  it('skip emits the skip event', async () => {
    const wrapper = mountGate(fakeApi())
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    await wrapper.get('button[aria-label="跳过登录，以游客身份进入"]').trigger('click')
    expect(wrapper.emitted('skip')).toHaveLength(1)
  })

  it('shows two role cards first and no forms before choosing', () => {
    const wrapper = mountGate(fakeApi())
    expect(wrapper.findAll('.auth-role-card')).toHaveLength(2)
    expect(wrapper.find('form').exists()).toBe(false)
  })

  it('student card opens login/register tabs; admin card has no register tab', async () => {
    const wrapper = mountGate(fakeApi())
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    expect(wrapper.findAll('.auth-tabs button').map((b) => b.text())).toEqual(['登录', '注册'])
    await wrapper.findAll('.auth-tabs button')[1].trigger('click')
    expect(wrapper.find('input[autocomplete="new-password"]').exists()).toBe(true)

    await wrapper.get('.auth-back').trigger('click')
    await wrapper.findAll('.auth-role-card')[1].trigger('click')
    expect(wrapper.find('.auth-tabs').exists()).toBe(false)
    expect(wrapper.find('input[autocomplete="new-password"]').exists()).toBe(false)
    expect(wrapper.text()).toContain('不支持注册')
  })

  it('flags a taken phone with red hint and disables the register button', async () => {
    const api = fakeApi({
      checkPhone: vi.fn(async () => ({ registered: true })),
    })
    const wrapper = mountGate(api)
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    await wrapper.findAll('.auth-tabs button')[1].trigger('click')
    const phone = wrapper.get('input[type="tel"]')
    await phone.setValue('13812345678')
    await new Promise((r) => setTimeout(r, 500))
    const hint = wrapper.get('.auth-field-hint')
    expect(hint.text()).toContain('该手机号已注册')
    expect(hint.classes()).toContain('is-taken')
    expect(wrapper.get('.auth-submit').attributes('disabled')).toBeDefined()
  })

  it('invalid phone shape is rejected before any request', async () => {
    const api = fakeApi()
    const wrapper = mountGate(api)
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    await wrapper.findAll('.auth-tabs button')[1].trigger('click')
    await wrapper.get('input[type="tel"]').setValue('123')
    await new Promise((r) => setTimeout(r, 20))
    expect(wrapper.get('.auth-field-hint').text()).toContain('11 位有效手机号')
    expect(api.checkPhone).not.toHaveBeenCalled()
  })

  it('login failure shows the unified red message', async () => {
    const api = fakeApi({
      login: vi.fn(async () => {
        throw new AuthApiError('手机号或密码错误。', 401)
      }),
    })
    const wrapper = mountGate(api)
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    await wrapper.get('input[type="tel"]').setValue('13812345678')
    await wrapper.get('input[type="password"]').setValue('badpass')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const error = wrapper.get('.auth-error')
    expect(error.text()).toBe('手机号或密码错误。')
  })

  it('successful register stores the token and emits the session', async () => {
    const api = fakeApi()
    const wrapper = mountGate(api)
    await wrapper.findAll('.auth-role-card')[0].trigger('click')
    await wrapper.findAll('.auth-tabs button')[1].trigger('click')
    await wrapper.get('input[type="tel"]').setValue('13812345678')
    await new Promise((r) => setTimeout(r, 500))
    const inputs = wrapper.findAll('input')
    await inputs[1].setValue('小陈')
    await inputs[2].setValue('abc123')
    await inputs[3].setValue('abc123')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(sessionStorage.getItem(AUTH_TOKEN_KEY)).toBe('tok-1')
    const emitted = wrapper.emitted('authenticated')
    expect(emitted).toBeTruthy()
    expect((emitted![0][0] as AuthSession).username).toBe('新学员')
  })

  it('admin login emits an admin session', async () => {
    const api = fakeApi()
    const wrapper = mountGate(api)
    await wrapper.findAll('.auth-role-card')[1].trigger('click')
    await wrapper.get('input[type="password"]').setValue('admin123456')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const emitted = wrapper.emitted('authenticated')
    expect((emitted![0][0] as AuthSession).role).toBe('admin')
  })
})
