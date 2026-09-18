"""Дедуплікація ринкових подій за event_uid, ідемпотентна під перестановками.

Найменування: ingest/dedup.py
Призначення: одна подія — один запис, незалежно від того, скільки разів і в якому порядку її
доставили WS, REST-добір чи реплей (дублікати кадрів, повтори після реконекту, перекриття сторінок).
Автор: Андрій Жук, 2026.

Конфлікт «той самий event_uid, різний вміст» (напр. незакрита і закрита версії однієї свічки)
розв'язується ПОВНИМ порядком пріоритету, а не порядком надходження — звідси незалежність результату
від перестановки входу (property-тест test_dedup_idempotent_under_permutation):
    ключ = (закрита?, −src, ts_event_ns, −ts_ingest_ns, BLAKE2b канонічного вмісту)
закрита свічка > незакрита; WS(1) > REST(2) > REPLAY(3) (як у upsert: EXCLUDED.src <= candle.src);
новіший стан біржі > старіший; серед однакових — те, що прийшло раніше; останній ключ — лише для
абсолютної детермінованості на повністю рівних за часом копіях.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from enum import StrEnum

from fuzzhelm.core.digest import hex_digest
from fuzzhelm.core.dto import Candle, MarketEvent

PrecedenceKey = tuple[int, int, int, int]


class DedupOutcome(StrEnum):
    NEW = "NEW"               # event_uid побачено вперше
    DUPLICATE = "DUPLICATE"   # уже є версія з не нижчим пріоритетом — подію відкинуто
    REPLACED = "REPLACED"     # подія витіснила збережену версію з нижчим пріоритетом


def precedence(ev: MarketEvent) -> PrecedenceKey:
    if isinstance(ev, Candle):
        return (int(ev.is_closed), -int(ev.src), ev.ts_event_ns, -ev.ts_ingest_ns)
    return (1, 0, ev.ts_event_ns, -ev.ts_ingest_ns)


def _outranks(new: MarketEvent, old: MarketEvent) -> bool:
    kn, ko = precedence(new), precedence(old)
    if kn != ko:
        return kn > ko
    if new is old:
        return False
    # повністю рівні за часом копії: детермінований тай-брейк за КАНОНІЧНИМИ байтами (рахується рідко).
    # Не `new == old`: Decimal("81015.4") == Decimal("81015.40"), але канонічний JSON (і хеш журналу)
    # у них різний — порівняння за == лишало б переможцем першу за надходженням копію.
    return hex_digest(new) > hex_digest(old)


class Deduplicator:
    """Потоковий дедуплікатор. `max_size` обмежує пам'ять (FIFO-витіснення найстаріших uid);
    з обмеженням незалежність від перестановок гарантується лише в межах вікна."""

    def __init__(self, max_size: int | None = None) -> None:
        if max_size is not None and max_size < 1:
            raise ValueError("max_size must be >= 1")
        self._max = max_size
        self._events: OrderedDict[str, MarketEvent] = OrderedDict()
        self.stats: dict[DedupOutcome, int] = dict.fromkeys(DedupOutcome, 0)

    def offer(self, ev: MarketEvent) -> DedupOutcome:
        uid = ev.event_uid
        old = self._events.get(uid)
        if old is None:
            self._events[uid] = ev
            if self._max is not None and len(self._events) > self._max:
                self._events.popitem(last=False)
            outcome = DedupOutcome.NEW
        elif _outranks(ev, old):
            self._events[uid] = ev
            outcome = DedupOutcome.REPLACED
        else:
            outcome = DedupOutcome.DUPLICATE
        self.stats[outcome] += 1
        return outcome

    def offer_all(self, events: Iterable[MarketEvent]) -> list[MarketEvent]:
        """Пропускає всі події; повертає ті, що стали новими або витіснили стару версію (порядок входу)."""
        return [ev for ev in events if self.offer(ev) is not DedupOutcome.DUPLICATE]

    def seen(self, uid: str) -> bool:
        return uid in self._events

    def get(self, uid: str) -> MarketEvent | None:
        return self._events.get(uid)

    def __contains__(self, uid: object) -> bool:
        return uid in self._events

    def __len__(self) -> int:
        return len(self._events)

    def values(self) -> list[MarketEvent]:
        """Збережені події в канонічному порядку (ts_event_ns, event_uid) — не залежить від порядку входу."""
        return sorted(self._events.values(), key=lambda e: (e.ts_event_ns, e.event_uid))


def dedup(events: Iterable[MarketEvent]) -> list[MarketEvent]:
    """Пакетна дедуплікація: множина представників за event_uid у канонічному порядку."""
    d = Deduplicator()
    for ev in events:
        d.offer(ev)
    return d.values()
