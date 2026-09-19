"""Автентифікація і авторизація API: JWT HS256, чотири ролі, явна матриця доступу, обмеження спроб входу.

Найменування: api/auth.py
Призначення: єдине джерело правди «хто що може» (ACCESS_MATRIX: дозвіл → множина ролей), видача і
перевірка токенів доступу (HS256, TTL із Settings.jwt_ttl_hours), обмеження частоти невдалих входів.
Автор: Андрій Жук, 2026.

Матриця — явна таблиця, а не ієрархія «analyst+»: з чотирма ролями ієрархія неоднозначна (аудитор не
«вищий» і не «нижчий» за оператора), а таблиця дослівно переноситься у звіт (підрозділ 2.9) і
перевіряється параметризованим тестом по всіх маршрутах застосунку.

Час у токені рахується від ін'єктованого годинника (порт Clock), а не від time.time() у python-jose:
перевірку exp/nbf робимо самі, тому тести на прострочення детерміновані і без sleep.
"""

from __future__ import annotations

import secrets
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from jose import JWTError, jwt

from fuzzhelm.core.enums import Role
from fuzzhelm.core.ports import Clock

JWT_ALGORITHM = "HS256"
JWT_ISSUER = "fuzzhelm"
NS_PER_S = 1_000_000_000
CLOCK_SKEW_S = 30  # допуск розсинхронізації годинників для iat/nbf (не для exp)


class Permission(StrEnum):
    """Атомарні дозволи API; кожен маршрут вимагає рівно один."""

    SELF_READ = "self:read"
    MARKET_READ = "market:read"
    STRATEGY_READ = "strategy:read"
    STRATEGY_WRITE = "strategy:write"
    BACKTEST_RUN = "backtest:run"
    RUN_READ = "run:read"
    DECISION_READ = "decision:read"
    RISK_READ = "risk:read"
    RISK_LIMITS_WRITE = "risk:limits:write"
    KILLSWITCH_RELEASE = "risk:killswitch:release"
    STREAM_READ = "stream:read"
    AUDIT_READ = "audit:read"


_ALL = frozenset(Role)
_OP = Role.OPERATOR
_AN = Role.ANALYST
_AU = Role.AUDITOR
_AD = Role.ADMIN

# Обґрунтування (docs/security.md §2):
#  * читання ринку/стратегій/прогонів/рішень/ризику — усі 4 ролі (брифінг: «analyst+»);
#  * зміна стратегій — operator (брифінг) і admin (адміністратор ⊇ оператор);
#  * запуск бектесту створює прогін (мутація) — усі, крім auditor: аудитор лише читає і не створює
#    артефактів, які сам же перевіряє (розділення обов'язків);
#  * ліміти ризику і зняття kill-switch — лише admin (брифінг §5.12, §8.1);
#  * журнал аудиту (IP, user_id) — лише auditor і admin (мінімальні привілеї).
ACCESS_MATRIX: dict[Permission, frozenset[Role]] = {
    Permission.SELF_READ: _ALL,
    Permission.MARKET_READ: _ALL,
    Permission.STRATEGY_READ: _ALL,
    Permission.STRATEGY_WRITE: frozenset({_OP, _AD}),
    Permission.BACKTEST_RUN: frozenset({_OP, _AN, _AD}),
    Permission.RUN_READ: _ALL,
    Permission.DECISION_READ: _ALL,
    Permission.RISK_READ: _ALL,
    Permission.RISK_LIMITS_WRITE: frozenset({_AD}),
    Permission.KILLSWITCH_RELEASE: frozenset({_AD}),
    Permission.STREAM_READ: _ALL,
    Permission.AUDIT_READ: frozenset({_AU, _AD}),
}

# Дозволи на зміну стану: для них роль перевіряється ще й за БД (відкликання ролі діє одразу,
# а не через 8 год, коли спливе токен).
MUTATING_PERMISSIONS: frozenset[Permission] = frozenset(
    {
        Permission.STRATEGY_WRITE,
        Permission.BACKTEST_RUN,
        Permission.RISK_LIMITS_WRITE,
        Permission.KILLSWITCH_RELEASE,
    }
)


def allowed(role: Role | str, permission: Permission) -> bool:
    return Role(role) in ACCESS_MATRIX[permission]


