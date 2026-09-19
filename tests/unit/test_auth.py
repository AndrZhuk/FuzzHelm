"""Автентифікація і авторизація API: JWT HS256, матриця доступу, обмеження спроб входу.

Найменування: tests/unit/test_auth.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import jwt
import pytest
from passlib.context import CryptContext

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
from fuzzhelm.api.main import weak_jwt_secret
from fuzzhelm.api.routers.auth import check_password
from fuzzhelm.core.clock import FixedClock, ManualClock
from fuzzhelm.core.enums import Role
from fuzzhelm.storage.repositories import UserRow
from fuzzhelm.storage.repositories.user import hash_password

SECRET = "unit-test-secret-" + "k" * 32
OTHER_SECRET = "другий-секрет-" + "q" * 32  # ≥ 32 байти: PyJWT не попереджає про короткий ключ HMAC
NS = 1_000_000_000
T0 = 1_758_153_600 * NS

# той самий ключ SECRET (49 байт) під HS384/HS512 — PyJWT попереджає про довжину; це і є суть підробки
SAME_KEY_OTHER_HMAC = pytest.mark.filterwarnings("ignore::jwt.InsecureKeyLengthWarning")


def unverified_claims(token: str) -> dict[str, Any]:
    """Payload без перевірки підпису (лише для побудови підробок у тестах)."""
    return dict(jwt.decode(token, options={"verify_signature": False}))


def b64url(obj: dict[str, Any] | bytes) -> str:
    raw = obj if isinstance(obj, bytes) else json.dumps(obj, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def test_issue_and_decode_roundtrip_claims() -> None:
    clock = FixedClock(T0)
    tok = issue_token(
        uid=7, login="olena", role=Role.OPERATOR, secret=SECRET, ttl_hours=8, clock=clock, jti="abc"
    )
    assert tok.expires_in_s == 8 * 3600 and tok.expires_at_s == T0 // NS + 8 * 3600
    p = decode_token(tok.token, secret=SECRET, clock=clock)
    assert (p.uid, p.login, p.role, p.jti, p.exp_s) == (7, "olena", Role.OPERATOR, "abc", tok.expires_at_s)
    claims = unverified_claims(tok.token)
    assert (
        set(claims) == {"sub", "uid", "role", "iat", "nbf", "exp", "iss", "jti"}
        and claims["iss"] == JWT_ISSUER
    )


@SAME_KEY_OTHER_HMAC
def test_decode_rejects_other_algorithm_other_secret_and_issuer() -> None:
    clock = FixedClock(T0)
    good = unverified_claims(
        issue_token(uid=1, login="a", role="admin", secret=SECRET, ttl_hours=1, clock=clock).token
    )
    for token in (
        jwt.encode(good, SECRET, algorithm="HS512"),  # інший алгоритм тим самим ключем
        jwt.encode(good, OTHER_SECRET, algorithm="HS256"),  # чужий ключ
        jwt.encode({**good, "iss": "evil"}, SECRET, algorithm="HS256"),
        jwt.encode({k: v for k, v in good.items() if k != "uid"}, SECRET, algorithm="HS256"),
        jwt.encode({**good, "role": "root"}, SECRET, algorithm="HS256"),  # роль поза Role
    ):
        with pytest.raises(AuthError):
            decode_token(token, secret=SECRET, clock=clock)


@SAME_KEY_OTHER_HMAC
def test_token_with_other_algorithm_or_alg_none_is_rejected() -> None:
    """PyJWT з algorithms=["HS256"]: alg=none (з підписом і без, у будь-якому регістрі), інші HMAC
    (HS384/HS512 тим самим ключем), асиметричні заголовки (RS256/ES256 — атака підміни алгоритму) і
    заголовок без alg відкидаються до перевірки підпису; ідентичні claims під HS256 — приймаються."""
    clock = FixedClock(T0)
    good = issue_token(uid=1, login="a", role="admin", secret=SECRET, ttl_hours=1, clock=clock).token
    claims = unverified_claims(good)
    assert jwt.get_unverified_header(good)["alg"] == "HS256"
    assert decode_token(good, secret=SECRET, clock=clock).role is Role.ADMIN
    body = b64url(claims)
    sig = good.rsplit(".", 1)[1]
    forged = [
        jwt.encode(claims, SECRET, algorithm="HS384"),
        jwt.encode(claims, SECRET, algorithm="HS512"),
        jwt.encode(claims, None, algorithm="none"),  # PyJWT уміє видати unsecured JWT — прийняти не має
        f"{b64url({'alg': 'none', 'typ': 'JWT'})}.{body}.",
        f"{b64url({'alg': 'None', 'typ': 'JWT'})}.{body}.",
        f"{b64url({'alg': 'NONE', 'typ': 'JWT'})}.{body}.{sig}",
        f"{b64url({'alg': 'RS256', 'typ': 'JWT'})}.{body}.{b64url(b'x' * 256)}",
        f"{b64url({'alg': 'ES256', 'typ': 'JWT'})}.{body}.{b64url(b'x' * 64)}",
        f"{b64url({'typ': 'JWT'})}.{body}.{sig}",
    ]
    for token in forged:
        with pytest.raises(AuthError, match="invalid token"):
            decode_token(token, secret=SECRET, clock=clock)


def test_claims_are_required_and_typed() -> None:
    """Без nbf/iat/jti/sub/exp/iss або з нецілим exp/uid (bool, дріб, рядок) токен відкидається, навіть
    із правильним підписом."""
    clock = FixedClock(T0)
    good = unverified_claims(
        issue_token(uid=1, login="a", role="admin", secret=SECRET, ttl_hours=1, clock=clock).token
    )
    variants: list[dict[str, Any]] = [{k: v for k, v in good.items() if k != name}
                                      for name in ("nbf", "iat", "jti", "sub", "exp", "iss")]
    variants += [{**good, "uid": True}, {**good, "uid": "1"}, {**good, "exp": good["exp"] + 0.5},
                 {**good, "exp": str(good["exp"])}]
    for bad in variants:
        with pytest.raises(AuthError):
            decode_token(jwt.encode(bad, SECRET, algorithm="HS256"), secret=SECRET, clock=clock)
    # iat «з майбутнього» за межею допуску — ще не дійсний, навіть якщо nbf у минулому
    future_iat = {**good, "iat": good["iat"] + CLOCK_SKEW_S + 1}
    with pytest.raises(AuthError, match="not yet valid"):
        decode_token(jwt.encode(future_iat, SECRET, algorithm="HS256"), secret=SECRET, clock=clock)


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


def test_check_password_same_semantics_as_repo_authenticate() -> None:
    """Перевірка пароля для /auth/login (виконується в потоці): невідомий логін, хибний пароль і пароль
    довший за 72 байти bcrypt — однаково False; правильний — True."""
    ctx = CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)
    pwd_hash = hash_password("s3cret-pass", ctx)
    user = UserRow(id=1, login="admin", pwd_hash=pwd_hash, role="admin", created_at_ns=0)
    assert check_password(user, "s3cret-pass", ctx) is True
    assert check_password(user, "wrong", ctx) is False
    assert check_password(user, "s3cret-pass" + "x" * 80, ctx) is False  # bcrypt обрізав би хвіст
    assert check_password(None, "s3cret-pass", ctx) is False  # dummy_verify, без винятку
    assert check_password(UserRow(1, "admin", None, "admin", 0), "s3cret-pass", ctx) is False


def test_weak_jwt_secret_detects_dev_defaults_and_short_keys() -> None:
    assert weak_jwt_secret("dev-only-change-me") and weak_jwt_secret("change-me") and weak_jwt_secret("")
    assert weak_jwt_secret("x" * 31)  # < 256 біт для HS256
    assert not weak_jwt_secret(SECRET) and len(SECRET) >= 32
