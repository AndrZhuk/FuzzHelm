"""Telegram-нотифікатор: сигнали, ризик-події, kill-switch, розриви WS — з дедуплікацією і rate-limit.

Найменування: notify/telegram.py
Призначення: доставити оператору на телефон події, що потребують уваги, не заваливши чат і не зупинивши
торговий цикл: send() ніколи не кидає виняток і не чекає (нотифікація — best effort), повтор того самого
ключа в межах вікна придушується, частоту обмежує token bucket (Telegram: ~20 повідомлень/хв у групу).
Автор: Андрій Жук, 2026.

Секрети: токен бота — лише Settings.telegram_bot_token (SecretStr, з .env), у журнал не потрапляє. httpx
пише в журнал URL запиту (а в URL Bot API є токен: /bot<token>/sendMessage), тому на логери httpx/httpcore
ставиться фільтр, що замінює токен на «***»; тексти помилок формуються без URL. Без токена або chat_id
нотифікатор — no-op (DISABLED), бо кроки з ботом робить людина (брифінг §12.9).
Хост — лише https://api.telegram.org (allowlist, як і для біржі).
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import IntEnum, StrEnum
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import SecretStr

from fuzzhelm.config import Settings
from fuzzhelm.notify import templates

log = logging.getLogger(__name__)

TELEGRAM_API_BASE = "https://api.telegram.org"
ALLOWED_TELEGRAM_HOSTS: frozenset[str] = frozenset({"api.telegram.org"})
DEFAULT_DEDUP_WINDOW_S = 300.0
DEFAULT_RATE_CAPACITY = 20.0
DEFAULT_RATE_PER_S = 20.0 / 60.0
REDACTED = "***"


class NotifyKind(StrEnum):
    SIGNAL = "signal"
    RISK_EVENT = "risk_event"
    RISK_STATE = "risk_state"
    KILLSWITCH = "killswitch"
    WS_DISCONNECT = "ws_disconnect"
    REPORT = "report"


class Priority(IntEnum):
    NORMAL = 0
    CRITICAL = 1  # kill-switch: оминає token bucket (але не дедуплікацію)


class SendStatus(StrEnum):
    SENT = "SENT"
    DISABLED = "DISABLED"
    DUPLICATE = "DUPLICATE"
    RATE_LIMITED = "RATE_LIMITED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Notification:
    kind: NotifyKind
    key: str  # ключ дедуплікації: однакові ключі в межах вікна — одне повідомлення
    text: str
    priority: Priority = Priority.NORMAL


@dataclass(frozen=True, slots=True)
class SendResult:
    status: SendStatus
    key: str
    http_status: int | None = None
    error: str | None = None


def assert_telegram_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in ALLOWED_TELEGRAM_HOSTS:
        raise ValueError(f"Telegram base URL must be https://api.telegram.org, got host {parsed.hostname!r}")
    return url.rstrip("/")


class RateBucket:
    """Token bucket без очікування: try_take() одразу каже «так/ні» (надлишок відкидається, а не чекає)."""

    def __init__(self, capacity: float, refill_per_s: float, now_s: Callable[[], float]) -> None:
        if capacity <= 0 or refill_per_s <= 0:
            raise ValueError("capacity and refill_per_s must be > 0")
        self.capacity = capacity
        self.refill_per_s = refill_per_s
        self._now = now_s
        self._tokens = capacity
        self._t = now_s()

    def _refill(self) -> None:
        now = self._now()
        if now > self._t:
            self._tokens = min(self.capacity, self._tokens + (now - self._t) * self.refill_per_s)
        self._t = max(self._t, now)

    @property
    def tokens(self) -> float:
        self._refill()
        return self._tokens

    def try_take(self, n: float = 1.0) -> bool:
        self._refill()
        if self._tokens >= n:
            self._tokens -= n
            return True
        return False


class Deduplicator:
    """Останній час відправки за ключем; той самий ключ у межах вікна — дублікат. Пам'ять обмежена."""

    def __init__(self, window_s: float, now_s: Callable[[], float], max_keys: int = 4096) -> None:
        self.window_s = window_s
        self._now = now_s
        self._max = max_keys
        self._last: OrderedDict[str, float] = OrderedDict()

    def is_duplicate(self, key: str) -> bool:
        t = self._last.get(key)
        return t is not None and self._now() - t < self.window_s

    def mark(self, key: str) -> None:
        self._last[key] = self._now()
        self._last.move_to_end(key)
        while len(self._last) > self._max:
            self._last.popitem(last=False)