class AuthError(Exception):
    """Токен відсутній, невалідний або прострочений (→ HTTP 401). Причина — лише для журналу/тестів."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True, slots=True)
class Principal:
    """Автентифікований користувач запиту (з перевіреного токена)."""

    uid: int
    login: str
    role: Role
    jti: str
    exp_s: int


@dataclass(frozen=True, slots=True)
class IssuedToken:
    token: str
    expires_in_s: int
    expires_at_s: int
    jti: str


def issue_token(
    *,
    uid: int,
    login: str,
    role: Role | str,
    secret: str,
    ttl_hours: int,
    clock: Clock,
    jti: str | None = None,
) -> IssuedToken:
    """JWT HS256 з claims sub (логін), uid, role, iat, nbf, exp, iss, jti."""
    if not secret:
        raise ValueError("JWT secret must not be empty")
    if ttl_hours <= 0:
        raise ValueError("jwt_ttl_hours must be > 0")
    now_s = clock.now_ns() // NS_PER_S
    ttl_s = ttl_hours * 3600
    token_id = jti or secrets.token_hex(16)
    claims: dict[str, Any] = {
        "sub": login,
        "uid": int(uid),
        "role": Role(role).value,
        "iat": now_s,
        "nbf": now_s,
        "exp": now_s + ttl_s,
        "iss": JWT_ISSUER,
        "jti": token_id,
    }
    token = jwt.encode(claims, secret, algorithm=JWT_ALGORITHM)
    return IssuedToken(token=token, expires_in_s=ttl_s, expires_at_s=now_s + ttl_s, jti=token_id)


def decode_token(token: str, *, secret: str, clock: Clock) -> Principal:
    """Перевірити підпис (лише HS256 — alg=none і підміна алгоритму відкидаються), iss, exp/nbf за clock."""
    try:
        claims = jwt.decode(
            token,
            secret,
            algorithms=[JWT_ALGORITHM],
            issuer=JWT_ISSUER,
            options={"verify_exp": False, "verify_nbf": False, "verify_iat": False, "verify_aud": False},
        )
    except JWTError as e:
        raise AuthError(f"invalid token: {type(e).__name__}") from e
    now_s = clock.now_ns() // NS_PER_S
    try:
        exp = int(claims["exp"])
        nbf = int(claims.get("nbf", claims["iat"]))
        principal = Principal(
            uid=int(claims["uid"]),
            login=str(claims["sub"]),
            role=Role(claims["role"]),
            jti=str(claims["jti"]),
            exp_s=exp,
        )
    except (KeyError, TypeError, ValueError) as e:
        raise AuthError("token misses required claims") from e
    if now_s >= exp:
        raise AuthError("token expired")
    if now_s + CLOCK_SKEW_S < nbf:
        raise AuthError("token not yet valid")
    return principal


class LoginRateLimiter:
    """Ковзне вікно невдалих входів: не більше `max_failures` за `window_s` на IP і на логін.

    Захист від перебору паролів (STRIDE: Spoofing). Лише невдалі спроби витрачають ліміт; успішний
    вхід очищає лічильник логіна. `now_s` ін'єктується (монотонний годинник), тож тести без sleep.
    """

    def __init__(
        self,
        *,
        max_failures: int = 10,
        window_s: float = 300.0,
        now_s: Callable[[], float] | None = None,
        max_keys: int = 10_000,
    ) -> None:
        if max_failures <= 0 or window_s <= 0:
            raise ValueError("max_failures and window_s must be > 0")
        self.max_failures = max_failures
        self.window_s = window_s
        self._now = now_s or _monotonic_s
        self._max_keys = max_keys
        self._fails: dict[str, deque[float]] = {}

    def _prune(self, key: str, now: float) -> deque[float]:
        q = self._fails.get(key)
        if q is None:
            return deque()
        while q and now - q[0] >= self.window_s:
            q.popleft()
        if not q:
            del self._fails[key]
        return q

    def retry_after_s(self, *keys: str) -> float:
        """0 — можна пробувати; інакше скільки секунд чекати (для заголовка Retry-After)."""
        now = self._now()
        wait = 0.0
        for key in keys:
            q = self._prune(key, now)
            if len(q) >= self.max_failures:
                wait = max(wait, self.window_s - (now - q[0]))
        return wait

    def record_failure(self, *keys: str) -> None:
        now = self._now()
        if len(self._fails) >= self._max_keys:
            # межа пам'яті під час розподіленого перебору: викидаємо найстаріші ключі
            for k in list(self._fails)[: self._max_keys // 10]:
                del self._fails[k]
        for key in keys:
            self._fails.setdefault(key, deque()).append(now)

    def reset(self, key: str) -> None:
        self._fails.pop(key, None)


def _monotonic_s() -> float:
    return time.monotonic()
