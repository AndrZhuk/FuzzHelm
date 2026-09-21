"""Маршрутизатор заявок: ідемпотентність за client_order_id і делегування порту ExecutionVenue.

Найменування: execution/router.py
Призначення: єдина точка, через яку рішення ядра стають заявками (PaperBroker).
Автор: Андрій Жук, 2026.

Семантика ключа ідемпотентності (як Idempotency-Key у HTTP API):
  * повтор ТІЄЇ САМОЇ заявки (той самий client_order_id і той самий вміст) → повертається
    збережена квитанція першої спроби, до біржі нічого не йде, подвійного виконання немає;
  * той самий client_order_id з ІНШИМ вмістом → REJECTED(DUPLICATE_CLIENT_ID);
  * виняток біржі (мережа/5xx) → стан заявки невідомий; якщо біржа вміє звіряння
    (`query_order`), маршрутизатор питає її стан; інакше повертає REJECTED(VENUE_ERROR) і НЕ
    запам'ятовує ключ, щоб повтор з тим самим id був можливий.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable
from uuid import UUID

from fuzzhelm.core.dto import Candle, Fill, OrderAck, OrderRequest
from fuzzhelm.core.enums import OrderStatus, RejectCode
from fuzzhelm.core.ports import Clock, ExecutionVenue


@runtime_checkable
class ReconcilableVenue(Protocol):
    """Біржа, що вміє повідомити стан заявки за client_order_id (звіряння після збою)."""

    def query_order(self, client_order_id: UUID, instrument: str) -> OrderAck | None: ...


class OrderRouter:
    """Реалізує ExecutionVenue поверх іншої ExecutionVenue."""

    def __init__(self, venue: ExecutionVenue, clock: Clock | None = None) -> None:
        self.venue = venue
        self.name = f"router:{venue.name}"
        self._clock = clock
        self._requests: dict[UUID, OrderRequest] = {}
        self._acks: dict[UUID, OrderAck] = {}
        self.venue_calls = 0

    def submit(self, req: OrderRequest) -> OrderAck:
        coid = req.client_order_id
        prev = self._requests.get(coid)
        if prev is not None:
            if prev == req:
                return self._acks[coid]
            return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED,
                            reject_code=RejectCode.DUPLICATE_CLIENT_ID, ts_ns=self._now(req))
        self.venue_calls += 1
        try:
            ack = self.venue.submit(req)
        except Exception:  # будь-який збій транспорту = невідомий стан заявки
            recovered = self._reconcile(req)
            if recovered is None:
                return OrderAck(client_order_id=coid, status=OrderStatus.REJECTED,
                                reject_code=RejectCode.VENUE_ERROR, ts_ns=self._now(req))
            ack = recovered
        self._requests[coid] = req
        self._acks[coid] = ack
        return ack

    def on_bar(self, bar: Candle) -> list[Fill]:
        return self.venue.on_bar(bar)

    def cancel_all(self, instrument: str) -> None:
        self.venue.cancel_all(instrument)

    def ack_of(self, client_order_id: UUID) -> OrderAck | None:
        return self._acks.get(client_order_id)

    def _reconcile(self, req: OrderRequest) -> OrderAck | None:
        if not isinstance(self.venue, ReconcilableVenue):
            return None
        try:
            return self.venue.query_order(req.client_order_id, req.instrument)
        except Exception:  # звіряння теж могло впасти: стан лишається невідомим
            return None

    def _now(self, req: OrderRequest) -> int:
        return self._clock.now_ns() if self._clock is not None else req.ts_created_ns
