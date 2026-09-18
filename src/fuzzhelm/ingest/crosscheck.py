"""Крос-звірка цін двох незалежних джерел: Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT.

Найменування: ingest/crosscheck.py
Призначення: «агрегація біржових даних» — два джерела звіряються похвилинно за ціною закриття;
розбіжність понад поріг (за замовчуванням 50 б.п.) позначається; рядки — «таблиця розбіжностей» звіту.
Автор: Андрій Жук, 2026.

Розбіжність, б.п.:  d = 10⁴ · (c_Binance − c_Kraken) / c_Kraken   (Decimal, без float; арифметика — у
core.money.DECIMAL_CONTEXT незалежно від контексту потоку, що викликає, тож звіт відтворюваний);
позначка: |d| > поріг (строго). Базис USDT/USD і премія перпетуалу до споту входять у d — саме тому
поріг широкий: він ловить збій джерела (застиглий фід, хибний тик), а не нормальний базис.
Звіряються лише ЗАКРИТІ свічки з однаковим open_time.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import Final

from fuzzhelm.core.dto import Candle
from fuzzhelm.core.money import D0, DECIMAL_CONTEXT, dec, dec_str

BPS: Final = Decimal(10_000)
DEFAULT_THRESHOLD_BPS: Final = Decimal(50)
_Q4: Final = Decimal("0.0001")


@dataclass(frozen=True, slots=True)
class CrosscheckRow:
    open_time_ns: int
    binance_close: Decimal
    kraken_close: Decimal
    diff: Decimal            # c_Binance − c_Kraken, у валюті котирування
    diff_bps: Decimal        # точне значення (контекст Decimal, 38 знаків)
    flagged: bool


@dataclass(frozen=True, slots=True)
class CrosscheckReport:
    binance_instrument: str
    kraken_instrument: str
    tf: str
    threshold_bps: Decimal
    rows: tuple[CrosscheckRow, ...]
    flagged: tuple[CrosscheckRow, ...]
    missing_in_binance: tuple[int, ...]      # open_time_ns закритих барів Kraken без пари
    missing_in_kraken: tuple[int, ...]

    @property
    def n_matched(self) -> int:
        return len(self.rows)

    @property
    def mean_abs_bps(self) -> Decimal | None:
        if not self.rows:
            return None
        with localcontext(DECIMAL_CONTEXT):
            return sum((abs(r.diff_bps) for r in self.rows), D0) / len(self.rows)

    @property
    def max_abs_bps(self) -> Decimal | None:
        return max((abs(r.diff_bps) for r in self.rows), default=None)

    @property
    def mean_bps(self) -> Decimal | None:
        if not self.rows:
            return None
        with localcontext(DECIMAL_CONTEXT):
            return sum((r.diff_bps for r in self.rows), D0) / len(self.rows)

    def summary(self) -> dict[str, str | int | None]:
        def q(x: Decimal | None) -> str | None:
            return None if x is None else dec_str(x.quantize(_Q4))

        return {
            "binance": self.binance_instrument, "kraken": self.kraken_instrument, "tf": self.tf,
            "threshold_bps": dec_str(self.threshold_bps), "n_matched": self.n_matched,
            "n_flagged": len(self.flagged), "missing_in_binance": len(self.missing_in_binance),
            "missing_in_kraken": len(self.missing_in_kraken), "mean_bps": q(self.mean_bps),
            "mean_abs_bps": q(self.mean_abs_bps), "max_abs_bps": q(self.max_abs_bps),
        }

    def table(self, *, only_flagged: bool = False) -> list[dict[str, str]]:
        """Рядки «таблиці розбіжностей» (усі значення — рядки, б.п. округлено до 0.0001)."""
        src = self.flagged if only_flagged else self.rows
        return [{
            "open_time_ns": str(r.open_time_ns),
            "binance_close": dec_str(r.binance_close),
            "kraken_close": dec_str(r.kraken_close),
            "diff": dec_str(r.diff),
            "diff_bps": dec_str(r.diff_bps.quantize(_Q4)),
            "flagged": "yes" if r.flagged else "no",
        } for r in src]


def divergence_bps(price: Decimal, reference: Decimal) -> Decimal:
    if reference <= D0:
        raise ValueError("reference price must be > 0")
    with localcontext(DECIMAL_CONTEXT):
        return (price - reference) * BPS / reference


def _index(candles: Sequence[Candle], side: str) -> tuple[str, str, dict[int, Candle]]:
    if not candles:
        raise ValueError(f"{side}: no candles")
    inst, tf = candles[0].instrument, candles[0].tf
    out: dict[int, Candle] = {}
    for c in candles:
        if c.instrument != inst or c.tf != tf:
            raise ValueError(f"{side}: mixed instruments/timeframes ({c.instrument}/{c.tf} vs {inst}/{tf})")
        if not c.is_closed:
            continue
        if c.open_time_ns in out:
            raise ValueError(f"{side}: duplicate candle at open_time_ns={c.open_time_ns}; dedup first")
        out[c.open_time_ns] = c
    return inst, tf, out


def crosscheck(binance: Sequence[Candle], kraken: Sequence[Candle],
               threshold_bps: Decimal | int | str = DEFAULT_THRESHOLD_BPS) -> CrosscheckReport:
    thr = dec(threshold_bps)
    if thr < D0:
        raise ValueError("threshold_bps must be >= 0")
    b_inst, b_tf, b = _index(binance, "binance")
    k_inst, k_tf, k = _index(kraken, "kraken")
    if b_tf != k_tf:
        raise ValueError(f"timeframes differ: {b_tf} vs {k_tf}")
    rows: list[CrosscheckRow] = []
    for t in sorted(b.keys() & k.keys()):
        cb, ck = b[t].c, k[t].c
        d = divergence_bps(cb, ck)
        with localcontext(DECIMAL_CONTEXT):
            diff = cb - ck
        rows.append(CrosscheckRow(t, cb, ck, diff, d, abs(d) > thr))
    # «відсутні» рахуються лише в спільному часовому вікні: поза ним інше джерело просто не запитували
    lo = max(min(b, default=0), min(k, default=0))
    hi = min(max(b, default=-1), max(k, default=-1))
    return CrosscheckReport(
        binance_instrument=b_inst, kraken_instrument=k_inst, tf=b_tf, threshold_bps=thr,
        rows=tuple(rows), flagged=tuple(r for r in rows if r.flagged),
        missing_in_binance=tuple(sorted(t for t in k.keys() - b.keys() if lo <= t <= hi)),
        missing_in_kraken=tuple(sorted(t for t in b.keys() - k.keys() if lo <= t <= hi)),
    )
