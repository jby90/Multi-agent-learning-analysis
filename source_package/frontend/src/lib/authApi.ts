/// <reference types="vite/client" />

export type AuthRole = 'student' | 'admin'

export interface AuthSession {
  user_id: number
  username: string
  role: AuthRole
  token: string
}

export interface AuthProfile {
  user_id: number
  username: string
  phone_masked?: string
  role: AuthRole
  active_sessions?: number
}

export class AuthApiError extends Error {
  status: number

  constructor(message: string, status: number) {
    super(message)
    this.status = status
  }
}

export const AUTH_TOKEN_KEY = 'ref-auth-token'

function trimTrailingSlash(value: string): string {
  return value.endsWith('/') ? value.slice(0, -1) : value
}

export function createAuthApi(
  baseUrl = import.meta.env.VITE_INTERACTIVE_API_BASE ?? '',
) {
  const base = trimTrailingSlash(baseUrl)

  async function request<T>(
    path: string,
    method: 'GET' | 'POST',
    body?: Record<string, unknown>,
  ): Promise<T> {
    let response: Response
    try {
      response = await fetch(`${base}${path}`, {
        method,
        headers: { 'Content-Type': 'application/json' },
        ...(body ? { body: JSON.stringify(body) } : {}),
      })
    } catch {
      throw new AuthApiError('登录服务暂时不可用，请稍后再试。', 0)
    }
    let value: unknown
    try {
      value = await response.json()
    } catch {
      throw new AuthApiError('登录服务暂时不可用，请稍后再试。', response.status)
    }
    if (!response.ok) {
      const message = typeof (value as { error?: unknown })?.error === 'string'
        ? (value as { error: string }).error
        : '请求失败，请稍后再试。'
      throw new AuthApiError(message, response.status)
    }
    return value as T
  }

  return {
    checkPhone: (phone: string) => request<{ registered: boolean }>(
      `/api/auth/check-phone?phone=${encodeURIComponent(phone)}`,
      'GET',
    ),
    register: (input: {
      phone: string
      username: string
      password: string
      confirm: string
    }) => request<AuthSession>('/api/auth/register', 'POST', input),
    login: (phone: string, password: string) => request<AuthSession>(
      '/api/auth/login',
      'POST',
      { phone, password },
    ),
    adminLogin: (username: string, password: string) => request<AuthSession>(
      '/api/auth/admin-login',
      'POST',
      { username, password },
    ),
    changePassword: (input: {
      token: string
      old_password: string
      new_password: string
      confirm: string
    }) => request<{ ok: boolean }>('/api/auth/change-password', 'POST', input),
    me: (token: string) => request<AuthProfile>(
      `/api/auth/me?token=${encodeURIComponent(token)}`,
      'GET',
    ),
  }
}

export type AuthApi = ReturnType<typeof createAuthApi>
