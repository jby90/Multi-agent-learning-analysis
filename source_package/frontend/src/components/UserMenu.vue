<script setup lang="ts">
import { ChevronDown, Download, History, KeyRound, LogOut, Upload } from '@lucide/vue'
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import PasswordInput from './PasswordInput.vue'
import WaveText from './WaveText.vue'
import {
  AUTH_TOKEN_KEY,
  createAuthApi,
  AuthApiError,
  type AuthProfile,
  type AuthRole,
} from '../lib/authApi'

const props = withDefaults(defineProps<{
  username: string
  role: AuthRole
  api?: ReturnType<typeof createAuthApi>
  /** 0818 需求 1：导出当前会话可用（工具栏原按钮移入下拉） */
  canExport?: boolean
}>(), {
  api: undefined,
  canExport: false,
})
const api = props.api ?? createAuthApi()

const emit = defineEmits<{
  logout: []
  /** 0818 需求 1：导入/导出回放轨迹（原工具栏按钮移入下拉） */
  importTrace: [file: File]
  exportTrace: []
  /** 0818 需求 5：打开学习记录页 */
  openRecords: []
}>()

const open = ref(false)
const profile = ref<AuthProfile | null>(null)
const root = ref<HTMLElement | null>(null)
const importInput = ref<HTMLInputElement | null>(null)

const initials = computed(() => props.username.trim().charAt(0) || '学')
const avatarClass = computed(() => {
  let hash = 0
  for (const character of props.username) {
    hash = (hash * 31 + character.codePointAt(0)!) % 8
  }
  return `auth-avatar-tone-${hash}`
})

function toggle(): void {
  open.value = !open.value
  if (open.value) void refreshProfile()
}

async function refreshProfile(): Promise<void> {
  const token = sessionStorage.getItem(AUTH_TOKEN_KEY)
  if (!token) return
  try {
    profile.value = await api.me(token)
  } catch (error) {
    if (error instanceof AuthApiError && error.status === 401) {
      emit('logout')
    }
  }
}

function onDocClick(event: MouseEvent): void {
  if (!open.value || !root.value) return
  // 必须用 composedPath() 而非 root.contains(target)：点击面板内 v-if 切换的按钮
  // 时 Vue 会立即摘除被点节点，contains(游离节点)=false 会被误判为"点击外部"。
  // composedPath() 是事件分发开始时拍下的路径快照，不受中途 DOM 摘除影响。
  if (event.composedPath().includes(root.value)) return
  open.value = false
}

onMounted(() => document.addEventListener('click', onDocClick))
onBeforeUnmount(() => document.removeEventListener('click', onDocClick))

// ---- 导入/导出（原工具栏按钮移入下拉，0818 需求 1） ----
function pickImport(): void {
  open.value = false
  importInput.value?.click()
}

function onImportChange(event: Event): void {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  if (file) emit('importTrace', file)
  input.value = ''
}

function exportTrace(): void {
  open.value = false
  emit('exportTrace')
}

// ---- 学习记录页（0818 需求 5） ----
function openRecords(): void {
  open.value = false
  emit('openRecords')
}

// ---- 修改密码：页面正中弹窗（0818 需求 2，原为面板内嵌表单） ----
const pwdOpen = ref(false)
const pwdOld = ref('')
const pwdNew = ref('')
const pwdConfirm = ref('')
const pwdError = ref('')
const pwdDone = ref(false)
const pwdBusy = ref(false)

const pwdDisabled = computed(() => (
  pwdBusy.value || pwdOld.value.length === 0
  || pwdNew.value.length < 6 || pwdNew.value !== pwdConfirm.value
))

function openPasswordModal(): void {
  open.value = false
  pwdError.value = ''
  pwdDone.value = false
  pwdOpen.value = true
}

function closePasswordModal(): void {
  pwdOpen.value = false
  pwdOld.value = ''
  pwdNew.value = ''
  pwdConfirm.value = ''
  pwdError.value = ''
  pwdDone.value = false
}

async function changePassword(): Promise<void> {
  pwdError.value = ''
  pwdDone.value = false
  if (pwdNew.value === pwdOld.value) {
    pwdError.value = '新密码不能与旧密码相同。'
    return
  }
  pwdBusy.value = true
  try {
    await api.changePassword({
      token: sessionStorage.getItem(AUTH_TOKEN_KEY) ?? '',
      old_password: pwdOld.value,
      new_password: pwdNew.value,
      confirm: pwdConfirm.value,
    })
    pwdDone.value = true
    pwdOld.value = ''
    pwdNew.value = ''
    pwdConfirm.value = ''
  } catch (error) {
    pwdError.value = error instanceof AuthApiError
      ? error.message
      : '修改失败，请稍后再试。'
  } finally {
    pwdBusy.value = false
  }
}

