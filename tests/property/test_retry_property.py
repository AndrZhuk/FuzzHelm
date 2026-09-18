"""C11 [property]: повний джитер backoff завжди в межах [0, min(cap, base·2^attempt)].

Найменування: tests/property/test_retry_property.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import math
from fractions import Fraction

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.ingest.retry import RetryableError, RetryExhaustedError, RetryPolicy


@pytest.mark.property
@settings(max_examples=300)
@given(base=st.floats(1e-300, 10.0), cap=st.floats(1e-3, 300.0), attempt=st.integers(0, 2000),
       seed=st.integers(0, 2**63 - 1))
def test_backoff_full_jitter_within_cap(base: float, cap: float, attempt: int, seed: int) -> None:
    p = RetryPolicy(base=base, cap=cap, max_attempts=3, rng_seed=seed)
    # межу min(cap, base·2^attempt) рахуємо незалежно й ТОЧНО (раціональні числа, без переповнення float
    # і без припущення «для великих attempt це вже cap» — з крихітним base воно хибне)
    exact = Fraction(base) * 2**attempt
    bound = cap if exact >= Fraction(cap) else float(exact)
    assert p.ceiling(attempt) == bound
    delays = [p.backoff(attempt) for _ in range(16)]
    assert all(0.0 <= d <= bound and d <= cap for d in delays)
    # відтворюваність: той самий seed → та сама послідовність пауз
    q = RetryPolicy(base=base, cap=cap, max_attempts=3, rng_seed=seed)
    assert [q.backoff(attempt) for _ in range(16)] == delays


@pytest.mark.property
@settings(max_examples=100)
@given(n_fail=st.integers(0, 6), seed=st.integers(0, 2**32 - 1))
async def test_execute_sleeps_only_within_jitter_bounds(n_fail: int, seed: int) -> None:
    p = RetryPolicy(base=0.25, cap=2.0, max_attempts=5, rng_seed=seed)
    calls = 0
    slept: list[float] = []

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls <= n_fail:
            raise RetryableError("transient")
        return "ok"

    async def fake_sleep(dt: float) -> None:
        slept.append(dt)

    if n_fail < p.max_attempts:
        assert await p.execute(flaky, sleep=fake_sleep) == "ok"
        assert calls == n_fail + 1
    else:
        with pytest.raises(RetryExhaustedError):
            await p.execute(flaky, sleep=fake_sleep)
        assert calls == p.max_attempts
    assert len(slept) == min(n_fail, p.max_attempts - 1)
    assert all(0.0 <= d <= min(2.0, 0.25 * 2**k) for k, d in enumerate(slept))


def test_jitter_is_full_not_equal_or_decorrelated() -> None:
    """«Повний» джитер розподілений по ВСЬОМУ [0, межа): вибірка з фіксованим seed покриває обидва краї
    й має середнє ≈ межа/2 (на відміну від «рівного» джитера, де все ≥ межа/2)."""
    p = RetryPolicy(base=1.0, cap=8.0, max_attempts=3, rng_seed=20260918)
    xs = [p.backoff(10) for _ in range(4000)]         # межа = min(8, 1024) = 8
    assert min(xs) < 0.1 and max(xs) > 7.9
    assert sum(1 for x in xs if x < 4.0) > 1800        # ≈ половина — нижче середини інтервалу
    assert abs(sum(xs) / len(xs) - 4.0) < 0.2


def test_ceiling_exact_for_tiny_base_and_rejects_non_finite() -> None:
    # base·2^62 < cap: стара евристика «attempt ≥ 62 → cap» дала б U(0, 30) замість U(0, ~4.6e−10)
    p = RetryPolicy(base=1e-28, cap=30.0, rng_seed=1)
    assert p.ceiling(62) == math.ldexp(1e-28, 62) < 1e-9
    assert all(0.0 <= p.backoff(62) <= math.ldexp(1e-28, 62) for _ in range(100))
    assert p.ceiling(5000) == 30.0                    # переповнення float → cap
    for bad in (float("nan"), float("inf"), 0.0, -1.0):
        with pytest.raises(ValueError):
            RetryPolicy(base=bad)
        with pytest.raises(ValueError):
            RetryPolicy(cap=bad)
