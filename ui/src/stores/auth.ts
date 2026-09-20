import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, getToken, setToken, ApiError } from '@/lib/api'
import type { MeResponse, Role, TokenResponse } from '@/lib/types'

export const useAuth = defineStore('auth', () => {
  const me = ref<MeResponse | null>(null)
  const pending = ref(false)
  const error = ref<string | null>(null)

  const isAuthenticated = computed(() => me.value !== null)
  const role = computed<Role | null>(() => me.value?.role ?? null)
  const can = (permission: string): boolean => me.value?.permissions.includes(permission) ?? false
  const isAdmin = computed(() => role.value === 'admin')

  async function login(login_: string, password: string): Promise<boolean> {
    pending.value = true; error.value = null
    try {
      const t = await api<TokenResponse>('/auth/login', {
        method: 'POST',
        body: JSON.stringify({ login: login_, password }),
      })
      setToken(t.access_token)
      me.value = await api<MeResponse>('/auth/me')
      return true
    } catch (e) {
      setToken(null); me.value = null
      error.value = e instanceof ApiError
        ? (e.status === 401 ? 'Невірний логін або пароль.' : e.message)
        : 'Невідома помилка входу.'
      return false
    } finally { pending.value = false }
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

  return { me, pending, error, isAuthenticated, role, isAdmin, can, login, restore, logout }
})
