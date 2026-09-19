"""Справжнє трасування рішення для тестів API (/explain): детектори → консенсус → Мамдані → κ → сайзер.

Найменування: tests/helpers/api_traces.py
Автор: Андрій Жук, 2026.

Спільне для офлайн e2e (tests/e2e/test_api.py) та інтеграційних тестів на PostgreSQL
(tests/integration/test_api_db.py).
"""

from __future__ import annotations

from decimal import Decimal

from fuzzhelm.decision.aggregator import consensus
from fuzzhelm.decision.agreement import agreement
from fuzzhelm.decision.core import clip_unit
from fuzzhelm.decision.trace import DecisionTrace
from fuzzhelm.detectors.base import DetectorGroup, DetectorOutput
from fuzzhelm.fuzzy.mamdani import default_engine
from fuzzhelm.sizing.sizer import PositionSizer, SizingInput
from tests.helpers.api_fakes import T0_NS


def make_outputs() -> tuple[DetectorOutput, ...]:
    trend, rev, ctx = DetectorGroup.TREND, DetectorGroup.REVERSION, DetectorGroup.CONTEXT
    return (
        DetectorOutput("ema_slope", s=0.55, c=0.8, features={"slope": 0.0012}, group=trend, weight=1.0),
        DetectorOutput("donchian", s=0.4, c=0.6, features={"stale": 3.0}, group=trend, weight=1.0),
        DetectorOutput("rsi_exhaustion", s=-0.2, c=0.5, features={"rsi": 61.0}, group=rev, weight=0.8),
        DetectorOutput("bollinger_z", s=-0.1, c=0.7, features={"z": 0.4}, group=rev, weight=0.8),
        DetectorOutput("candle_geometry", s=0.05, c=0.3, group=rev, weight=0.6),
        DetectorOutput("vol_regime", s=0.0, c=1.0, features={"V": 0.42, "warm": 1.0}, group=ctx, weight=1.0),
    )


def make_trace() -> DecisionTrace:
    """Справжній ланцюжок ядра: консенсус → Мамдані → κ → u_final → сайзер (без мока)."""
    outputs = make_outputs()
    cons = consensus(outputs)
    fz = default_engine().infer(cons.T, cons.R, cons.V)
    agr = agreement(outputs)
    u_final = clip_unit(agr.kappa * fz.u_raw)
    trace = DecisionTrace(
        open_time_ns=T0_NS,
        detector_outputs=outputs,
        consensus=cons,
        fuzzy=fz,
        agreement=agr,
        u_raw=fz.u_raw,
        kappa=agr.kappa,
        u_final=u_final,
        engine="mamdani",
    )
    sizing = PositionSizer().size(
        SizingInput(
            u_final=u_final,
            equity=Decimal("10000"),
            price=Decimal("60000.1"),
            atr=35.0,
            s_t=1.2,
            kappa_mode=1.0,
            step_size=Decimal("0.001"),
            min_notional=Decimal("5"),
        )
    )
    risk = {
        "state": "NORMAL",
        "kappa_mode": 1.0,
        "verdict": {"kind": "SHRINK", "factor": "0.5"},
        "vetoes": [],
        "approved_qty": format(sizing.qty / 2, "f"),
    }
    return trace.with_sizing(sizing.to_dict()).with_risk(risk)
