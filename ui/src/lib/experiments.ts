/**
 * Дані експерименту фази 7, зведені `scripts/export_ui_experiments.py` у
 * `src/data/experiments.json` з тих самих артефактів, з яких зроблено таблиці звіту.
 * Імпорт на збірці, а не запит до API: це незмінні результати прогонів, і панель мусить
 * показувати рівно ті числа, що й звіт.
 */
import raw from '@/data/experiments.json'

export interface FoldMetrics {
  sharpe?: number; max_drawdown?: number; turnover?: number
  total_return?: number; n_trades?: number; psr?: number
  [k: string]: number | undefined
}
export interface Fold { fold: number; selected_cell: number | null; is: FoldMetrics; oos: FoldMetrics }
export interface EngineFolds { engine: string; concat_oos: FoldMetrics; folds: Fold[] }
export interface Provenance {
  git_sha?: string; git_dirty?: boolean; dataset_hash?: string; symbol?: string; command?: string
}
export interface WalkForward { provenance: Provenance; engines: EngineFolds[] }

export interface GridCell {
  cell: number; n_atr: number; chi: number; u_enter: number; rho_base: number; lam: number
  oos_sharpe?: number; oos_max_drawdown?: number; oos_turnover?: number
  oos_total_return?: number; oos_psr?: number
  pareto_oos?: boolean; feasible_oos?: boolean; selected?: boolean
}
export interface ParetoChoice {
  index: number; front: number[]; feasible: number[]; dd_cap: number; rule: string
  params: Record<string, number>
}
export interface Pareto {
  provenance: Provenance; criteria: string[]; selection_rule_uk: string | null
  dd_cap: number | null; choice: ParetoChoice | null; front: number[]
  dsr: number | null; statement: string | null; cells: GridCell[]
}

export interface TornadoRow {
  param: string; symbol: string; label_uk: string
  base_value: number; low_value: number; high_value: number
  sharpe_base: number; sharpe_low: number; sharpe_high: number
  sharpe_d_low: number; sharpe_d_high: number; sharpe_swing: number
}
export interface Sensitivity {
  provenance: Provenance; target: string; levels: number[]; tornado: TornadoRow[]
}

interface Experiments {
  symbols: string[]
  walkforward: Record<string, WalkForward>
  pareto: Record<string, Pareto>
  sensitivity: Record<string, Sensitivity>
}

export const experiments = raw as unknown as Experiments

/** Символ прогону («BTC-USDT-PERP») → символ експерименту («BTCUSDT»). */
export function expSymbol(canonical: string | null | undefined): string {
  if (!canonical) return experiments.symbols[0] ?? 'BTCUSDT'
  const compact = canonical.replace(/-/g, '').replace(/PERP$/, '')
  return experiments.symbols.includes(compact) ? compact : experiments.symbols[0] ?? compact
}
