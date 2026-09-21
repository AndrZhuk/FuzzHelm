"""M1–M3. Свічки: ідемпотентний upsert, закрита свічка незмінна, CHECK-обмеження DDL.

Найменування: tests/integration/test_candles.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import time
from decimal import Decimal
from typing import Any

import numpy as np
import pytest
from sqlalchemy import insert, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from tests.integration._data import BTC, SYMBOL, T0_NS, add_instrument, candle

from fuzzhelm.backtest.manifest import dataset_hash
from fuzzhelm.core.clock import NS_PER_MIN
from fuzzhelm.core.enums import Src, Venue
from fuzzhelm.features.convert import to_float
from fuzzhelm.storage.models import CandleModel
from fuzzhelm.storage.repositories.candle import CandleRepo, UpsertResult
from fuzzhelm.storage.repositories.common import (
    SQLSTATE_CHECK_VIOLATION,
    constraint_name,
    ns_to_dt,
    sqlstate,
)
from fuzzhelm.storage.session import session_scope

pytestmark = pytest.mark.integration

_C = CandleModel.__table__
_STATE_COLS = [c for c in _C.c if c.name != "ingested_at"]


async def _snapshot(s: AsyncSession) -> list[tuple[Any, ...]]:
    res = await s.execute(select(*_STATE_COLS).order_by(_C.c.instrument_id, _C.c.tf, _C.c.open_time))
    return [tuple(r) for r in res.all()]


@pytest.mark.parametrize("use_copy", [True, False], ids=["copy", "values"])
async def test_candle_upsert_idempotent(factory: async_sessionmaker[AsyncSession], use_copy: bool) -> None:
    closed = [candle(i) for i in range(1_200)]
    still_open = [candle(1_200 + i, src=Src.WS, closed=False) for i in range(50)]
    batch = closed + still_open

    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        first = await CandleRepo(s).upsert(batch, use_copy=use_copy)  # instrument_id з symbol_canon
    assert first == UpsertResult(inserted=1_250, updated=0, skipped=0)
    async with session_scope(factory) as s:
        snap1 = await _snapshot(s)
        rows = (await CandleRepo(s).range(iid, "1m", limit=2_000)).items

    # значення в БД — ті самі DTO (NUMERIC(38,18) числово тотожний Decimal-у, час кратний 1 мс — точно)
    assert len(rows) == 1_250
    for row, c in zip(rows, batch, strict=True):
        assert row.to_dto(SYMBOL, Venue.BINANCE_USDM, tick_size=BTC.tick_size) == c.model_copy(
            update={"ts_ingest_ns": row.ingested_at_ns}
        )

    async with session_scope(factory) as s:
        second = await CandleRepo(s).upsert(batch, use_copy=use_copy)
    # закриті 1200 правило upsert не чіпає зовсім, 50 відкритих перезаписуються тими самими значеннями
    assert second == UpsertResult(inserted=0, updated=50, skipped=1_200)
    async with session_scope(factory) as s:
        assert await _snapshot(s) == snap1
        # і тим самим шляхом, і іншим — стан той самий (обидва шляхи будує один _upsert_stmt)
        third = await CandleRepo(s).upsert(batch, use_copy=not use_copy)
        assert third == second
        assert await _snapshot(s) == snap1
        assert await CandleRepo(s).count(iid, "1m") == 1_250


async def test_upsert_does_not_overwrite_closed_candle(factory: async_sessionmaker[AsyncSession]) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        repo = CandleRepo(s)
        base = candle(0, src=Src.REST, closed=True)
        assert (await repo.upsert([base])).inserted == 1
        before = await repo.get(iid, "1m", base.open_time_ns)
        assert before is not None

        # жодне джерело, навіть пріоритетніший WS, не змінює закриту свічку
        other = Decimal("60005.00")
        for src in (Src.WS, Src.REST, Src.REPLAY):
            for closed in (True, False):
                res = await repo.upsert([candle(0, src=src, closed=closed, close_px=other)])
                assert res == UpsertResult(inserted=0, updated=0, skipped=1), (src, closed)
        after = await repo.get(iid, "1m", base.open_time_ns)
        assert after == before  # включно з ingested_at: UPDATE не виконувався взагалі

        # контроль: відкриту свічку оновлює лише джерело не нижчого пріоритету (EXCLUDED.src <= candle.src)
        t1 = candle(1).open_time_ns
        assert (await repo.upsert([candle(1, src=Src.REST, closed=False)])).inserted == 1
        assert (await repo.upsert([candle(1, src=Src.REPLAY, closed=False, close_px=other)])).skipped == 1
        assert (
            await repo.upsert([candle(1, src=Src.WS, closed=False, close_px=Decimal("60001.00"))])
        ).updated == 1
        row = await repo.get(iid, "1m", t1)
        assert row is not None and row.c == Decimal("60001.00") and row.src == Src.WS and not row.is_closed
        # тепер рядок належить WS: REST (нижчий пріоритет) його не закриє
        assert (await repo.upsert([candle(1, src=Src.REST, closed=True, close_px=other)])).skipped == 1
        assert (
            await repo.upsert([candle(1, src=Src.WS, closed=True, close_px=Decimal("60002.00"))])
        ).updated == 1
        assert (await repo.upsert([candle(1, src=Src.WS, closed=True, close_px=other)])).skipped == 1
        row = await repo.get(iid, "1m", t1)
        assert row is not None and row.c == Decimal("60002.00") and row.is_closed

        # дублікати ключа в одній пачці застосовуються послідовно (раунди), в обох шляхах запису
        for i, use_copy in ((2, False), (3, True)):
            batch = [
                candle(i, src=Src.WS, closed=False, close_px=Decimal("60010.00")),
                candle(i, src=Src.WS, closed=True, close_px=Decimal("60011.00")),
                candle(i, src=Src.WS, closed=False, close_px=Decimal("60012.00")),
            ]
            assert await repo.upsert(batch, use_copy=use_copy) == UpsertResult(1, 1, 1)
            row = await repo.get(iid, "1m", candle(i).open_time_ns)
            assert row is not None and row.c == Decimal("60011.00") and row.is_closed


def _valid_row(iid: int) -> dict[str, Any]:
    t = T0_NS
    return {
        "instrument_id": iid,
        "tf": "1m",
        "open_time": ns_to_dt(t),
        "close_time": ns_to_dt(t + NS_PER_MIN - 1),
        "o": Decimal("100"),
        "h": Decimal("102"),
        "l": Decimal("99"),
        "c": Decimal("101"),
        "volume": Decimal("1"),
        "quote_volume": Decimal("100"),
        "trades_count": 5,
        "vwap": Decimal("100.5"),
        "is_closed": True,
        "is_synthetic": False,
        "src": 2,
    }


# CHECK-и PostgreSQL перевіряє в алфавітному порядку імен, тож кожен випадок порушує рівно «свій» перший
CHECK_CASES: list[tuple[str, dict[str, Any]]] = [
    (
        "ck_hl",
        {"o": Decimal("100"), "c": Decimal("100"), "h": Decimal("100"), "l": Decimal("101"), "vwap": None},
    ),
    ("ck_h", {"c": Decimal("103")}),
    ("ck_l", {"l": Decimal("100.5")}),
    ("ck_vwap", {"vwap": Decimal("102.5")}),
    ("candle_volume_check", {"volume": Decimal("-1")}),
    ("candle_quote_volume_check", {"quote_volume": Decimal("-0.01")}),
    ("candle_trades_count_check", {"trades_count": -1}),
    ("candle_src_check", {"src": 4}),
]


@pytest.mark.parametrize(("constraint", "override"), CHECK_CASES, ids=[c for c, _ in CHECK_CASES])
async def test_check_constraint_rejects_invalid_candle(
    factory: async_sessionmaker[AsyncSession], constraint: str, override: dict[str, Any]
) -> None:
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
    async with factory() as s:
        # рядок пишеться в обхід DTO (той відхилив би його раніше): перевіряємо саме останній рубіж — СУБД
        with pytest.raises(IntegrityError) as ei:
            await s.execute(insert(_C).values(**{**_valid_row(iid), **override}))
        assert sqlstate(ei.value) == SQLSTATE_CHECK_VIOLATION
        assert constraint_name(ei.value) == constraint
        await s.rollback()
    async with session_scope(factory) as s:  # контроль: той самий рядок без порушення проходить
        await s.execute(insert(_C).values(**_valid_row(iid)))
        assert await CandleRepo(s).count(iid) == 1


async def test_ddl_checks_do_not_reject_nan_high_documented(
    factory: async_sessionmaker[AsyncSession],
) -> None:
    """Документує межу нормативного DDL (deviations ST-04): NUMERIC NaN > будь-якого числа, тож h = NaN
    проходить ck_hl/ck_h/ck_vwap. Від NaN захищає DTO (pydantic відкидає NaN у Decimal)."""
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        await s.execute(insert(_C).values(**{**_valid_row(iid), "h": Decimal("NaN")}))
        got = (await s.execute(select(_C.c.h).where(_C.c.instrument_id == iid))).scalar_one()
        assert got.is_nan()
    with pytest.raises(ValueError, match=r"finite|NaN|nan"):
        candle(0).model_validate({**candle(0).model_dump(), "h": Decimal("NaN")})


async def test_candle_range_pagination_arrays_and_gaps(factory: async_sessionmaker[AsyncSession]) -> None:
    idx = [i for i in range(300) if not 100 <= i < 105]  # дірка на 5 хвилин
    candles = [candle(i) for i in idx]
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
        await CandleRepo(s).upsert(candles, iid)
        await CandleRepo(s).upsert([candle(400, src=Src.WS, closed=False)], iid)
    async with session_scope(factory) as s:
        repo = CandleRepo(s)
        # keyset-пагінація: сторінки покривають усе рівно по разу, у порядку часу
        got: list[int] = []
        after: int | None = None
        while True:
            page = await repo.range(iid, "1m", after_ns=after, limit=64, closed_only=True)
            got.extend(r.open_time_ns for r in page.items)
            if page.next_after_ns is None:
                break
            after = page.next_after_ns
        assert got == [c.open_time_ns for c in candles]
        desc = await repo.range(iid, "1m", limit=10, descending=True)
        assert [r.open_time_ns for r in desc.items] == sorted(
            [candle(400).open_time_ns, *(c.open_time_ns for c in candles)], reverse=True
        )[:10]
        # напіввідкритий інтервал [from, to)
        window = await repo.range(iid, "1m", T0_NS + 10 * NS_PER_MIN, T0_NS + 20 * NS_PER_MIN)
        assert [r.open_time_ns for r in window.items] == [T0_NS + i * NS_PER_MIN for i in range(10, 20)]

        arrays = await repo.load_arrays(iid, "1m")  # лише закриті
        assert len(arrays) == len(candles)
        assert arrays.t_ns.dtype == np.int64 and arrays.o.dtype == np.float64
        assert arrays.t_ns.tolist() == [c.open_time_ns for c in candles]
        # та сама точка межі типів, що й у конвеєрі ознак: побітова рівність, а не наближена
        assert arrays.o.tolist() == [to_float(c.o) for c in candles]
        assert arrays.c.tolist() == [to_float(c.c) for c in candles]
        assert arrays.v.tolist() == [to_float(c.volume) for c in candles]
        assert arrays.n.tolist() == [c.trades_count for c in candles]
        again = await repo.load_arrays(iid, "1m")
        assert dataset_hash(arrays.columns()) == dataset_hash(again.columns())
        bars = await repo.load_bars(iid, "1m")
        assert bars[0].t_ns == candles[0].open_time_ns and bars[-1].c == to_float(candles[-1].c)

        assert await repo.find_gaps(iid, "1m", NS_PER_MIN) == [
            (T0_NS + 100 * NS_PER_MIN, T0_NS + 105 * NS_PER_MIN, 5),
            (T0_NS + 300 * NS_PER_MIN, T0_NS + 400 * NS_PER_MIN, 100),
        ]
        assert await repo.latest_open_time_ns(iid, "1m") == candles[-1].open_time_ns
        assert await repo.latest_open_time_ns(iid, "1m", closed_only=False) == candle(400).open_time_ns

    async with session_scope(factory) as s:
        # відкриту свічку повторний upsert оновлює
        repo = CandleRepo(s)
        await repo.upsert([candle(400, src=Src.WS, closed=False, close_px=Decimal("60003.00"))], iid)
        row = await repo.get(iid, "1m", candle(400).open_time_ns)
        assert row is not None and row.c == Decimal("60003.00")


@pytest.mark.slow
async def test_bulk_upsert_130k_rows(
    factory: async_sessionmaker[AsyncSession], capsys: pytest.CaptureFixture[str]
) -> None:
    """Масштаб добору 45 днів × 2 інструменти ≈ 130 тис. рядків: коректність і повторна ідемпотентність."""
    n = 130_000
    candles = [candle(i) for i in range(n)]
    async with session_scope(factory) as s:
        iid = await add_instrument(s)
    t0 = time.perf_counter()
    async with session_scope(factory) as s:
        first = await CandleRepo(s).upsert(candles, iid)
    t1 = time.perf_counter()
    async with session_scope(factory) as s:
        second = await CandleRepo(s).upsert(candles, iid)
    t2 = time.perf_counter()
    async with session_scope(factory) as s:
        arrays = await CandleRepo(s).load_arrays(iid, "1m")
        size = (await s.execute(text("SELECT pg_total_relation_size('candle')"))).scalar_one()
    t3 = time.perf_counter()
    assert first == UpsertResult(n, 0, 0)
    assert second == UpsertResult(0, 0, n)
    assert len(arrays) == n
    with capsys.disabled():
        print(
            f"\n[bulk 130k] upsert {t1 - t0:.2f}s, repeat {t2 - t1:.2f}s, load_arrays {t3 - t2:.2f}s, "
            f"candle relation {size / 2**20:.1f} MiB"
        )
