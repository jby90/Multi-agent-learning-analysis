import { flushPromises, mount } from '@vue/test-utils'
import { readFileSync } from 'node:fs'
import { describe, expect, it, vi } from 'vitest'

import UserMenu from './UserMenu.vue'
import { AUTH_TOKEN_KEY, AuthApiError, type AuthApi } from '../lib/authApi'


function fakeApi(overrides: Record<string, unknown> = {}): AuthApi {
  return {
    checkPhone: vi.fn(async (_phone: string) => ({ registered: false })),
    register: vi.fn(async (_input: { phone: string; username: string; password: string; confirm: string }) => ({ user_id: 1, username: 'x', role: 'student' as const, token: 't' })),
    login: vi.fn(async (_phone: string, _password: string) => ({ user_id: 1, username: 'x', role: 'student' as const, token: 't' })),
    adminLogin: vi.fn(async (_u: string, _p: string) => ({ user_id: 2, username: 'admin', role: 'admin' as const, token: 'a' })),
    changePassword: vi.fn(async (_input: { token: string; old_password: string; new_password: string; confirm: string }) => ({ ok: true })),
    me: vi.fn(async (_token: string) => ({
      user_id: 7,
      username: '小陈',
      phone_masked: '138****5678',
      role: 'student' as const,
    })),
    ...overrides,
  }
}

async function openMenu(wrapper: ReturnType<typeof mount>) {
  await wrapper.get('.user-menu-trigger').trigger('click')
  await flushPromises()
}


