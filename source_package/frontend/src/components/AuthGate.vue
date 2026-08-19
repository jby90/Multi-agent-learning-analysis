<script setup lang="ts">
import { GraduationCap, Lock, ShieldCheck } from '@lucide/vue'
import { computed, ref, watch } from 'vue'

import PasswordInput from './PasswordInput.vue'
import WaveText from './WaveText.vue'
import {
  AUTH_TOKEN_KEY,
  createAuthApi,
  AuthApiError,
  type AuthSession,
} from '../lib/authApi'

const emit = defineEmits<{
  authenticated: [value: AuthSession]
  /** 0819 bug7：跳过登录，以游客身份直接进入（学习记录不归档） */
  skip: []
}>()

const props = withDefaults(defineProps<{
  api?: ReturnType<typeof createAuthApi>
}>(), {
  api: undefined,
})
const api = props.api ?? createAuthApi()

type RoleChoice = 'student' | 'admin'
type StudentTab = 'login' | 'register'

const role = ref<RoleChoice | null>(null)
const studentTab = ref<StudentTab>('login')

// ---- 学员登录 ----
const loginPhone = ref('')
const loginPassword = ref('')
const loginError = ref('')
const loginBusy = ref(false)

// ---- 学员注册 ----
const regPhone = ref('')
const regUsername = ref('')
const regPassword = ref('')
const regConfirm = ref('')
const regPhoneState = ref<'idle' | 'checking' | 'taken' | 'available' | 'invalid'>('idle')
const regError = ref('')
const regBusy = ref(false)

const PHONE_RE = /^1\d{10}$/

const registerDisabled = computed(() => (
  regBusy.value
  || regPhoneState.value !== 'available'
  || !regUsername.value.trim()
  || regPassword.value.length < 6
  || regPassword.value !== regConfirm.value
))

const phoneHint = computed(() => {
  if (regPhoneState.value === 'taken') return '该手机号已注册'
  if (regPhoneState.value === 'invalid') return '请输入 11 位有效手机号'
  return ''
})

let checkTimer: number | undefined
watch(regPhone, (value) => {
  if (checkTimer !== undefined) window.clearTimeout(checkTimer)
  regPhoneState.value = 'idle'
  if (!value) return
  if (!PHONE_RE.test(value)) {
    regPhoneState.value = 'invalid'
    return
  }
  regPhoneState.value = 'checking'
  checkTimer = window.setTimeout(async () => {
    try {
      const result = await api.checkPhone(value)
      regPhoneState.value = result.registered ? 'taken' : 'available'
    } catch {
      regPhoneState.value = 'idle'
    }
  }, 400)
})

async function studentLogin(): Promise<void> {
  loginError.value = ''
  if (!PHONE_RE.test(loginPhone.value) || !loginPassword.value) {
    loginError.value = '请输入手机号和密码。'
    return
  }
  loginBusy.value = true
  try {
    const session = await api.login(loginPhone.value, loginPassword.value)
    sessionStorage.setItem(AUTH_TOKEN_KEY, session.token)
    emit('authenticated', session)
  } catch (error) {
    loginError.value = error instanceof AuthApiError
      ? error.message
      : '登录失败，请稍后再试。'
  } finally {
    loginBusy.value = false
  }
}

async function studentRegister(): Promise<void> {
  regError.value = ''
  regBusy.value = true
  try {
    const session = await api.register({
      phone: regPhone.value,
      username: regUsername.value.trim(),
      password: regPassword.value,
      confirm: regConfirm.value,
    })
    sessionStorage.setItem(AUTH_TOKEN_KEY, session.token)
    emit('authenticated', session)
  } catch (error) {
    regError.value = error instanceof AuthApiError
      ? error.message
      : '注册失败，请稍后再试。'
  } finally {
    regBusy.value = false
  }
}

// ---- 管理员登录 ----
const adminUsername = ref('admin')
const adminPassword = ref('')
const adminError = ref('')
const adminBusy = ref(false)

async function adminLogin(): Promise<void> {
  adminError.value = ''
  if (!adminUsername.value || !adminPassword.value) {
    adminError.value = '请输入用户名和密码。'
    return
  }
  adminBusy.value = true
  try {
    const session = await api.adminLogin(adminUsername.value, adminPassword.value)
    sessionStorage.setItem(AUTH_TOKEN_KEY, session.token)
    emit('authenticated', session)
  } catch (error) {
    adminError.value = error instanceof AuthApiError
      ? error.message
      : '登录失败，请稍后再试。'
  } finally {
    adminBusy.value = false
  }
}

function backToRoles(): void {
  role.value = null
  loginError.value = ''
  regError.value = ''
  adminError.value = ''
}
</script>

