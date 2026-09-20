import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, ApiError, qs } from '@/lib/api'
import type { RiskEvent, RiskEventPage, RiskLimits, RiskState } from '@/lib/types'

/** Режим ризику, журнал вердиктів і кіл-світч. */
export const useRisk = defineStore('risk', () => {
  const state = ref<RiskState | null>(null)
  const events = ref<RiskEvent[]>([])
  const limits = ref<RiskLimits | null>(null)
  const pending = ref(false)
  const error = ref<string | null>(null)
  const releasing = ref(false)

  /** Журнал відхилених ордерів — підмножина подій із вердиктом VETO/SHRINK (§8.2). */
  const rejections = computed(() => events.value.filter((e) => e.verdict === 'VETO' || e.verdict === 'SHRINK'))
  const isHalted = computed(() => state.value?.state === 'HALTED')

  async function loadState(): Promise<void> {
    try { state.value = await api<RiskState>('/risk/state') }
    catch (e) { error.value = e instanceof ApiError ? e.message : null }
  }

  async function loadEvents(limit = 60): Promise<void> {
    pending.value = true
    try {
      const page = await api<RiskEventPage>(`/risk/events${qs({ limit })}`)
      events.value = page.items
    } catch (e) {
      events.value = []
      error.value = e instanceof ApiError ? e.message : null
    } finally { pending.value = false }
  }

  async function loadLimits(): Promise<void> {
    try { limits.value = await api<RiskLimits>('/risk/limits') } catch { limits.value = null }
  }

  /** Зняття аварійного стопу — лише admin; пише before/after в audit_log. */
  async function release(reason: string): Promise<{ ok: boolean; message: string }> {
    releasing.value = true
    try {
      await api('/risk/killswitch/release', { method: 'POST', body: JSON.stringify({ reason }) })
      await Promise.all([loadState(), loadEvents()])
      return { ok: true, message: 'Аварійний стоп знято, запис додано до журналу аудиту.' }
    } catch (e) {
      return { ok: false, message: e instanceof ApiError ? e.message : 'Не вдалося зняти HALTED.' }
    } finally { releasing.value = false }
  }

  async function refresh(): Promise<void> { await Promise.all([loadState(), loadEvents(), loadLimits()]) }

  return { state, events, limits, pending, error, releasing, rejections, isHalted,
           loadState, loadEvents, loadLimits, release, refresh }
})
