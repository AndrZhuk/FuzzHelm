/** Типи відповідей API. Дзеркалять src/fuzzhelm/api/schemas.py і api/explain.py. */

export type Role = 'analyst' | 'operator' | 'auditor' | 'admin'
export type RiskStateName = 'NORMAL' | 'WARNING' | 'COOLDOWN' | 'HALTED'
export type VerdictKind = 'ALLOW' | 'SHRINK' | 'VETO'

export interface TokenResponse {
  access_token: string; token_type: string; expires_in: number; role: Role; login: string
}
/** Кнопки швидкого входу: демо-користувачі стенда (сервер віддає їх лише при FUZZHELM_DEMO_LOGIN=1). */
export interface DemoUser { login: string; role: Role }
export interface DemoUsersResponse { enabled: boolean; users: DemoUser[] }
export interface MeResponse {
  uid: number; login: string; role: Role; permissions: string[]; expires_at_s: number
}

export interface Term {
  name: string; name_uk: string; kind: string
  params: Record<string, unknown>
  mu: number; active: boolean; curve: number[]
}
export interface FuzzyVariable {
  name: string; name_uk: string; range: [number, number]
  value: number; x: number[]; terms: Term[]
}
export interface FiredRule {
  rule_id: string; alpha: number; consequent: string; consequent_uk: string
  antecedent: Record<string, string>; antecedent_uk: Record<string, string>; text_uk: string
}
export interface Aggregate {
  grid: number[]; mu: number[]; centroid: number; scheme: string; nodes: number
  area: number; height: number
  strengths: { term: string; term_uk: string; beta: number }[]
}
export interface DetectorOutput {
  name: string; group: string; s: number; c: number; weight: number
  features: Record<string, number>
}
export interface Sizing {
  qty: number; s_t: number; q_vt: number; side: number; q_atr: number; q_lev: number
  q_raw: number; notional: string | number; sigma_ann: number; kappa_mode: number
  reject_code: string | null; stop_distance: number; binding_constraint: string
}
export interface RiskRecord {
  rule: string; limit: string; factor: string; verdict: VerdictKind; observed: string
}
export interface RiskBlock {
  halt: boolean; state: RiskStateName; factor: string; vetoes: string[]
  records: RiskRecord[]; verdict: VerdictKind; evaluated: boolean; kappa_mode: number
  current_qty: string; flatten_all: boolean; approved_qty: string; requested_qty: string
  increase_approved: string; increase_requested: string
}
export interface AgreementBlock {
  p_plus: number; p_minus: number; p_zero: number; H: number; A_g: number
  kappa: number; mass: number; kappa_min: number; nu: number; has_evidence: boolean
}
export interface Consistency {
  u_raw_stored: number; u_raw_recomputed: number; u_raw_abs_diff: number
  kappa_abs_diff: number; fired_rules_match: boolean; max_alpha_abs_diff: number
  column_tolerance: number; ok: boolean
}
export interface Explain {
  decision_id: number; run_id: string; instrument_id: number; open_time_ns: number
  engine: string
  strategy: { id: number | null; name: string | null; version: number | null; source: string }
  inputs: Record<string, number>
  inputs_source: string; v_source: string
  stored: Record<string, number>
  variables: Record<string, FuzzyVariable>
  memberships: Record<string, Record<string, number>>
  fired_rules: FiredRule[]; n_fired: number
  aggregate: Aggregate
  u_raw: number; kappa: number; u_final: number
  agreement: AgreementBlock
  detector_outputs: DetectorOutput[]
  sizing: Sizing
  risk: RiskBlock
  target: { side: number; qty: string; binding_constraint: string }
  prices: { stop: string | null; tp: string | null; liq: string | null }
  narrative_uk: string; narrative_source: string
  consistency: Consistency
  terms_uk: Record<string, Record<string, string>>
}

export interface Candle {
  open_time_ns: number; close_time_ns: number
  o: string | null; h: string | null; l: string | null; c: string | null
  volume: string | null; quote_volume: string | null; trades_count: number | null
  vwap: string | null; is_closed: boolean; is_synthetic: boolean; src: number
}
export interface CandlePage {
  symbol: string; instrument_id: number; tf: string; items: Candle[]; next_after_ns: number | null
}

export interface RiskEvent {
  id: number; run_id: string | null; ts_ns: number | null; instrument_id: number | null
  rule: string; verdict: VerdictKind | null; factor: string | null
  observed: string | null; limit_value: string | null
  state_from: string | null; state_to: string | null; dwell_bars: number | null
  actor: string | null; payload: Record<string, unknown> | null
}
export interface RiskEventPage { run_id: string | null; items: RiskEvent[]; next_before_id?: number | null }

export interface RiskState {
  run_id: string | null; state: RiskStateName; state_source: string; kappa_mode: number
  last_transition: RiskEvent | null
  equity: string | null; drawdown: number | null; equity_ts_ns: number | null
  last_release_request: Record<string, unknown> | null
}
export interface RiskLimits { config: Record<string, unknown>; sha256: string }

export interface DqRow {
  hour_start_ns: number; expected_buckets: number | null; observed_buckets: number | null
  invalid_count: number | null; gap_seconds: number | null
  lag_p95_ms: number | null; completeness: number | null; validity: number | null
  timeliness: number | null; continuity: number | null; score: number | null
}
export interface DqScore {
  symbol: string; instrument_id: number; weights: Record<string, number>; items: DqRow[]
}

export interface InstrumentHealth {
  symbol: string; instrument_id: number; last_closed_open_time_ns: number | null
  data_lag_s: number | null; latest_q: DqRow | null; open_gaps: number
}
export interface Health {
  now_ns: number; instruments: InstrumentHealth[]
  gaps_by_status: Record<string, number>; open_gaps_total: number
  pipeline: Record<string, unknown> | null
  pipelines: Record<string, Record<string, unknown>>
}

export interface Run {
  id: string; kind: string | null; status: string | null; engine: string | null
  strategy_id: number | null; instrument_id: number | null; tf: string | null
  ts_from_ns: number | null; ts_to_ns: number | null; seed: number | null
  git_sha: string | null; config_hash: string | null; dataset_hash: string | null
  journal_head_hash: string | null; equity_hash: string | null; error: string | null
  started_at_ns: number | null; finished_at_ns: number | null
  config: Record<string, unknown> | null; job: Record<string, unknown> | null
}
export interface RunMetrics { run_id: string; metrics: Record<string, number> }
export interface EquityPoint {
  ts_ns: number; equity: string | null; cash: string | null; unrealized: string | null
  gross_exposure: string | null; leverage: number | null; drawdown: number | null
  risk_state: RiskStateName | null; kappa: number | null
}
export interface EquityCurveData { run_id: string; n_total: number; stride: number; items: EquityPoint[] }

export interface StrategySummary {
  name: string; active_version: number | null; versions: number
  updated_at_ns?: number | null; [k: string]: unknown
}
export interface StrategyVersion {
  name: string; version: number; engine: string | null; is_active: boolean
  membership_yaml?: string; rules_yaml?: string; sha256?: string
  created_at_ns?: number | null; [k: string]: unknown
}