<template>
  <main class="auth-gate" aria-label="系统登录">
    <div class="auth-gate-card">
      <header class="auth-gate-brand">
        <span class="auth-gate-mark">智</span>
        <div>
          <strong>智能岗位学习中心</strong>
          <small>船厂数字化岗位培训</small>
        </div>
        <!-- 0819 bug7 补：右上角"跳过"仅在学员登录/注册页显示，
             角色选择页与管理员页不再出现 -->
        <button
          v-if="role === 'student'"
          type="button"
          class="auth-gate-skip"
          aria-label="跳过登录，以游客身份进入"
          @click="emit('skip')"
        >跳过</button>
      </header>

      <!-- 第一步：角色选择 -->
      <section v-if="!role" class="auth-role-grid" aria-label="选择角色">
        <button type="button" class="auth-role-card" @click="role = 'student'; studentTab = 'login'">
          <GraduationCap :size="26" aria-hidden="true" />
          <strong>学员</strong>
          <small>进入岗位实操训练</small>
        </button>
        <button type="button" class="auth-role-card" @click="role = 'admin'">
          <ShieldCheck :size="26" aria-hidden="true" />
          <strong>系统管理员</strong>
          <small>查看全系统运行</small>
        </button>
      </section>

      <!-- 学员：登录 / 注册 -->
      <section v-else-if="role === 'student'" class="auth-form">
        <button type="button" class="auth-back" @click="backToRoles">← 返回角色选择</button>
        <div class="auth-tabs" role="tablist">
          <button
            type="button"
            role="tab"
            :aria-selected="studentTab === 'login'"
            :class="{ 'is-active': studentTab === 'login' }"
            @click="studentTab = 'login'"
          >登录</button>
          <button
            type="button"
            role="tab"
            :aria-selected="studentTab === 'register'"
            :class="{ 'is-active': studentTab === 'register' }"
            @click="studentTab = 'register'"
          >注册</button>
        </div>

        <form v-if="studentTab === 'login'" class="auth-fields" @submit.prevent="studentLogin">
          <label>
            <span>手机号</span>
            <input
              v-model="loginPhone"
              type="tel"
              maxlength="11"
              autocomplete="tel"
              placeholder="请输入注册手机号"
            >
          </label>
          <label>
            <span>密码</span>
            <PasswordInput
              v-model="loginPassword"
              autocomplete="current-password"
              placeholder="请输入密码"
            />
          </label>
          <p v-if="loginError" class="auth-error" role="alert">{{ loginError }}</p>
          <button type="submit" class="auth-submit" :disabled="loginBusy">
            <Lock v-if="loginBusy" :size="14" aria-hidden="true" />
            {{ loginBusy ? '' : '登录' }}<WaveText v-if="loginBusy" text="正在登录…" />
          </button>
        </form>

        <form v-else class="auth-fields" @submit.prevent="studentRegister">
          <label>
            <span>手机号</span>
            <input
              v-model="regPhone"
              type="tel"
              maxlength="11"
              autocomplete="tel"
              placeholder="请输入 11 位手机号"
            >
            <small
              class="auth-field-hint"
              :class="{ 'is-taken': regPhoneState === 'taken' }"
            >{{ phoneHint || '演示环境，请使用测试手机号' }}</small>
          </label>
          <label>
            <span>用户名</span>
            <input
              v-model="regUsername"
              type="text"
              maxlength="32"
              autocomplete="nickname"
              placeholder="1~32 个字符"
            >
          </label>
          <label>
            <span>密码</span>
            <PasswordInput
              v-model="regPassword"
              autocomplete="new-password"
              placeholder="至少 6 位"
            />
          </label>
          <label>
            <span>确认密码</span>
            <PasswordInput
              v-model="regConfirm"
              autocomplete="new-password"
              placeholder="再次输入密码"
            />
          </label>
          <p v-if="regError" class="auth-error" role="alert">{{ regError }}</p>
          <button type="submit" class="auth-submit" :disabled="registerDisabled">
            {{ regBusy ? '' : '注册并进入' }}<WaveText v-if="regBusy" text="正在注册…" />
          </button>
        </form>
      </section>

      <!-- 管理员：仅登录 -->
      <section v-else class="auth-form">
        <button type="button" class="auth-back" @click="backToRoles">← 返回角色选择</button>
        <p class="auth-admin-note">
          <ShieldCheck :size="14" aria-hidden="true" />
          管理员账号由部署配置预置，不支持注册。
        </p>
        <form class="auth-fields" @submit.prevent="adminLogin">
          <label>
            <span>用户名</span>
            <input v-model="adminUsername" type="text" autocomplete="username">
          </label>
          <label>
            <span>密码</span>
            <PasswordInput
              v-model="adminPassword"
              autocomplete="current-password"
              placeholder="请输入管理员密码"
            />
          </label>
          <p v-if="adminError" class="auth-error" role="alert">{{ adminError }}</p>
          <button type="submit" class="auth-submit" :disabled="adminBusy">
            {{ adminBusy ? '' : '登录' }}<WaveText v-if="adminBusy" text="正在登录…" />
          </button>
        </form>
      </section>

      <!-- 优化17：底部"学员训练数据与登录信息分开存储…"小字已删 -->
    </div>
  </main>
</template>
