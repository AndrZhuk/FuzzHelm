import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { api, ApiError, qs } from '@/lib/api'
import type {
  EquityCurveData, Run, RunMetrics, StrategySummary, StrategyVersion,
} from '@/lib/types'

/** Порядок метрик у таблиці звіту: спершу дохідність, тоді ризик, тоді службові. */
export const METRIC_ORDER = [
  'total_return', 'cagr', 'sharpe', 'sortino', 'calmar', 'psr',
  'max_drawdown', 'ulcer_index', 'ann_vol', 'var95', 'cvar95',
  'win_rate', 'profit_factor', 'expectancy', 'avg_win', 'avg_loss',
  'n_trades', 'n_fills', 'turnover', 'exposure', 'traded_notional',
  'skew', 'kurt', 'tail_ratio', 'sr_period', 'n_obs', 'halted',
] as const

export const useBacktest = defineStore('backtest', () => {
  const runs = ref<Run[]>([])
  const selected = ref<Run | null>(null)
  const metrics = ref<RunMetrics | null>(null)
  const equity = ref<EquityCurveData | null>(null)
  const strategies = ref<StrategySummary[]>([])
  const versions = ref<StrategyVersion[]>([])
  const pending = ref(false)
  const pendingRun = ref(false)
  const error = ref<string | null>(null)
  const notice = ref<string | null>(null)

  const doneRuns = computed(() => runs.value.filter((r) => r.status === 'DONE'))

  async function loadRuns(limit = 40): Promise<void> {
    pending.value = true; error.value = null
    try { runs.value = await api<Run[]>(`/runs${qs({ limit })}`) }
    catch (e) { runs.value = []; error.value = e instanceof ApiError ? e.message : null }
    finally { pending.value = false }
  }

  async function select(run: Run): Promise<void> {
    selected.value = run
    metrics.value = null; equity.value = null
    pending.value = true
    try {
      const [m, eq] = await Promise.allSettled([
        api<RunMetrics>(`/runs/${run.id}/metrics`),
        api<EquityCurveData>(`/runs/${run.id}/equity${qs({ max_points: 1200 })}`),
      ])
      if (m.status === 'fulfilled') metrics.value = m.value
      if (eq.status === 'fulfilled') equity.value = eq.value
    } finally { pending.value = false }
  }

  async function loadStrategies(): Promise<void> {
    try { strategies.value = await api<StrategySummary[]>('/strategies') }
    catch { strategies.value = [] }
  }

  async function loadVersions(name: string): Promise<void> {
    try { versions.value = await api<StrategyVersion[]>(`/strategies/${encodeURIComponent(name)}`) }
    catch { versions.value = [] }
  }

  async function createStrategy(body: {
    name: string; rules_yaml: string; membership_yaml: string; activate: boolean
  }): Promise<{ ok: boolean; message: string }> {
    try {
      await api('/strategies', { method: 'POST', body: JSON.stringify(body) })
      await loadStrategies()
      return { ok: true, message: `Стратегію «${body.name}» створено (версія 1).` }
    } catch (e) {
      return { ok: false, message: e instanceof ApiError ? e.message : 'Не вдалося створити стратегію.' }
    }
  }

  async function launch(body: {
    symbol: string; tf: string; ts_from_ns: number; ts_to_ns: number
    engine: string; strategy_id?: number | null; seed?: number | null
  }): Promise<{ ok: boolean; message: string; runId?: string }> {
    pendingRun.value = true; notice.value = null
    try {
      const r = await api<{ run_id: string; status: string }>('/backtests', {
        method: 'POST', body: JSON.stringify(body),
      })
      await loadRuns()
      return { ok: true, message: `Прогін ${r.run_id.slice(0, 8)}… поставлено в чергу (${r.status}).`, runId: r.run_id }
    } catch (e) {
      return { ok: false, message: e instanceof ApiError ? e.message : 'Не вдалося запустити прогін.' }
    } finally { pendingRun.value = false }
  }

  return { runs, selected, metrics, equity, strategies, versions, pending, pendingRun, error, notice,
           doneRuns, loadRuns, select, loadStrategies, loadVersions, createStrategy, launch }
})
