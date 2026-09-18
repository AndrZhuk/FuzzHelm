"""Група D брифінгу (§10), property: скор якості Q лежить у [0, 1] для будь-яких входів.

Найменування: tests/property/test_quality_property.py
Призначення: Q — опукла комбінація чотирьох компонент, кожна обрізана до [0, 1]; перевірка на
довільних (у т.ч. «неможливих»: N_obs > N_exp, N_invalid > N_total, прогалина > години) входах і
довільних допустимих вагах (невід'ємні, Σ = 1), а також на фактичних AHP-вагах з конфігурації.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import given
from hypothesis import strategies as st

from fuzzhelm.quality.dq_score import DqInputs, dq_score, load_dq_weights

pytestmark = pytest.mark.property

counts = st.integers(min_value=0, max_value=10_000)
seconds = st.floats(min_value=0.0, max_value=1e6, allow_nan=False, allow_infinity=False)
lag_ms = st.floats(min_value=-1e4, max_value=1e7, allow_nan=False, allow_infinity=False)


@st.composite
def simplex_weights(draw: st.DrawFn) -> tuple[float, float, float, float]:
    raw = draw(st.lists(st.floats(min_value=0.0, max_value=1.0, allow_nan=False), min_size=4, max_size=4)
               .filter(lambda xs: sum(xs) > 1e-6))
    s = math.fsum(raw)
    w = [x / s for x in raw]
    return w[0], w[1], w[2], w[3]


@st.composite
def dq_inputs(draw: st.DrawFn) -> DqInputs:
    return DqInputs(
        expected_buckets=draw(counts), observed_buckets=draw(counts), total_count=draw(counts),
        invalid_count=draw(counts), anomaly_count=draw(counts), gap_seconds=draw(seconds),
        lag_p95_ms=draw(lag_ms),
    )


@given(inp=dq_inputs(), w=simplex_weights())
def test_dq_score_in_unit_interval(inp: DqInputs, w: tuple[float, float, float, float]) -> None:
    for weights in (w, load_dq_weights()):
        s = dq_score(inp, weights)
        comps = (s.completeness, s.validity, s.timeliness, s.continuity)
        assert all(0.0 <= x <= 1.0 for x in comps), comps
        assert 0.0 <= s.score <= 1.0
        # опуклість: Q між найменшою і найбільшою компонентою
        assert min(comps) - 1e-12 <= s.score <= max(comps) + 1e-12


@given(inp=dq_inputs(), extra=st.integers(min_value=1, max_value=100))
def test_dq_score_monotone_in_invalid_count(inp: DqInputs, extra: int) -> None:
    """Більше невалідних подій за тих самих інших умов ніколи не підвищує Q."""
    w = load_dq_weights()
    worse = DqInputs(inp.expected_buckets, inp.observed_buckets, inp.total_count, inp.invalid_count + extra,
                     inp.anomaly_count, inp.gap_seconds, inp.lag_p95_ms)
    assert dq_score(worse, w).score <= dq_score(inp, w).score + 1e-15
