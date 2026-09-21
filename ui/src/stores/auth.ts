import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, getToken, setToken, ApiError } from '@/lib/api'
import type { DemoUser, DemoUsersResponse, MeResponse, Role, TokenResponse } from '@/lib/types'

export const useAuth = defineStore('auth', () => {
  const me = ref<MeResponse | null>(null)
  const pending = ref(false)
  const error = ref<string | null>(null)
  /** Демо-користувачі для кнопок швидкого входу; порожньо, якщо сервер демо-вхід не ввімкнув. */
  const demoUsers = ref<DemoUser[]>([])
  /** Логін, під яким саме зараз іде швидкий вхід (для стану кнопки). */
  const pendingLogin = ref<string | null>(null)

  const isAuthenticated = computed(() => me.value !== null)
  const role = computed<Role | null>(() => me.value?.role ?? null)
  const can = (permission: string): boolean => me.value?.permissions.includes(permission) ?? false
  const isAdmin = computed(() => role.value === 'admin')

  /** Спільне завершення входу: зберегти токен і підтягнути профіль з правами. */
  async function signIn(request: () => Promise<TokenResponse>, badCredentials: string): Promise<boolean> {
    pending.value = true; error.value = null
    try {
      const t = await request()
      setToken(t.access_token)
      me.value = await api<MeResponse>('/auth/me')
      return true
    } catch (e) {
      setToken(null); me.value = null
      error.value = e instanceof ApiError
        ? (e.status === 401 || e.status === 404 ? badCredentials : e.message)
        : 'Невідома помилка входу.'
      return false
    } finally { pending.value = false }
  }

  async function login(login_: string, password: string): Promise<boolean> {
    return signIn(() => api<TokenResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ login: login_, password }),
    }), 'Невірний логін або пароль.')
  }

  /** Швидкий вхід демо-користувачем без пароля (працює лише на локальному стенді з увімкненим демо). */
  async function demoLogin(login_: string): Promise<boolean> {
    pendingLogin.value = login_
    try {
      return await signIn(() => api<TokenResponse>(`/auth/demo/${encodeURIComponent(login_)}`, { method: 'POST' }),
        'Демо-вхід вимкнено на сервері або користувача немає.')
    } finally { pendingLogin.value = null }
  }

  async function loadDemoUsers(): Promise<void> {
    try {
      const r = await api<DemoUsersResponse>('/auth/demo')
      demoUsers.value = r.enabled ? r.users : []
    } catch { demoUsers.value = [] }
  }

  /** Відновлення сесії зі збереженого токена при перезавантаженні сторінки. */
  async function restore(): Promise<void> {
    if (getToken() === null) return
    pending.value = true
    try { me.value = await api<MeResponse>('/auth/me') }
    catch { setToken(null); me.value = null }
    finally { pending.value = false }
  }

  function logout(): void { setToken(null); me.value = null; error.value = null }

  return {
    me, pending, error, demoUsers, pendingLogin, isAuthenticated, role, isAdmin, can,
    login, demoLogin, loadDemoUsers, restore, logout,
  }
})