describe('UserMenu', () => {
  it('student menu shows uid, masked phone, change password, logout', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    const wrapper = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api: fakeApi() },
    })
    await openMenu(wrapper)
    const text = wrapper.get('.user-menu-panel').text()
    expect(text).toContain('UID 7')
    expect(text).toContain('138****5678')
    expect(text).toContain('修改密码')
    expect(text).toContain('退出登录')
    expect(text).not.toContain('系统管理员')
    expect(text).not.toContain('13812345678')
  })

  it('admin menu shows badge and live sessions instead of phone', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'admin-tok')
    const wrapper = mount(UserMenu, {
      props: { username: 'admin', role: 'admin', api: fakeApi({
        me: vi.fn(async (_token: string) => ({ user_id: 2, username: 'admin', role: 'admin' as const, active_sessions: 3 })),
      }) },
    })
    await openMenu(wrapper)
    const text = wrapper.get('.user-menu-panel').text()
    expect(text).toContain('系统管理员')
    expect(text).toContain('在线学员会话')
    expect(text).toContain('3')
    expect(text).not.toContain('修改密码')
    expect(text).not.toContain('绑定手机')
  })

  it('avatar shows the first character of the username', () => {
    const wrapper = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api: fakeApi() },
    })
    expect(wrapper.get('.user-menu-avatar').text()).toBe('小')
  })

  /** 0818 需求 2：修改密码改为页面正中弹窗（Teleport→body，测试内置 stub 渲染原位） */
  async function openPasswordModal(wrapper: ReturnType<typeof mount>): Promise<void> {
    await openMenu(wrapper)
    const trigger = wrapper
      .findAll('.user-menu-action')
      .find((button) => button.text().includes('修改密码'))
    if (!trigger) throw new Error('修改密码入口缺失')
    await trigger.trigger('click')
    await flushPromises()
  }

  it('student can change password in the centered modal and success note appears', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    const api = fakeApi()
    const wrapper = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api },
      global: { stubs: { teleport: true } },
    })
    await openPasswordModal(wrapper)
    expect(wrapper.find('.user-modal').exists()).toBe(true)
    const inputs = wrapper.findAll('.user-menu-pwd input')
    await inputs[0].setValue('abc123')
    await inputs[1].setValue('newpass6')
    await inputs[2].setValue('newpass6')
    await wrapper.get('form.user-menu-pwd').trigger('submit')
    await flushPromises()
    expect(api.changePassword).toHaveBeenCalled()
    expect(wrapper.get('.user-menu-done').text()).toContain('密码已更新')
  })

  it('change password errors surface in red', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    const wrapper = mount(UserMenu, {
      props: {
        username: '小陈', role: 'student',
        api: fakeApi({
          changePassword: vi.fn(async () => {
            throw new AuthApiError('旧密码不正确。', 400)
          }),
        }),
      },
      global: { stubs: { teleport: true } },
    })
    await openPasswordModal(wrapper)
    const inputs = wrapper.findAll('.user-menu-pwd input')
    await inputs[0].setValue('wrong')
    await inputs[1].setValue('newpass6')
    await inputs[2].setValue('newpass6')
    await wrapper.get('form.user-menu-pwd').trigger('submit')
    await flushPromises()
    expect(wrapper.get('.auth-error').text()).toContain('旧密码不正确')
  })

  it('rejects a new password identical to the old one without calling the api', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    const api = fakeApi()
    const wrapper = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api },
      global: { stubs: { teleport: true } },
    })
    await openPasswordModal(wrapper)
    const inputs = wrapper.findAll('.user-menu-pwd input')
    await inputs[0].setValue('abc123')
    await inputs[1].setValue('abc123')
    await inputs[2].setValue('abc123')
    await wrapper.get('form.user-menu-pwd').trigger('submit')
    await flushPromises()
    expect(api.changePassword).not.toHaveBeenCalled()
    expect(wrapper.get('.auth-error').text()).toContain('新密码不能与旧密码相同')
    expect(wrapper.find('.user-menu-done').exists()).toBe(false)
  })

  it('learning records entry emits openRecords and closes the panel', async () => {
    const wrapper = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api: fakeApi() },
    })
    await openMenu(wrapper)
    const records = wrapper
      .findAll('.user-menu-action')
      .find((button) => button.text().includes('学习记录'))
    await records!.trigger('click')
    expect(wrapper.emitted('openRecords')).toHaveLength(1)
    expect(wrapper.find('.user-menu-panel').exists()).toBe(false)
  })

  it('export emits exportTrace; import file emits importTrace (admin sees import, student does not)', async () => {
    const admin = mount(UserMenu, {
      props: { username: '管', role: 'admin', api: fakeApi(), canExport: true },
    })
    await openMenu(admin)
    await admin
      .findAll('.user-menu-action')
      .find((button) => button.text().includes('导出会话'))!
      .trigger('click')
    expect(admin.emitted('exportTrace')).toHaveLength(1)

    const input = admin.get('input[aria-label="导入会话轨迹文件"]')
    const file = new File(['{}'], 'demo.jsonl', { type: 'application/x-ndjson' })
    Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
    await input.trigger('change')
    expect(admin.emitted('importTrace')).toHaveLength(1)

    const student = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api: fakeApi(), canExport: true },
    })
    await openMenu(student)
    expect(
      student.findAll('.user-menu-action').some((button) => button.text().includes('导入会话')),
    ).toBe(false)
  })

  it('logout clears the token and emits logout', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'tok')
    const wrapper = mount(UserMenu, {
      props: { username: '小陈', role: 'student', api: fakeApi() },
    })
    await openMenu(wrapper)
    await wrapper.get('.is-logout').trigger('click')
    expect(sessionStorage.getItem(AUTH_TOKEN_KEY)).toBeNull()
    expect(wrapper.emitted('logout')).toBeTruthy()
  })

  it('an invalid session on open triggers logout', async () => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, 'expired')
    const wrapper = mount(UserMenu, {
      props: {
        username: '小陈', role: 'student',
        api: fakeApi({
          me: vi.fn(async (_token: string) => {
            throw new AuthApiError('登录已过期，请重新登录。', 401)
          }),
        }),
      },
    })
    await openMenu(wrapper)
    await flushPromises()
    expect(wrapper.emitted('logout')).toBeTruthy()
  })

  it('styles.css anchors the dropdown: .user-menu is positioned, panel is absolute', () => {
    // 回归守卫：面板 position:absolute 依赖组件根 .user-menu 提供 position:relative
    // 锚点。锚点丢失时面板会相对整页定位、漂到屏幕外，而 happy-dom 不算布局，
    // DOM 断言照样通过 —— 所以必须直接读样式表断言规则存在。
    const css = readFileSync('src/styles.css', 'utf-8')
      .replace(/\s+/g, '')
    const rootRule = css.match(/\.user-menu\{[^}]*\}/)
    expect(rootRule, '缺少 .user-menu 根规则（下拉锚点）').toBeTruthy()
    expect(rootRule![0]).toContain('position:relative')
    const panelRule = css.match(/\.user-menu-panel\{[^}]*\}/)
    expect(panelRule, '缺少 .user-menu-panel 规则').toBeTruthy()
    expect(panelRule![0]).toContain('position:absolute')

    // 第二个坑（2026-08-16 无头浏览器实测）：apple-interface.css 的
    // .workshop-header 卡片 overflow:hidden 会把伸出卡片下边缘的下拉面板
    // 拦腰裁断。卡片必须放开，圆角裁剪下沉到 .session-commandbar。
    // （先剥注释再断言，避免源码注释里的说明文字误伤匹配。）
    const apple = readFileSync('src/apple-interface.css', 'utf-8')
      .replace(/\/\*[\s\S]*?\*\//g, '')
      .replace(/\s+/g, '')
    const headerRule = apple.match(/\.workshop-header\{[^}]*\}/)
    expect(headerRule, '缺少 .workshop-header 规则').toBeTruthy()
    expect(headerRule![0]).toContain('overflow:visible')
    expect(headerRule![0]).not.toContain('overflow:hidden')
    const cmdRule = apple.match(/\.session-commandbar\{[^}]*\}/)
    expect(cmdRule, '缺少 .session-commandbar 圆角裁剪规则（深色数据条）').toBeTruthy()
    expect(cmdRule![0]).toContain('overflow:hidden')
    expect(cmdRule![0]).toContain('border-radius:')

    // 第三个坑（2026-08-16 无头浏览器实测归因）：点"修改密码"时，真实浏览器
    // 在目标阶段处理器与 document 冒泡监听之间执行微任务检查点，Vue 摘除被点
    // 按钮（v-if 换表单），root.contains(游离节点)=false → 面板被误关。
    // 必须用 composedPath() 判断；happy-dom 同步分发测不出该竞争，故源码守卫。
    const src = readFileSync('src/components/UserMenu.vue', 'utf-8')
    expect(src).toContain('composedPath()')
    const onDocClick = src.match(/function onDocClick[\s\S]*?\n}/)![0]
    expect(onDocClick).not.toContain('contains(event.target')
  })
})
