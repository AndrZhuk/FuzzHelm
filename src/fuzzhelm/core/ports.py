"""Порти (гексагональна архітектура): через них ядро бачить час, ідентифікатори, ринок і біржу.

Найменування: core/ports.py
Призначення: межа детермінізму — усе від EventJournal до PaperBroker отримує час і випадковість
лише через ці ін'єктовані порти, тому один і той самий код рішень працює і в live, і в бектесті.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable
from uuid import UUID

from fuzzhelm.core.dto import Candle, Fill, MarketEvent, OrderAck, OrderRequest


@runtime_checkable
class Clock(Protocol):
    def now_ns(self) -> int: ...


@runtime_checkable
class IdGenerator(Protocol):
    def next_uuid(self) -> UUID: ...


@runtime_checkable
class MarketFeed(Protocol):
    """Джерело ринкових подій у порядку надходження (ReplayFeed | LiveWsFeed)."""

    def __aiter__(self) -> AsyncIterator[MarketEvent]: ...


@runtime_checkable
class ExecutionVenue(Protocol):
    """Порт виконання. Синхронний, бо бектест-воркер — чиста функція без event loop.

    PaperBroker: `submit` ставить заявку в чергу; `on_bar(bar)` виконує MARKET-заявки за bar.o
    (рішення на закритті t → виконання на відкритті t+1) і перевіряє STOP_MARKET у межах бару.
    BinanceTestnetVenue: `submit` робить підписаний POST /fapi/v1/order; `on_bar` звіряє стан.
    """

    name: str

    def submit(self, req: OrderRequest) -> OrderAck: ...

    def on_bar(self, bar: Candle) -> list[Fill]: ...

    def cancel_all(self, instrument: str) -> None: ...