class _RedactFilter(logging.Filter):
    """Замінює секрет у повідомленнях сторонніх логерів (httpx пише URL із /bot<token>/)."""

    def __init__(self, secret: str) -> None:
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        if self.secret in msg:
            record.msg = msg.replace(self.secret, REDACTED)
            record.args = None
        return True


_INSTALLED_FILTERS: dict[str, _RedactFilter] = {}
REDACTED_LOGGERS = ("httpx", "httpcore")


def install_redaction(secret: str) -> None:
    """Ідемпотентно поставити фільтр-редактор секрету на логери httpx/httpcore."""
    if not secret or secret in _INSTALLED_FILTERS:
        return
    flt = _RedactFilter(secret)
    _INSTALLED_FILTERS[secret] = flt
    for name in REDACTED_LOGGERS:
        logging.getLogger(name).addFilter(flt)


class TelegramNotifier:
    def __init__(
        self,
        token: SecretStr | None,
        chat_id: str | None,
        *,
        http: httpx.AsyncClient | None = None,
        base_url: str = TELEGRAM_API_BASE,
        dedup_window_s: float = DEFAULT_DEDUP_WINDOW_S,
        rate_capacity: float = DEFAULT_RATE_CAPACITY,
        rate_per_s: float = DEFAULT_RATE_PER_S,
        now_s: Callable[[], float] | None = None,
        timeout_s: float = 5.0,
    ) -> None:
        self._token = token
        self.chat_id = chat_id
        self.base_url = assert_telegram_url(base_url)
        self._now = now_s or time.monotonic
        self._http = http
        self._own_http = False
        self._timeout = timeout_s
        self.dedup = Deduplicator(dedup_window_s, self._now)
        self.bucket = RateBucket(rate_capacity, rate_per_s, self._now)
        self._blocked_until = 0.0
        self.counters: dict[str, int] = {s.value: 0 for s in SendStatus}
        secret = self._secret()
        if secret:
            install_redaction(secret)

    @classmethod
    def from_settings(cls, settings: Settings, **kwargs: Any) -> TelegramNotifier:
        return cls(settings.telegram_bot_token, settings.telegram_chat_id, **kwargs)

    def _secret(self) -> str:
        return self._token.get_secret_value() if self._token is not None else ""

    @property
    def enabled(self) -> bool:
        return bool(self._secret()) and bool(self.chat_id)

    def __repr__(self) -> str:  # токен не з'являється навіть у repr/налагодженні
        return f"TelegramNotifier(enabled={self.enabled}, chat_id={'set' if self.chat_id else None})"

    def _result(self, status: SendStatus, key: str, **kw: Any) -> SendResult:
        self.counters[status.value] += 1
        return SendResult(status=status, key=key, **kw)

    async def send(self, n: Notification) -> SendResult:
        """Надіслати або чесно пояснити, чому ні. Ніколи не кидає і не чекає на rate-limit."""
        if not self.enabled:
            return self._result(SendStatus.DISABLED, n.key)
        if self.dedup.is_duplicate(n.key):
            return self._result(SendStatus.DUPLICATE, n.key)
        now = self._now()
        if now < self._blocked_until:
            return self._result(SendStatus.RATE_LIMITED, n.key, error="telegram retry_after in effect")
        if not self.bucket.try_take() and n.priority < Priority.CRITICAL:
            return self._result(SendStatus.RATE_LIMITED, n.key, error="local token bucket empty")
        return await self._post(n)

    async def _post(self, n: Notification) -> SendResult:
        url = f"{self.base_url}/bot{self._secret()}/sendMessage"
        body = {"chat_id": self.chat_id, "text": templates.clip(n.text), "disable_web_page_preview": True}
        if self._http is None:
            self._http, self._own_http = httpx.AsyncClient(timeout=self._timeout), True
        try:
            resp = await self._http.post(url, json=body, timeout=self._timeout)
        except httpx.HTTPError as e:
            # str(e) може містити URL із токеном — лише тип винятку
            log.warning("telegram send failed: %s", type(e).__name__)
            return self._result(SendStatus.FAILED, n.key, error=type(e).__name__)
        if resp.status_code == 429:
            retry_after = _retry_after(resp)
            self._blocked_until = self._now() + retry_after
            log.warning("telegram rate limit, retry after %.0f s", retry_after)
            return self._result(SendStatus.RATE_LIMITED, n.key, http_status=429, error="telegram 429")
        if resp.status_code != 200 or not _ok(resp):
            log.warning("telegram send rejected: HTTP %s", resp.status_code)
            return self._result(
                SendStatus.FAILED, n.key, http_status=resp.status_code, error=f"HTTP {resp.status_code}"
            )
        self.dedup.mark(n.key)
        return self._result(SendStatus.SENT, n.key, http_status=200)

    # ------------------------------------------------------------------ події домену

    async def signal(
        self,
        *,
        symbol: str,
        side: int,
        u_final: float,
        top_rule: str | None = None,
        alpha: float | None = None,
        consequent: str | None = None,
        qty: Decimal | None = None,
        price: Decimal | None = None,
        binding_constraint: str | None = None,
    ) -> SendResult:
        text = templates.signal_text(
            symbol=symbol,
            side=side,
            u_final=u_final,
            top_rule=top_rule,
            alpha=alpha,
            consequent=consequent,
            qty=qty,
            price=price,
            binding_constraint=binding_constraint,
        )
        return await self.send(Notification(NotifyKind.SIGNAL, f"signal:{symbol}:{side}", text))

    async def risk_event(
        self,
        *,
        rule: str,
        verdict: str,
        symbol: str | None = None,
        observed: Any = None,
        limit: Any = None,
        factor: Any = None,
    ) -> SendResult:
        code = str(getattr(verdict, "value", verdict))
        text = templates.risk_event_text(
            rule=rule, verdict=code, symbol=symbol, observed=observed, limit=limit, factor=factor
        )
        return await self.send(Notification(NotifyKind.RISK_EVENT, f"risk:{rule}:{code}:{symbol}", text))

    async def risk_state(
        self, *, state_from: str, state_to: str, drawdown: float | None = None, reason: str | None = None
    ) -> SendResult:
        text = templates.risk_state_text(
            state_from=state_from, state_to=state_to, drawdown=drawdown, reason=reason
        )
        prio = Priority.CRITICAL if str(state_to) == "HALTED" else Priority.NORMAL
        return await self.send(
            Notification(NotifyKind.RISK_STATE, f"state:{state_from}:{state_to}", text, prio)
        )

    async def killswitch(
        self, *, action: str, reason: str | None = None, actor: str | None = None, run_id: str | None = None
    ) -> SendResult:
        text = templates.killswitch_text(action=action, reason=reason, actor=actor, run_id=run_id)
        return await self.send(
            Notification(NotifyKind.KILLSWITCH, f"ks:{action}:{run_id}", text, Priority.CRITICAL)
        )

    async def ws_disconnect(
        self, *, conn: str, cls: str, detail: str | None = None, reconnect_in_s: float | None = None
    ) -> SendResult:
        text = templates.ws_disconnect_text(conn=conn, cls=cls, detail=detail, reconnect_in_s=reconnect_in_s)
        return await self.send(Notification(NotifyKind.WS_DISCONNECT, f"ws:{conn}:{cls}", text))

    async def daily_report(
        self,
        *,
        day: str,
        candles: Mapping[str, int],
        q_min: Mapping[str, float | None],
        gaps: Mapping[str, int],
        vetoes: int,
        transitions: Sequence[str],
        equity: Decimal | None = None,
        drawdown: float | None = None,
    ) -> SendResult:
        text = templates.daily_report_text(
            day=day,
            candles=candles,
            q_min=q_min,
            gaps=gaps,
            vetoes=vetoes,
            transitions=transitions,
            equity=equity,
            drawdown=drawdown,
        )
        return await self.send(Notification(NotifyKind.REPORT, f"report:{day}", text))

    async def aclose(self) -> None:
        if self._http is not None and self._own_http:
            await self._http.aclose()
            self._http, self._own_http = None, False


def _ok(resp: httpx.Response) -> bool:
    try:
        return bool(resp.json().get("ok"))
    except ValueError:
        return False


def _retry_after(resp: httpx.Response) -> float:
    try:
        value = resp.json().get("parameters", {}).get("retry_after")
        return max(1.0, float(value))
    except (ValueError, TypeError, AttributeError):
        return 30.0
