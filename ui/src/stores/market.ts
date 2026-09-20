import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, ApiError, qs } from '@/lib/api'
import type { Candle, CandlePage, DqScore, Health } from '@/lib/types'

/** Ринкові дані і здоров'я конвеєра для LiveView. */
export const useMarket = defineStore('market', () => {
  const symbol = ref<string>('BTC-USDT-PERP')
  const tf = ref<string>('1m')
  const candles = ref<Candle[]>([])
  const health = ref<Health | null>(null)
  const dq = ref<DqScore | null>(null)
  const pending = ref(false)
  const error = ref<string | null>(null)

  async function loadCandles(limit = 240): Promise<void> {
    pending.value = true; error.value = null
    try {
      const page = await api<CandlePage>(`/market/candles${qs({ symbol: symbol.value, tf: tf.value, limit, order: 'desc' })}`)
      // API віддає найсвіжіші першими — графіку потрібен хронологічний порядок.
      candles.value = [...page.items].sort((a, b) => a.open_time_ns - b.open_time_ns)
    } catch (e) {
      candles.value = []
      error.value = e instanceof ApiError ? e.message : 'Невідома помилка.'
    } finally { pending.value = false }
  }

  async function loadHealth(): Promise<void> {
    try { health.value = await api<Health>('/market/health') } catch { health.value = null }
  }

  /** Погодинний Q: вікно задається часом (limit ендпоінт не приймає). */
  async function loadDq(hours = 48): Promise<void> {
    const to = Date.now() * 1e6
    const from = to - hours * 3600 * 1e9
    try { dq.value = await api<DqScore>(`/dq/score${qs({ symbol: symbol.value, from_ns: Math.trunc(from), to_ns: Math.trunc(to) })}`) }
    catch { dq.value = null }
  }

  async function refresh(): Promise<void> {
    await Promise.all([loadCandles(), loadHealth(), loadDq()])
  }

  return { symbol, tf, candles, health, dq, pending, error, loadCandles, loadHealth, loadDq, refresh }
})
