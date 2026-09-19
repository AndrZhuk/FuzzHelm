"""Telegram-нотифікатор: no-op без токена, дедуплікація, token bucket, 429, секрет не потрапляє в журнал.

Найменування: tests/unit/test_notify.py
Автор: Андрій Жук, 2026.

HTTP лише через respx (жодного мережевого виклику), час — ін'єктований лічильник (без sleep).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from decimal import Decimal

import httpx
import pytest
import respx
from pydantic import SecretStr

from fuzzhelm.config import Settings
from fuzzhelm.notify import templates
from fuzzhelm.notify.telegram import (
    Notification,
    NotifyKind,
    Priority,
    RateBucket,
    SendStatus,
    TelegramNotifier,
    assert_telegram_url,
)

TOKEN = "123456789:AAH-test_token_value_0123456789abcdef"
CHAT = "-100200300"
SEND_URL = f"https://api.telegram.org/bot{TOKEN}/sendMessage"


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


@pytest.fixture
async def http() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient() as client:
        yield client


def make(http: httpx.AsyncClient, clock: Clock, **kw: object) -> TelegramNotifier:
    return TelegramNotifier(SecretStr(TOKEN), CHAT, http=http, now_s=clock, **kw)  # type: ignore[arg-type]


def ok() -> httpx.Response:
    return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})


async def test_notifier_is_noop_without_token_or_chat(http: httpx.AsyncClient) -> None:
    with respx.mock(assert_all_called=False) as mock:
        route = mock.post(url__regex=r"https://api\.telegram\.org/.*").mock(return_value=ok())
        for n in (
            TelegramNotifier(None, CHAT, http=http),
            TelegramNotifier(SecretStr(TOKEN), None, http=http),
            TelegramNotifier.from_settings(Settings(_env_file=None), http=http),
        ):  # type: ignore[call-arg]
            assert not n.enabled
            res = await n.signal(symbol="BTC-USDT-PERP", side=1, u_final=0.4)
            assert res.status is SendStatus.DISABLED
        assert not route.called


async def test_signal_sent_as_plain_ukrainian_text(http: httpx.AsyncClient) -> None:
    clock = Clock()
    n = make(http, clock)
    with respx.mock() as mock:
        route = mock.post(SEND_URL).mock(return_value=ok())
        res = await n.signal(
            symbol="BTC-USDT-PERP",
            side=1,
            u_final=0.4234,
            top_rule="R22",
            alpha=0.62,
            consequent="LONG",
            qty=Decimal("0.012"),
            price=Decimal("60000.1"),
            binding_constraint="ATR_RISK",
        )
    assert res.status is SendStatus.SENT and route.call_count == 1
    body = json.loads(route.calls[0].request.content)
    assert body == {
        "chat_id": CHAT,
        "disable_web_page_preview": True,
        "text": (
            "Сигнал BTC-USDT-PERP: ЛОНГ, u_final = 0.42. Головне правило R22 (α = 0.62, висновок ЛОНГ). "
            "Кількість 0.012 за ціною 60000.1. Обмежувальний чинник сайзера: ризик на ATR."
        ),
    }
    assert "parse_mode" not in body  # без розмітки — без ін'єкцій


async def test_duplicate_within_window_suppressed_then_resent(http: httpx.AsyncClient) -> None:
    clock = Clock()
    n = make(http, clock, dedup_window_s=300.0)
    with respx.mock() as mock:
        route = mock.post(SEND_URL).mock(return_value=ok())
        kw = {
            "rule": "stale_data",
            "verdict": "VETO",
            "symbol": "BTC-USDT-PERP",
            "observed": Decimal("6.5"),
            "limit": Decimal("5"),
        }
        assert (await n.risk_event(**kw)).status is SendStatus.SENT  # type: ignore[arg-type]
        clock.t = 299.0
        assert (await n.risk_event(**kw)).status is SendStatus.DUPLICATE  # type: ignore[arg-type]
        other = await n.risk_event(**{**kw, "verdict": "SHRINK", "factor": Decimal("0.5")})  # type: ignore[arg-type]
        assert other.status is SendStatus.SENT  # інший ключ
        clock.t = 300.0
        assert (await n.risk_event(**kw)).status is SendStatus.SENT  # type: ignore[arg-type]
    assert route.call_count == 3
    assert n.counters == {"SENT": 3, "DISABLED": 0, "DUPLICATE": 1, "RATE_LIMITED": 0, "FAILED": 0}


async def test_failed_send_is_not_marked_as_duplicate(http: httpx.AsyncClient) -> None:
    clock = Clock()
    n = make(http, clock)
    with respx.mock() as mock:
        route = mock.post(SEND_URL).mock(side_effect=[httpx.Response(500, json={"ok": False}), ok()])
        first = await n.ws_disconnect(conn="market", cls="network", reconnect_in_s=1.5)
        second = await n.ws_disconnect(conn="market", cls="network", reconnect_in_s=1.5)
    assert first.status is SendStatus.FAILED and first.http_status == 500
    assert second.status is SendStatus.SENT and route.call_count == 2


async def test_malformed_telegram_reply_is_failed_not_raised(http: httpx.AsyncClient) -> None:
    """send() за контрактом не кидає: 200 з тілом-не-об'єктом (список, не JSON) — це FAILED, а не виняток."""
    clock = Clock()
    n = make(http, clock)
    with respx.mock() as mock:
        replies = [httpx.Response(200, json=[1, 2]), httpx.Response(200, text="<html>")]
        mock.post(SEND_URL).mock(side_effect=replies)
        first = await n.ws_disconnect(conn="market", cls="network")
        second = await n.ws_disconnect(conn="market", cls="network")
    assert first.status is SendStatus.FAILED and second.status is SendStatus.FAILED
    assert n.counters["FAILED"] == 2 and n.counters["SENT"] == 0


