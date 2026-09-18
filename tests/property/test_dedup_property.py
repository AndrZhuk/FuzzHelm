"""B8 [property]: дедуплікація за event_uid ідемпотентна й не залежить від порядку надходження.

Найменування: tests/property/test_dedup_property.py
Автор: Андрій Жук, 2026.

Генератор навмисно створює КОНФЛІКТНІ версії однієї події (той самий event_uid: незакрита/закрита
свічка, WS/REST, різний час біржі й отримання, навіть різний вміст за рівних часів) і точні дублікати —
тобто саме ті ситуації, де наївне «перший переміг» залежить від перестановки.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from fuzzhelm.core.digest import canonical_json
from fuzzhelm.core.dto import Candle, MarketEvent, Trade
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.ingest.dedup import Deduplicator, DedupOutcome, dedup
from fuzzhelm.ingest.normalize import kline_uid, trade_uid

T0 = 1_789_758_540_000_000_000
MIN = 60_000_000_000
INST = "BTC-USDT-PERP"
V = Venue.BINANCE_USDM


def _candle(slot: int, closed: bool, src: Src, ev_off: int, ing_off: int, variant: int) -> Candle:
    o = Decimal("81000.00") + slot
    c = o + Decimal("0.10") * variant
    open_ns = T0 + slot * MIN
    return Candle(
        instrument=INST, venue=V, tf="1m", open_time_ns=open_ns, close_time_ns=open_ns + MIN - 1_000_000,
        o=o, h=c + 1, l=o - 1, c=c, volume=Decimal("1.000") + variant, is_closed=closed, src=src,
        ts_event_ns=open_ns + 1_000_000 * ev_off, ts_ingest_ns=open_ns + 1_000_000 * (ev_off + ing_off),
        event_uid=kline_uid(V, INST, "1m", open_ns),
    )


def _trade(agg: int, ing_off: int) -> Trade:
    t = T0 + agg * 1_000_000
    return Trade(instrument=INST, venue=V, agg_id=agg, price=Decimal("81015.40"), qty=Decimal("0.005"),
                 is_buyer_maker=bool(agg % 2), ts_event_ns=t, ts_ingest_ns=t + 1_000_000 * ing_off,
                 event_uid=trade_uid(V, INST, agg))


candles = st.builds(_candle, st.integers(0, 3), st.booleans(), st.sampled_from([Src.WS, Src.REST]),
                    st.integers(0, 2), st.integers(0, 2), st.integers(0, 1))
trades = st.builds(_trade, st.integers(0, 4), st.integers(0, 2))
events = st.lists(st.one_of(candles, trades), min_size=1, max_size=40)


def _reference_representative(evs: list[MarketEvent]) -> dict[str, MarketEvent]:
    """Незалежна специфікація вибору: найвищий пріоритет (закрита > незакрита, WS > REST, новіша біржова
    версія, раніше отримана); повні часові «нічиї» мусять мати однаковий вибір для будь-якого порядку."""
    best: dict[str, MarketEvent] = {}
    for e in evs:
        cur = best.get(e.event_uid)
        if cur is None or _rank(e) > _rank(cur):
            best[e.event_uid] = e
    return best


def _rank(e: MarketEvent) -> tuple[int, int, int, int]:
    if isinstance(e, Candle):
        return (int(e.is_closed), -int(e.src), e.ts_event_ns, -e.ts_ingest_ns)
    return (1, 0, e.ts_event_ns, -e.ts_ingest_ns)


@pytest.mark.property
@settings(max_examples=300)
@given(evs=events, data=st.data())
def test_dedup_idempotent_under_permutation(evs: list[MarketEvent], data: st.DataObject) -> None:
    perm = data.draw(st.permutations(evs))
    once = dedup(evs)
    # 1) незалежність від перестановки (включно з тим, ЯКА версія конфліктного uid перемогла)
    assert dedup(perm) == once
    # 2) ідемпотентність: повторна дедуплікація нічого не змінює
    assert dedup(once) == once
    assert dedup(once + perm) == once
    # 3) рівно один представник на кожен uid, нічого не загублено
    assert len({e.event_uid for e in once}) == len(once)
    assert {e.event_uid for e in once} == {e.event_uid for e in evs}
    # 4) представник — версія з найвищим пріоритетом за специфікацією (для розрізнюваних за часом версій)
    ref = _reference_representative(evs)
    for e in once:
        assert _rank(e) == _rank(ref[e.event_uid])
    # 5) потоковий режим: кількість NEW = кількості унікальних uid для будь-якого порядку
    d = Deduplicator()
    outcomes = [d.offer(e) for e in perm]
    assert outcomes.count(DedupOutcome.NEW) == len(once)
    assert d.values() == once


def test_dedup_prefers_closed_ws_and_keeps_first_arrival_among_equals() -> None:
    open_ws = _candle(0, False, Src.WS, 2, 0, 0)
    closed_rest = _candle(0, True, Src.REST, 1, 0, 0)
    closed_ws = _candle(0, True, Src.WS, 0, 0, 1)
    d = Deduplicator()
    assert d.offer(open_ws) is DedupOutcome.NEW
    assert d.offer(closed_rest) is DedupOutcome.REPLACED        # закрита > незакрита
    assert d.offer(closed_ws) is DedupOutcome.REPLACED          # WS > REST (як у upsert src)
    assert d.offer(open_ws) is DedupOutcome.DUPLICATE
    early, late = _trade(1, 0), _trade(1, 2)                    # та сама угода, різний час отримання
    assert d.offer(late) is DedupOutcome.NEW
    assert d.offer(early) is DedupOutcome.REPLACED              # раніше отримана копія
    assert d.offer(late) is DedupOutcome.DUPLICATE
    assert d.get(early.event_uid) == early


def test_equal_value_copies_with_different_scale_resolve_by_canonical_bytes() -> None:
    """Decimal("81015.4") == Decimal("81015.40"), але канонічний JSON (а отже хеш журналу) різний:
    переможець мусить не залежати від порядку надходження навіть тут (раніше вирішувало `==`)."""
    a = _trade(1, 0)
    b = a.model_copy(update={"price": Decimal("81015.400")})
    assert a == b and canonical_json(a) != canonical_json(b)
    assert canonical_json(dedup([a, b])) == canonical_json(dedup([b, a]))