function logout(): void {
  sessionStorage.removeItem(AUTH_TOKEN_KEY)
  open.value = false
  emit('logout')
}
</script>

<template>
  <div ref="root" class="user-menu">
    <button
      type="button"
      class="user-menu-trigger"
      :aria-expanded="open"
      aria-label="个人中心"
      @click="toggle"
    >
      <span class="user-menu-avatar" :class="avatarClass">{{ initials }}</span>
      <span class="user-menu-name">{{ username }}</span>
      <ChevronDown :size="13" aria-hidden="true" />
    </button>

    <Transition name="user-menu">
      <div v-if="open" class="user-menu-panel" role="menu" aria-label="个人中心">
        <div class="user-menu-head">
          <strong>{{ profile?.username ?? username }}</strong>
          <span v-if="role === 'admin'" class="user-menu-badge">系统管理员</span>
        </div>

        <dl v-if="role === 'student'" class="user-menu-info">
          <div><dt>用户 ID</dt><dd>UID {{ profile?.user_id ?? '—' }}</dd></div>
          <div><dt>绑定手机</dt><dd>{{ profile?.phone_masked ?? '—' }}</dd></div>
        </dl>
        <dl v-else class="user-menu-info">
          <div><dt>在线学员会话</dt><dd>{{ profile?.active_sessions ?? '—' }}</dd></div>
        </dl>

        <button
          type="button"
          class="user-menu-action"
          role="menuitem"
          @click="openRecords"
        >
          <History :size="14" aria-hidden="true" /> 学习记录
        </button>

        <button
          v-if="role === 'student'"
          type="button"
          class="user-menu-action"
          role="menuitem"
          @click="openPasswordModal"
        >
          <KeyRound :size="14" aria-hidden="true" /> 修改密码
        </button>

        <!-- 0818 需求 1：导入/导出移入下拉；导入进入回放，学员无回放入口故隐藏 -->
        <button
          v-if="role !== 'student'"
          type="button"
          class="user-menu-action"
          role="menuitem"
          @click="pickImport"
        >
          <Upload :size="14" aria-hidden="true" /> 导入会话
        </button>
        <button
          type="button"
          class="user-menu-action"
          role="menuitem"
          aria-label="导出当前会话"
          :disabled="!canExport"
          @click="exportTrace"
        >
          <Download :size="14" aria-hidden="true" /> 导出会话
        </button>

        <button type="button" class="user-menu-action is-logout" role="menuitem" @click="logout">
          <LogOut :size="14" aria-hidden="true" /> 退出登录
        </button>
      </div>
    </Transition>

    <input
      ref="importInput"
      type="file"
      accept=".jsonl,.json"
      class="user-menu-import-input"
      aria-label="导入会话轨迹文件"
      @change="onImportChange"
    />

    <!-- 0818 需求 2：修改密码——页面正中央弹窗（原为下拉面板内嵌表单） -->
    <Teleport to="body">
      <Transition name="user-modal">
        <div
          v-if="pwdOpen"
          class="user-modal-overlay"
          role="presentation"
          @click.self="closePasswordModal"
        >
          <div
            class="user-modal"
            role="dialog"
            aria-modal="true"
            aria-label="修改密码"
          >
            <header class="user-modal-head">
              <strong>修改密码</strong>
              <button
                type="button"
                class="user-modal-close"
                aria-label="关闭修改密码"
                @click="closePasswordModal"
              >×</button>
            </header>
            <form class="user-menu-pwd" @submit.prevent="changePassword">
              <PasswordInput v-model="pwdOld" placeholder="旧密码" autocomplete="current-password" />
              <PasswordInput v-model="pwdNew" placeholder="新密码（至少 6 位）" autocomplete="new-password" />
              <PasswordInput v-model="pwdConfirm" placeholder="确认新密码" autocomplete="new-password" />
              <p v-if="pwdError" class="auth-error" role="alert">{{ pwdError }}</p>
              <p v-if="pwdDone" class="user-menu-done">密码已更新。</p>
              <div class="user-menu-pwd-actions">
                <button type="button" class="user-menu-action is-ghost" @click="closePasswordModal">取消</button>
                <button type="submit" class="user-menu-action is-primary" :disabled="pwdDisabled">
                  {{ pwdBusy ? '' : '确认修改' }}<WaveText v-if="pwdBusy" text="提交中…" />
                </button>
              </div>
            </form>
          </div>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>
