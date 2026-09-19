"""Pydantic-схеми запитів і відповідей API (описи англійською — для OpenAPI /docs, ЗК5).

Найменування: api/schemas.py
Призначення: типізований контракт HTTP-межі. Гроші/ціни/кількості серіалізуються рядками без
експоненти (core.money.dec_str), бо JSON-число втратило б точність NUMERIC(38,18); метрики ядра
(T/R/V, α, μ, u, κ) — float. Час — наносекунди UTC (int), як у домені.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from fuzzhelm.core.enums import Role

MAX_YAML_CHARS = 256 * 1024  # межа розміру тексту стратегії (DoS через величезний YAML)

DecimalStr = str  # документаційний псевдонім: десятковий рядок без експоненти


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _Out(BaseModel):
    model_config = ConfigDict(extra="allow")


# ------------------------------------------------------------------ помилки


class ErrorItem(_Out):
    loc: list[str | int] = Field(description="Location of the error (FastAPI convention).")
    msg: str = Field(description="Human-readable message.")
    type: str = Field(description="Error type, e.g. `config_validation`, `yaml_syntax`.")
    path: str | None = Field(
        default=None, description="Precise field path inside the YAML/JSON document, e.g. `rules[3].if.T`."
    )


class ErrorResponse(_Out):
    detail: str | list[ErrorItem] = Field(description="Error description.")


# ------------------------------------------------------------------ auth


class LoginJson(_Model):
    username: str = Field(min_length=1, max_length=128, description="User login.")
    password: str = Field(min_length=1, max_length=128, description="Password (bcrypt limit: 72 bytes).")


class TokenResponse(_Out):
    access_token: str = Field(description="JWT (HS256) to send as `Authorization: Bearer <token>`.")
    token_type: Literal["bearer"] = "bearer"
    expires_in: int = Field(description="Token lifetime in seconds (8 h by default).")
    role: Role = Field(description="Role of the authenticated user.")
    login: str


class MeResponse(_Out):
    uid: int
    login: str
    role: Role
    permissions: list[str] = Field(description="Permissions granted to the role by the access matrix.")
    expires_at_s: int = Field(description="Token expiry, Unix seconds.")


# ------------------------------------------------------------------ market / dq


class CandleOut(_Out):
    open_time_ns: int
    close_time_ns: int
    o: DecimalStr | None
    h: DecimalStr | None
    l: DecimalStr | None
    c: DecimalStr | None
    volume: DecimalStr | None
    quote_volume: DecimalStr | None
    trades_count: int | None
    vwap: DecimalStr | None
    is_closed: bool
    is_synthetic: bool
    src: int = Field(description="1 = WebSocket, 2 = REST, 3 = replay.")
    anomaly_score: DecimalStr | None


class CandlePageOut(_Out):
    symbol: str
    instrument_id: int
    tf: str
    items: list[CandleOut]
    next_after_ns: int | None = Field(
        description="Pass as `after_ns` to fetch the next page; null on last page."
    )


class DqRowOut(_Out):
    hour_start_ns: int
    expected_buckets: int | None
    observed_buckets: int | None
    invalid_count: int | None
    anomaly_count: int | None
    gap_seconds: float | None
    lag_p95_ms: float | None
    completeness: float | None
    validity: float | None
    timeliness: float | None
    continuity: float | None
    score: float | None


class DqScoreOut(_Out):
    symbol: str
    instrument_id: int
    weights: dict[str, float] = Field(description="AHP weights of the 4 components (Q = Σ w_i·component_i).")
    items: list[DqRowOut]


class InstrumentHealth(_Out):
    symbol: str
    instrument_id: int
    last_closed_open_time_ns: int | None
    data_lag_s: float | None = Field(description="Seconds since the close of the last closed 1m candle.")
    latest_q: DqRowOut | None
    open_gaps: int


class HealthOut(_Out):
    now_ns: int
    instruments: list[InstrumentHealth]
    gaps_by_status: dict[str, int]
    open_gaps_total: int
    pipeline: dict[str, Any] | None = Field(
        description="Last PipelineHealth snapshot published by the ingest worker on channel "
        "`fuzzhelm_live` (kind `health`); null if none was received by this API process."
    )


# ------------------------------------------------------------------ strategies


class StrategyIn(_Model):
    name: str = Field(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_.-]+$",
        description="Strategy name (versions are grouped by name).",
    )
    rules_yaml: str = Field(
        min_length=1,
        max_length=MAX_YAML_CHARS,
        description="Mamdani rule base in the `config/rules_mamdani.yaml` format.",
    )
    membership_yaml: str = Field(
        min_length=1,
        max_length=MAX_YAML_CHARS,
        description="Membership functions in the `config/membership.yaml` format.",
    )
    activate: bool = Field(default=False, description="Make the new version the active one.")


class StrategyUpdateIn(_Model):
    rules_yaml: str = Field(min_length=1, max_length=MAX_YAML_CHARS)
    membership_yaml: str = Field(min_length=1, max_length=MAX_YAML_CHARS)
    activate: bool = False


class StrategyVersionOut(_Out):
    id: int
    name: str
    version: int
    rules_hash: str = Field(description="BLAKE2b-256 of the canonical (rules, membership) pair, hex.")
    created_by: str | None
    created_at_ns: int | None
    is_active: bool
    n_rules: int | None = None


class StrategyDetailOut(StrategyVersionOut):
    rules_yaml: str
    membership_yaml: str


class StrategySummaryOut(_Out):
    name: str
    versions: int
    latest_version: int
    active_version: int | None


# ------------------------------------------------------------------ backtests / runs


class BacktestIn(_Model):
    symbol: str = Field(default="BTC-USDT-PERP", min_length=1, max_length=32, description="Canonical symbol.")
    tf: Literal["1m"] = "1m"
    ts_from_ns: int = Field(ge=0, description="Start of the window, ns UTC (inclusive).")
    ts_to_ns: int = Field(ge=0, description="End of the window, ns UTC (exclusive).")
    engine: Literal["mamdani", "linear"] = "mamdani"
    strategy_id: int | None = Field(default=None, description="Strategy version id; null = active/default.")
    seed: int | None = Field(default=None, description="Seed; null = Settings.seed.")
    params: dict[str, float | int | str | bool] = Field(
        default_factory=dict, description="Parameter overrides (e.g. n_atr, chi, u_enter, rho_base, lam)."
    )


class BacktestAccepted(_Out):
    run_id: UUID
    status: str
    status_url: str


class RunOut(_Out):
    id: UUID
    kind: str | None
    status: str | None
    engine: str | None
    strategy_id: int | None
    instrument_id: int | None
    tf: str | None
    ts_from_ns: int | None
    ts_to_ns: int | None
    seed: int | None
    git_sha: str | None
    config_hash: str | None
    dataset_hash: str | None
    journal_head_hash: str | None
    equity_hash: str | None
    error: str | None
    started_at_ns: int | None
    finished_at_ns: int | None
    config: dict[str, Any] | None = None
    job: dict[str, Any] | None = Field(default=None, description="In-process job state (queued backtests).")


class MetricsOut(_Out):
    run_id: UUID
    metrics: dict[str, float | None]


class EquityPointOut(_Out):
    ts_ns: int
    equity: DecimalStr | None
    cash: DecimalStr | None
    unrealized: DecimalStr | None
    gross_exposure: DecimalStr | None
    leverage: float | None
    drawdown: float | None
    risk_state: str | None
    kappa: float | None
    var95: DecimalStr | None
    cvar95: DecimalStr | None


class EquityOut(_Out):
    run_id: UUID
    n_total: int
    stride: int = Field(description="Every `stride`-th point is returned (the last point is always kept).")
    items: list[EquityPointOut]


# ------------------------------------------------------------------ risk


class RiskEventOut(_Out):
    id: int
    run_id: UUID | None
    ts_ns: int | None
    instrument_id: int | None
    rule: str
    verdict: str | None
    factor: DecimalStr | None = Field(
        description="Exact multiplier (from payload when NUMERIC(6,4) rounded it)."
    )
    observed: DecimalStr | None
    limit_value: DecimalStr | None
    state_from: str | None
    state_to: str | None
    dwell_bars: int | None
    actor: str | None
    payload: dict[str, Any] | None


class RiskStateOut(_Out):
    run_id: UUID | None
    state: str
    state_source: str = Field(description="`transition` | `equity_point` | `default`.")
    kappa_mode: float
    last_transition: RiskEventOut | None
    equity: DecimalStr | None
    drawdown: float | None
    equity_ts_ns: int | None
    last_release_request: dict[str, Any] | None


class RiskLimitsOut(_Out):
    config: dict[str, Any] = Field(description="Validated `config/risk_limits.yaml` tree.")
    sha256: str = Field(
        description="Hash of the file content; send as `expected_sha256` to avoid lost updates."
    )


class RiskLimitsIn(_Model):
    limits: dict[str, Any] = Field(description="Six limits (see `config/risk_limits.yaml`).")
    state_machine: dict[str, Any] = Field(description="Risk state machine thresholds and κ_mode.")
    sizing: dict[str, Any] | None = Field(default=None, description="Sizer parameters (defaults if omitted).")
    hysteresis: dict[str, Any] | None = Field(default=None, description="Schmitt trigger thresholds.")
    expected_sha256: str | None = Field(
        default=None,
        min_length=64,
        max_length=64,
        description="Optimistic lock: current file hash from GET /risk/limits.",
    )


class RiskLimitsChanged(_Out):
    sha256: str
    audit_id: int
    changed: list[str] = Field(description="Dotted paths of changed values.")
    config: dict[str, Any]


class KillSwitchReleaseIn(_Model):
    run_id: UUID | None = Field(
        default=None,
        description="Run whose state machine is released; null = latest live run (paper/replay/testnet).",
    )
    reason: str = Field(
        min_length=3, max_length=500, description="Why the halt is released (goes to audit_log)."
    )


class KillSwitchReleaseAccepted(_Out):
    audit_id: int
    run_id: UUID | None
    observed_state: str | None
    status: Literal["requested"] = "requested"
    channel: str = Field(description="Control channel the worker listens on.")


# ------------------------------------------------------------------ audit


class AuditOut(_Out):
    id: int
    ts_ns: int | None
    user_id: int | None
    action: str | None
    target: str | None
    before_json: dict[str, Any] | None
    after_json: dict[str, Any] | None
    ip: str | None
