"""Автентифікація і авторизація API: JWT HS256, матриця доступу, обмеження спроб входу.

Найменування: tests/unit/test_auth.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import pytest
from jose import jwt

from fuzzhelm.api.auth import (
    ACCESS_MATRIX,
    CLOCK_SKEW_S,
    JWT_ISSUER,
    MUTATING_PERMISSIONS,
    AuthError,
    LoginRateLimiter,
    Permission,
    allowed,
    decode_token,
    issue_token,
)
from fuzzhelm.core.clock import FixedClock, ManualClock
from fuzzhelm.core.enums import Role

SECRET = "unit-test-secret-" + "k" * 32
NS = 1_000_000_000
T0 = 1_758_153_600 * NS


def test_issue_and_decode_roundtrip_claims() -> None:
    clock = FixedClock(T0)
    tok = issue_token(
        uid=7, login="olena", role=Role.OPERATOR, secret=SECRET, ttl_hours=8, clock=clock, jti="abc"
    )
    assert tok.expires_in_s == 8 * 3600 and tok.expires_at_s == T0 // NS + 8 * 3600
    p = decode_token(tok.token, secret=SECRET, clock=clock)
    assert (p.uid, p.login, p.role, p.jti, p.exp_s) == (7, "olena", Role.OPERATOR, "abc", tok.expires_at_s)
    claims = jwt.get_unverified_claims(tok.token)
    assert (
        set(claims) == {"sub", "uid", "role", "iat", "nbf", "exp", "iss", "jti"}
        and claims["iss"] == JWT_ISSUER
    )


def test_decode_rejects_other_algorithm_other_secret_and_issuer() -> None:
    clock = FixedClock(T0)
    good = jwt.get_unverified_claims(
        issue_token(uid=1, login="a", role="admin", secret=SECRET, ttl_hours=1, clock=clock).token
    )
    for token in (
        jwt.encode(good, SECRET, algorithm="HS512"),  # інший алгоритм тим самим ключем
        jwt.encode(good, "другий-секрет", algorithm="HS256"),  # чужий ключ
        jwt.encode({**good, "iss": "evil"}, SECRET, algorithm="HS256"),
        jwt.encode({k: v for k, v in good.items() if k != "uid"}, SECRET, algorithm="HS256"),
        jwt.encode({**good, "role": "root"}, SECRET, algorithm="HS256"),  # роль поза Role
    ):
        with pytest.raises(AuthError):
            decode_token(token, secret=SECRET, clock=clock)


def test_expiry_and_not_before_use_injected_clock() -> None:
    clock = ManualClock(T0)
    tok = issue_token(uid=1, login="a", role="analyst", secret=SECRET, ttl_hours=1, clock=clock).token
    clock.set(T0 + 3600 * NS - 1)
    assert decode_token(tok, secret=SECRET, clock=clock).login == "a"  # остання наносекунда TTL
    clock.set(T0 + 3600 * NS)
    with pytest.raises(AuthError, match="expired"):
        decode_token(tok, secret=SECRET, clock=clock)
    # токен «з майбутнього» (годинник видавця попереду більше ніж на допуск) — ще не дійсний
    future = issue_token(
        uid=1,
        login="a",
        role="analyst",
        secret=SECRET,
        ttl_hours=1,
        clock=FixedClock(T0 + (CLOCK_SKEW_S + 1) * NS),
    ).token
    with pytest.raises(AuthError, match="not yet valid"):
        decode_token(future, secret=SECRET, clock=FixedClock(T0))
    within_skew = issue_token(
        uid=1, login="a", role="analyst", secret=SECRET, ttl_hours=1, clock=FixedClock(T0 + CLOCK_SKEW_S * NS)
    ).token
    assert decode_token(within_skew, secret=SECRET, clock=FixedClock(T0)).uid == 1


def test_issue_token_validates_inputs() -> None:
    with pytest.raises(ValueError):
        issue_token(uid=1, login="a", role="admin", secret="", ttl_hours=1, clock=FixedClock(T0))
    with pytest.raises(ValueError):
        issue_token(uid=1, login="a", role="admin", secret=SECRET, ttl_hours=0, clock=FixedClock(T0))
    with pytest.raises(ValueError):
        issue_token(uid=1, login="a", role="superuser", secret=SECRET, ttl_hours=1, clock=FixedClock(T0))


def test_access_matrix_is_total_and_auditor_is_read_only() -> None:
    assert set(ACCESS_MATRIX) == set(Permission)  # кожен дозвіл має рядок
    assert all(ACCESS_MATRIX[p] for p in Permission)  # і хоч одну роль
    for perm in MUTATING_PERMISSIONS:
        assert not allowed(Role.AUDITOR, perm), perm  # аудитор нічого не змінює
        assert allowed(Role.ADMIN, perm), perm
    # admin ⊇ будь-яка інша роль (адміністратор не втрачає жодного дозволу)
    for role in Role:
        assert {p for p in Permission if allowed(role, p)} <= {
            p for p in Permission if allowed(Role.ADMIN, p)
        }
    assert {p for p in Permission if allowed(Role.ANALYST, p)} & MUTATING_PERMISSIONS == {
        Permission.BACKTEST_RUN
    }


def test_login_rate_limiter_window_and_reset() -> None:
    now = [0.0]
    lim = LoginRateLimiter(max_failures=3, window_s=60.0, now_s=lambda: now[0])
    for _ in range(3):
        assert lim.retry_after_s("ip:1", "login:x") == 0.0
        lim.record_failure("ip:1", "login:x")
        now[0] += 1.0
    assert lim.retry_after_s("ip:1") == pytest.approx(57.0)  # перша невдача була в t=0
    assert lim.retry_after_s("ip:2", "login:x") > 0  # інший IP, той самий логін
    lim.reset("login:x")
    assert lim.retry_after_s("ip:2", "login:x") == 0.0 and lim.retry_after_s("ip:1") > 0
    now[0] = 60.0
    assert lim.retry_after_s("ip:1") == 0.0  # найстаріша невдача вийшла з вікна
    with pytest.raises(ValueError):
        LoginRateLimiter(max_failures=0)


def test_login_rate_limiter_memory_is_bounded() -> None:
    lim = LoginRateLimiter(max_failures=1, window_s=60.0, now_s=lambda: 0.0, max_keys=100)
    for i in range(1_000):
        lim.record_failure(f"ip:{i}")
    assert len(lim._fails) <= 100
