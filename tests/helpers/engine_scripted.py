"""Тестові утиліти рушія: заскриптований намір ядра і побудова наборів даних із реальних барів.

Найменування: tests/helpers/engine_scripted.py
Призначення: детерміновані сценарії для TradingLoop (вхід/розворот/вето/фандинг) без залежності від
    того, що саме видасть нечітке ядро на фікстурі; синтетичний flash-crash з реальних барів.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from decimal import Decimal
from functools import cache

import numpy as np

from fuzzhelm.backtest.dataset import Dataset, load_fixture_dataset
from fuzzhelm.backtest.engine import BacktestConfig, BacktestResult, TradingLoop, run_backtest
from fuzzhelm.features.window import BarWindow
from fuzzhelm.sizing.convert import price_to_decimal


class ScriptedCore:
    """Підміна DecisionCore: u_final за абсолютним індексом бару (window.t), κ = 1."""

    def __init__(self, script: Mapping[int, float] | Callable[[int], float], default: float = 0.0) -> None:
        self._script = script
        self._default = default
        self.calls: list[int] = []

    def intent(self, window: BarWindow) -> tuple[float, float, float]:
        t = window.t
        self.calls.append(t)
        u = self._script(t) if callable(self._script) else self._script.get(t, self._default)
        return u, 1.0, u

    def decide(self, window: BarWindow, open_time_ns: int | None = None) -> object:
        raise AssertionError("scripted core produces no traces; use record_traces='none'")


@cache
def fixture() -> Dataset:
    """3000 реальних 1m-барів BTCUSDT (кешовано на процес)."""
    return load_fixture_dataset()


@cache
def base_config() -> BacktestConfig:
    """Конфіг з файлів config/ (читається один раз)."""
    return BacktestConfig()


@cache
def fixture_run(n_bars: int | None = None, record_traces: str = "trades",
                seed: int = 20260918) -> BacktestResult:
    """run_backtest(перші n_bars фікстури, base_config() з record_traces, seed) — один прогін на процес.

    Прогін детермінований за (дані, конфігурація, seed) — це перевіряють test_backtest_engine.py
    (незалежний повторний прогін) і test_riskfix_var.py; результат лише читається, тож тести, що потребують
    саме цього прогону, ділять його замість повторного обчислення.
    """
    ds = fixture() if n_bars is None else fixture().slice(0, n_bars)
    return run_backtest(ds, base_config().with_params(record_traces=record_traces), seed=seed)


def scripted_loop(script: Mapping[int, float] | Callable[[int], float], *, warmup: int = 30, seed: int = 1,
                  **overrides: object) -> TradingLoop:
    params: dict[str, object] = {"warmup_bars": warmup, "record_traces": "none", "check_invariants": True}
    params.update(overrides)
    cfg = base_config().with_params(**params)
    loop = TradingLoop(fixture().instrument, cfg, seed=seed)
    loop.core = ScriptedCore(script)  # type: ignore[assignment]
    return loop


def run_loop(loop: TradingLoop, ds: Dataset, stop: int | None = None) -> None:
    for i, (bar, dbar, close_ns) in enumerate(ds.feed()):
        if stop is not None and i >= stop:
            break
        loop.step(bar, close_ns, dbar=dbar)


def crash_dataset(ds: Dataset, k: int, factor: str = "0.75") -> Dataset:
    """Бари 0..k без змін; з бару k+1 ціни × factor (кратно tick) — розрив униз на відкритті k+1."""
    tick = ds.instrument.tick_size
    f = Decimal(factor)

    def scaled(col: np.ndarray) -> np.ndarray:
        out = col.copy()
        for j in range(k + 1, len(col)):
            out[j] = float(price_to_decimal(float(col[j]), tick) * f // tick * tick)
        return out

    return Dataset.from_arrays(ds.instrument, tf=ds.tf, t_ns=ds.t_ns, o=scaled(ds.o), h=scaled(ds.h),
                               l=scaled(ds.l), c=scaled(ds.c), v=ds.v, qv=ds.qv, n=ds.n, source="flash_crash")
