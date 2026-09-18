"""Walk-forward розбиття з embargo: IS → embargo → OOS, зсув на крок, k фолдів.

Найменування: backtest/walkforward.py
Призначення: чесна поза-вибіркова оцінка: параметри підбираються на IS, оцінюються на OOS, а
embargo між ними не дає ознакам з вікном max_lookback «підглянути» в OOS (брифінг §5.16).
Автор: Андрій Жук, 2026.

Розбиття (індекси барів, напіввідкриті інтервали [start, end)), s_i = offset + i·step:
    IS_i      = [s_i,                 s_i + is_bars − E)
    embargo_i = [s_i + is_bars − E,   s_i + is_bars)
    OOS_i     = [s_i + is_bars,       s_i + is_bars + oos_bars)
Embargo вирізається з КІНЦЯ IS-вікна (а не зсуває OOS), тому OOS-вікна при step = oos_bars
лягають суцільно і для профілю 15/5/5 днів × 6 покривають рівно 45 днів (deviations.md D-03).
Інваріант (перевіряє test_walkforward_embargo_no_overlap):
    (IS_i ∪ E_i) ∩ OOS_i = ∅   ∧   max(IS_i.t) + E ≤ min(OOS_i.t)
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from fuzzhelm.config import load_yaml

BARS_PER_DAY_1M = 1440


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    is_start: int
    is_end: int            # виключно
    embargo_start: int
    embargo_end: int       # виключно (= oos_start)
    oos_start: int
    oos_end: int           # виключно

    @property
    def is_range(self) -> range:
        return range(self.is_start, self.is_end)

    @property
    def embargo_range(self) -> range:
        return range(self.embargo_start, self.embargo_end)

    @property
    def oos_range(self) -> range:
        return range(self.oos_start, self.oos_end)

    @property
    def embargo_bars(self) -> int:
        return self.embargo_end - self.embargo_start


def make_folds(  # noqa: PLR0917 — позиційна сигнатура зафіксована в contracts.md §13
    n_bars: int,
    is_bars: int,
    oos_bars: int,
    step_bars: int,
    embargo_bars: int,
    k: int,
    *,
    anchor: Literal["start", "end"] = "start",
) -> list[Fold]:
    """k фолдів walk-forward.

    anchor="start" — перший IS починається з бару 0; "end" — останній OOS закінчується на n_bars.
    """
    if min(is_bars, oos_bars, step_bars, k) <= 0:
        raise ValueError("is_bars, oos_bars, step_bars and k must be > 0")
    if embargo_bars < 0:
        raise ValueError("embargo_bars must be >= 0")
    if embargo_bars >= is_bars:
        raise ValueError(f"embargo ({embargo_bars}) must be shorter than IS window ({is_bars})")
    span = (k - 1) * step_bars + is_bars + oos_bars
    if span > n_bars:
        raise ValueError(f"{k} folds need {span} bars, only {n_bars} available")
    offset = 0 if anchor == "start" else n_bars - span
    folds: list[Fold] = []
    for i in range(k):
        s = offset + i * step_bars
        is_end = s + is_bars - embargo_bars
        oos_start = s + is_bars
        folds.append(Fold(index=i, is_start=s, is_end=is_end, embargo_start=is_end,
                          embargo_end=oos_start, oos_start=oos_start, oos_end=oos_start + oos_bars))
    return folds


def folds_from_profile(
    n_bars: int,
    *,
    max_lookback: int,
    bars_per_day: int = BARS_PER_DAY_1M,
    profile: Mapping[str, Any] | None = None,
) -> list[Fold]:
    """Фолди за config/profiles/backtest.yaml (секція walkforward); embargo null → 2·max_lookback."""
    prof = profile if profile is not None else load_yaml("profiles/backtest")
    wf = prof["walkforward"]
    emb = wf.get("embargo_bars")
    embargo = 2 * max_lookback if emb is None else int(emb)
    return make_folds(
        n_bars,
        is_bars=int(wf["is_days"]) * bars_per_day,
        oos_bars=int(wf["oos_days"]) * bars_per_day,
        step_bars=int(wf["step_days"]) * bars_per_day,
        embargo_bars=embargo,
        k=int(wf["folds"]),
    )
