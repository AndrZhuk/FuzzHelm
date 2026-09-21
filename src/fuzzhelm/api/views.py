"""Перетворення рядків storage у JSON-відповіді API (Decimal → рядок, bytes → hex, NaN → null).

Найменування: api/views.py
Призначення: одна точка серіалізації HTTP-межі, щоб точність NUMERIC(38,18) не губилась у JSON-числах,
а неcкінченні float (NaN-метрики) не ламали JSON (у JSON немає NaN).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from decimal import Decimal
from typing import Any

from fuzzhelm.core.money import dec_str
from fuzzhelm.features.convert import to_float
from fuzzhelm.storage.repositories import (
    AuditRow,
    CandleRow,
    DqRow,
    EquityRow,
    RiskEventRow,
    RunRow,
    StrategyRow,
)


def dstr(x: Decimal | None) -> str | None:
    return None if x is None else dec_str(x)


def fnum(x: Decimal | float | int | None) -> float | None:
    if x is None:
        return None
    v = to_float(x) if isinstance(x, Decimal) else float(x)
    return v if math.isfinite(v) else None


def hexb(x: bytes | str | None) -> str | None:
    if x is None:
        return None
    return x.hex() if isinstance(x, bytes | bytearray | memoryview) else str(x)


def candle_out(r: CandleRow) -> dict[str, Any]:
    return {
        "open_time_ns": r.open_time_ns,
        "close_time_ns": r.close_time_ns,
        "o": dstr(r.o),
        "h": dstr(r.h),
        "l": dstr(r.l),
        "c": dstr(r.c),
        "volume": dstr(r.volume),
        "quote_volume": dstr(r.quote_volume),
        "trades_count": r.trades_count,
        "vwap": dstr(r.vwap),
        "is_closed": r.is_closed,
        "is_synthetic": r.is_synthetic,
        "src": r.src,
    }


def dq_out(r: DqRow) -> dict[str, Any]:
    return {
        "hour_start_ns": r.hour_start_ns,
        "expected_buckets": r.expected_buckets,
        "observed_buckets": r.observed_buckets,
        "invalid_count": r.invalid_count,
        "gap_seconds": fnum(r.gap_seconds),
        "lag_p95_ms": fnum(r.lag_p95_ms),
        "completeness": fnum(r.completeness),
        "validity": fnum(r.validity),
        "timeliness": fnum(r.timeliness),
        "continuity": fnum(r.continuity),
        "score": fnum(r.score),
    }


def run_out(r: RunRow, *, with_config: bool = True) -> dict[str, Any]:
    return {
        "id": r.id,
        "kind": r.kind,
        "status": r.status,
        "engine": r.engine,
        "strategy_id": r.strategy_id,
        "instrument_id": r.instrument_id,
        "tf": r.tf,
        "ts_from_ns": r.ts_from_ns,
        "ts_to_ns": r.ts_to_ns,
        "seed": r.seed,
        "git_sha": r.git_sha,
        "config_hash": hexb(r.config_hash),
        "dataset_hash": hexb(r.dataset_hash),
        "journal_head_hash": hexb(r.journal_head_hash),
        "equity_hash": hexb(r.equity_hash),
        "error": r.error,
        "started_at_ns": r.started_at_ns,
        "finished_at_ns": r.finished_at_ns,
        "config": r.config if with_config else None,
    }


def equity_out(r: EquityRow) -> dict[str, Any]:
    return {
        "ts_ns": r.ts_ns,
        "equity": dstr(r.equity),
        "cash": dstr(r.cash),
        "unrealized": dstr(r.unrealized),
        "gross_exposure": dstr(r.gross_exposure),
        "leverage": fnum(r.leverage),
        "drawdown": fnum(r.drawdown),
        "risk_state": r.risk_state,
        "kappa": fnum(r.kappa),
        "var95": dstr(r.var95),
        "cvar95": dstr(r.cvar95),
    }


def risk_event_out(r: RiskEventRow) -> dict[str, Any]:
    exact = getattr(r, "factor_exact", r.factor)
    return {
        "id": r.id,
        "run_id": r.run_id,
        "ts_ns": r.ts_ns,
        "instrument_id": r.instrument_id,
        "rule": r.rule,
        "verdict": r.verdict,
        "factor": dstr(exact),
        "observed": dstr(r.observed),
        "limit_value": dstr(r.limit_value),
        "state_from": r.state_from,
        "state_to": r.state_to,
        "dwell_bars": r.dwell_bars,
        "actor": r.actor,
        "payload": r.payload,
    }


def audit_out(r: AuditRow) -> dict[str, Any]:
    return {
        "id": r.id,
        "ts_ns": r.ts_ns,
        "user_id": r.user_id,
        "action": r.action,
        "target": r.target,
        "before_json": r.before_json,
        "after_json": r.after_json,
        "ip": r.ip,
    }


def strategy_meta(r: StrategyRow, n_rules: int | None = None) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "version": r.version,
        "rules_hash": hexb(r.rules_hash),
        "created_by": r.created_by,
        "created_at_ns": r.created_at_ns,
        "is_active": bool(r.is_active),
        "n_rules": n_rules,
    }


def strategy_detail(r: StrategyRow) -> dict[str, Any]:
    return {**strategy_meta(r), "rules_yaml": r.rules_yaml, "membership_yaml": r.membership_yaml}


def finite_metrics(metrics: dict[str, float | None]) -> dict[str, float | None]:
    return {k: (v if v is not None and math.isfinite(v) else None) for k, v in metrics.items()}