async def test_token_bucket_drops_excess_but_not_critical(http: httpx.AsyncClient) -> None:
    clock = Clock()
    n = make(http, clock, rate_capacity=2.0, rate_per_s=1.0 / 3.0)
    with respx.mock() as mock:
        route = mock.post(SEND_URL).mock(return_value=ok())
        statuses = [
            (await n.send(Notification(NotifyKind.SIGNAL, f"k{i}", f"повідомлення {i}"))).status
            for i in range(4)
        ]
        assert statuses == [
            SendStatus.SENT,
            SendStatus.SENT,
            SendStatus.RATE_LIMITED,
            SendStatus.RATE_LIMITED,
        ]
        crit = await n.killswitch(action="tripped", reason="DD ≥ 12%", run_id="r1")
        assert crit.status is SendStatus.SENT  # kill-switch не губиться
        clock.t = 3.0  # +1 токен
        assert (await n.send(Notification(NotifyKind.SIGNAL, "k9", "ще"))).status is SendStatus.SENT
    assert route.call_count == 4


async def test_telegram_429_blocks_until_retry_after(http: httpx.AsyncClient) -> None:
    clock = Clock()
    n = make(http, clock)
    with respx.mock() as mock:
        route = mock.post(SEND_URL).mock(
            side_effect=[
                httpx.Response(429, json={"ok": False, "error_code": 429, "parameters": {"retry_after": 17}}),
                ok(),
            ]
        )
        first = await n.risk_state(state_from="WARNING", state_to="COOLDOWN", drawdown=0.081)
        clock.t = 16.9
        blocked = await n.risk_state(state_from="NORMAL", state_to="WARNING")
        clock.t = 17.0
        after = await n.risk_state(state_from="NORMAL", state_to="WARNING")
    assert first.status is SendStatus.RATE_LIMITED and first.http_status == 429
    assert blocked.status is SendStatus.RATE_LIMITED and route.call_count == 2
    assert after.status is SendStatus.SENT


async def test_token_never_logged_or_in_errors(
    http: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    clock = Clock()
    n = make(http, clock)
    with respx.mock() as mock:
        mock.post(SEND_URL).mock(side_effect=[ok(), httpx.ConnectError(f"cannot connect to {SEND_URL}")])
        sent = await n.killswitch(action="released", actor="admin", reason="ok")
        failed = await n.killswitch(action="tripped", reason="flash crash")
    assert sent.status is SendStatus.SENT and failed.status is SendStatus.FAILED
    assert failed.error == "ConnectError"  # без тексту з URL
    assert "HTTP Request: POST https://api.telegram.org/bot***/sendMessage" in caplog.text  # httpx пише URL…
    assert TOKEN not in caplog.text  # …але вже без токена
    assert TOKEN not in repr(n) and TOKEN not in str(sent) + str(failed)


def test_base_url_must_be_telegram_https_host() -> None:
    assert assert_telegram_url("https://api.telegram.org/") == "https://api.telegram.org"
    for url in ("http://api.telegram.org", "https://api.telegram.org.evil.example", "https://example.com"):
        with pytest.raises(ValueError):
            assert_telegram_url(url)
    with pytest.raises(ValueError):
        TelegramNotifier(SecretStr(TOKEN), CHAT, base_url="https://example.com")


def test_rate_bucket_refills_linearly_and_caps_at_capacity() -> None:
    clock = Clock()
    b = RateBucket(3.0, 0.5, clock)
    assert [b.try_take() for _ in range(4)] == [True, True, True, False]
    clock.t = 2.0
    assert b.tokens == pytest.approx(1.0) and b.try_take() and not b.try_take()
    clock.t = 100.0
    assert b.tokens == pytest.approx(3.0)
    with pytest.raises(ValueError):
        RateBucket(0.0, 1.0, clock)


def test_templates_are_ukrainian_and_bounded() -> None:
    text = templates.risk_event_text(
        rule="max_gross_leverage",
        verdict="SHRINK",
        symbol="ETH-USDT-PERP",
        observed=Decimal("3.25"),
        limit=Decimal("3"),
        factor=Decimal("0.92"),
    )
    assert text == (
        "Ризик-правило max_gross_leverage (ETH-USDT-PERP): зменшено (SHRINK), множник ×0.92; "
        "спостережено 3.25, ліміт 3."
    )
    assert templates.risk_state_text(state_from="COOLDOWN", state_to="HALTED", drawdown=0.1234) == (
        "Режим ризику: ОХОЛОДЖЕННЯ → ЗУПИНЕНО, просадка 12.34%."
    )
    assert templates.killswitch_text(action="tripped").startswith("УВАГА: спрацював kill-switch")
    assert "мовчання (heartbeat)" in templates.ws_disconnect_text(conn="public", cls="heartbeat")
    report = templates.daily_report_text(
        day="2026-09-18",
        candles={"BTC-USDT-PERP": 1440},
        q_min={"BTC-USDT-PERP": 0.9731},
        gaps={},
        vetoes=3,
        transitions=["10:15 NORMAL→WARNING"],
        equity=Decimal("10012.5"),
        drawdown=0.004,
    )
    assert "BTC-USDT-PERP: свічок 1440, мінімальний Q 0.9731." in report and "не є інвестиційною" in report
    assert len(templates.clip("я" * 10_000)) == templates.MAX_MESSAGE_CHARS
    assert Priority.CRITICAL > Priority.NORMAL
