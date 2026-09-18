"""Репозиторій свічок: ідемпотентний upsert, діапазонні запити з пагінацією, масиви для бектесту.

Найменування: storage/repositories/candle.py
Призначення: ядро даних (таблиця candle). Правило upsert (брифінг §6):

    INSERT ... ON CONFLICT (instrument_id, tf, open_time) DO UPDATE SET ...
    WHERE candle.is_closed = FALSE AND EXCLUDED.src <= candle.src

тобто закриту свічку не переписує ніщо, а відкриту — лише джерело не нижчого пріоритету
(Src: WS=1 < REST=2 < REPLAY=3, менше — пріоритетніше). Повтор тієї самої пачки нічого не змінює.
Автор: Андрій Жук, 2026.

Великі пачки (добір ~130 тис. рядків) ідуть через COPY у тимчасову таблицю + один INSERT ... SELECT
з тим самим ON CONFLICT; малі (живий WS) — через багаторядковий INSERT ... VALUES. Обидва шляхи
будує одна функція `_upsert_stmt`, тому семантика ідентична. Дублікати ключа в одній пачці
застосовуються послідовно (раунди `split_unique_rounds`), як при порядковій вставці.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

import numpy as np
from numpy.typing import NDArray
from sqlalchemy import BigInteger, Boolean, and_, bindparam, cast, column, func, literal_column, select, table
from sqlalchemy import text as sql_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.expression import Select

from fuzzhelm.core.digest import event_uid
from fuzzhelm.core.dto import Candle
from fuzzhelm.core.enums import Src, Stream, Venue
from fuzzhelm.core.money import quantize_price
from fuzzhelm.features.convert import Bar, to_float
from fuzzhelm.storage.models import CandleModel, InstrumentModel, table_of
from fuzzhelm.storage.repositories.common import (
    chunks,
    copy_records,
    dt_to_ns,
    from_mapping,
    ns_to_dt,
    split_unique_rounds,
    supports_copy,
    to_numeric,
    trim_decimal,
)

_T = table_of(CandleModel)
_I = table_of(InstrumentModel)

KEY_COLUMNS: tuple[str, ...] = ("instrument_id", "tf", "open_time")
# колонки, які пише upsert (ingested_at ставить DEFAULT now() / SET now())
WRITE_COLUMNS: tuple[str, ...] = (
    "instrument_id", "tf", "open_time", "close_time", "o", "h", "l", "c", "volume", "quote_volume",
    "trades_count", "vwap", "is_closed", "is_synthetic", "src", "anomaly_score",
)
_UPDATE_COLUMNS: tuple[str, ...] = tuple(
    c for c in WRITE_COLUMNS if c not in KEY_COLUMNS and c != "anomaly_score")

COPY_THRESHOLD = 500          # від скількох рядків у раунді вигідніший COPY
VALUES_CHUNK = 1_000          # 16 колонок × 1000 рядків < 32767 параметрів asyncpg
_STAGE = "fh_candle_stage"


@dataclass(frozen=True, slots=True)
class CandleRow:
    """Рядок таблиці candle; час — наносекунди UTC, гроші — Decimal масштабу 18."""

    instrument_id: int
    tf: str
    open_time_ns: int
    close_time_ns: int
    o: Decimal | None
    h: Decimal | None
    l: Decimal | None
    c: Decimal | None
    volume: Decimal | None
    quote_volume: Decimal | None
    trades_count: int | None
    vwap: Decimal | None
    is_closed: bool
    is_synthetic: bool
    src: int
    anomaly_score: Decimal | None
    ingested_at_ns: int | None

    def to_dto(self, symbol_canon: str, venue: Venue, *, tick_size: Decimal | None = None) -> Candle:
        """Відновити канонічний DTO.

        У таблиці немає event_uid/ts_event_ns/ts_ingest_ns: event_uid перераховується з природного
        ключа (контракт core), ts_event_ns := close_time_ns (як у REST-нормалізації),
        ts_ingest_ns := ingested_at. Ціни з tick_size квантуються до tick (точно, бо кратні йому),
        решта Decimal — до мінімального масштабу (NUMERIC не зберігає масштаб вхідного значення).
        """
        def px(x: Decimal | None) -> Decimal:
            if x is None:
                raise ValueError(f"candle {self.open_time_ns} has NULL price")
            return quantize_price(x, tick_size) if tick_size is not None else trim_decimal(x)

        return Candle(
            instrument=symbol_canon, venue=venue, tf=self.tf,
            open_time_ns=self.open_time_ns, close_time_ns=self.close_time_ns,
            o=px(self.o), h=px(self.h), l=px(self.l), c=px(self.c),
            volume=trim_decimal(self.volume or Decimal(0)),
            quote_volume=trim_decimal(self.quote_volume or Decimal(0)),
            trades_count=self.trades_count or 0,
            vwap=None if self.vwap is None else trim_decimal(self.vwap),
            is_closed=self.is_closed, is_synthetic=self.is_synthetic, src=Src(self.src),
            ts_event_ns=self.close_time_ns,
            ts_ingest_ns=self.ingested_at_ns if self.ingested_at_ns is not None else self.close_time_ns,
            event_uid=event_uid(venue.value, Stream.KLINES.value, symbol_canon, self.tf, self.open_time_ns),
        )


@dataclass(frozen=True, slots=True)
class UpsertResult:
    inserted: int
    updated: int
    skipped: int            # рядки пачки, які правило upsert не застосувало (закрита / нижчий пріоритет)

    @property
    def affected(self) -> int:
        return self.inserted + self.updated


@dataclass(frozen=True, slots=True)
class CandlePage:
    items: list[CandleRow]
    next_after_ns: int | None       # передати як after_ns для наступної сторінки; None — кінець


@dataclass(frozen=True, slots=True)
class CandleArrays:
    """Колонки свічок для бектесту: t_ns int64, o/h/l/c/v/qv float64, n int64 (за зростанням часу)."""

    t_ns: NDArray[np.int64]
    o: NDArray[np.float64]
    h: NDArray[np.float64]
    l: NDArray[np.float64]
    c: NDArray[np.float64]
    v: NDArray[np.float64]
    qv: NDArray[np.float64]
    n: NDArray[np.int64]

    def __len__(self) -> int:
        return int(self.t_ns.shape[0])

    def columns(self, names: Iterable[str] = ("t_ns", "o", "h", "l", "c", "v")) -> dict[str, NDArray[Any]]:
        """Словник колонок (напр. для backtest.manifest.dataset_hash)."""
        return {name: getattr(self, name) for name in names}

    def bars(self) -> list[Bar]:
        return [
            Bar(t_ns=t, o=o, h=h, l=lo, c=c, v=v, qv=qv, n=n)
            for t, o, h, lo, c, v, qv, n in zip(
                self.t_ns.tolist(), self.o.tolist(), self.h.tolist(), self.l.tolist(), self.c.tolist(),
                self.v.tolist(), self.qv.tolist(), self.n.tolist(), strict=True,
            )
        ]


def candle_record(c: Candle, instrument_id: int, anomaly_score: Decimal | None = None) -> tuple[Any, ...]:
    """DTO → кортеж у порядку WRITE_COLUMNS (для COPY і VALUES)."""
    return (
        instrument_id, c.tf, ns_to_dt(c.open_time_ns), ns_to_dt(c.close_time_ns),
        c.o, c.h, c.l, c.c, c.volume, c.quote_volume, c.trades_count, c.vwap,
        c.is_closed, c.is_synthetic, int(c.src), anomaly_score,
    )


def _upsert_stmt(source: Select[Any] | None = None, rows: Sequence[dict[str, Any]] | None = None) -> Any:
    ins = pg_insert(_T)
    ins = ins.from_select(list(WRITE_COLUMNS), source) if source is not None else ins.values(list(rows or ()))
    ex = ins.excluded
    set_: dict[str, Any] = {name: ex[name] for name in _UPDATE_COLUMNS}
    # скор аномалії ставить quality пізніше: повторний добір без скору не стирає вже порахований
    set_["anomaly_score"] = func.coalesce(ex.anomaly_score, _T.c.anomaly_score)
    set_["ingested_at"] = func.now()
    ins = ins.on_conflict_do_update(
        index_elements=list(KEY_COLUMNS),
        set_=set_,
        where=and_(_T.c.is_closed.is_(False), ex.src <= _T.c.src),
    )
    # xmax = 0 ⇔ рядок щойно вставлено (не оновлено) — стандартний прийом PostgreSQL
    return ins.returning(literal_column("(xmax = 0)", Boolean).label("inserted"))


class CandleRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    # ------------------------------------------------------------------ запис

    async def upsert(self, candles: Sequence[Candle], instrument_id: int | None = None, *,
                     use_copy: bool | None = None) -> UpsertResult:
        """Ідемпотентний upsert DTO-свічок.

        instrument_id=None → id шукається за `Candle.instrument` (symbol_canon) у таблиці instrument.
        use_copy=None → COPY для раундів ≥ COPY_THRESHOLD рядків (лише драйвер asyncpg).
        """
        if not candles:
            return UpsertResult(0, 0, 0)
        if instrument_id is None:
            ids = await self._ids_for({c.instrument for c in candles})
            records = [candle_record(c, ids[c.instrument]) for c in candles]
        else:
            records = [candle_record(c, instrument_id) for c in candles]
        return await self.upsert_records(records, use_copy=use_copy)

    async def upsert_records(self, records: Sequence[Sequence[Any]], *,
                             use_copy: bool | None = None) -> UpsertResult:
        """Upsert кортежів у порядку WRITE_COLUMNS (див. candle_record)."""
        inserted = updated = 0
        copy_ok = supports_copy(self.s)
        for rnd in split_unique_rounds(records, key=lambda r: (r[0], r[1], r[2])):
            want_copy = len(rnd) >= COPY_THRESHOLD if use_copy is None else use_copy
            if want_copy and copy_ok:
                flags = await self._upsert_via_copy(rnd)
            else:
                flags = []
                for part in chunks(rnd, VALUES_CHUNK):
                    rows = [dict(zip(WRITE_COLUMNS, r, strict=True)) for r in part]
                    res = await self.s.execute(_upsert_stmt(rows=rows))
                    flags.extend(res.scalars().all())
            ins = sum(1 for f in flags if f)
            inserted += ins
            updated += len(flags) - ins
        return UpsertResult(inserted, updated, len(records) - inserted - updated)

    async def _upsert_via_copy(self, rows: Sequence[Sequence[Any]]) -> list[bool]:
        await self.s.execute(sql_text(f"DROP TABLE IF EXISTS pg_temp.{_STAGE}"))
        await self.s.execute(sql_text(
            f"CREATE TEMP TABLE {_STAGE} (LIKE candle INCLUDING DEFAULTS) ON COMMIT DROP"))
        await copy_records(self.s, _STAGE, WRITE_COLUMNS, rows)
        stage = table(_STAGE, *[column(n) for n in WRITE_COLUMNS])
        res = await self.s.execute(_upsert_stmt(source=select(*[stage.c[n] for n in WRITE_COLUMNS])))
        flags = list(res.scalars().all())
        await self.s.execute(sql_text(f"DROP TABLE pg_temp.{_STAGE}"))
        return flags

    async def set_anomaly_scores(self, instrument_id: int, tf: str,
                                 scores: Sequence[tuple[int, Decimal | float]]) -> int:
        """Записати скор аномалії (quality/MLP) для свічок за open_time_ns; повертає к-сть оновлених."""
        if not scores:
            return 0
        stmt = (
            _T.update()
            .where(_T.c.instrument_id == bindparam("iid"), _T.c.tf == bindparam("tf_"),
                   _T.c.open_time == bindparam("ot"))
            .values(anomaly_score=bindparam("score"))
        )
        params = [{"iid": instrument_id, "tf_": tf, "ot": ns_to_dt(t), "score": to_numeric(v)}
                  for t, v in scores]
        res = await self.s.execute(stmt, params)
        return int(res.rowcount or 0)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ читання

    async def get(self, instrument_id: int, tf: str, open_time_ns: int) -> CandleRow | None:
        res = await self.s.execute(select(_T).where(
            _T.c.instrument_id == instrument_id, _T.c.tf == tf, _T.c.open_time == ns_to_dt(open_time_ns)))
        m = res.mappings().one_or_none()
        return None if m is None else from_mapping(CandleRow, m)

    def _range_query(self, instrument_id: int, tf: str, ts_from_ns: int | None, ts_to_ns: int | None, *,
                     closed_only: bool, cols: Sequence[Any]) -> Select[Any]:
        q = select(*cols).where(_T.c.instrument_id == instrument_id, _T.c.tf == tf)
        if ts_from_ns is not None:
            q = q.where(_T.c.open_time >= ns_to_dt(ts_from_ns))
        if ts_to_ns is not None:
            q = q.where(_T.c.open_time < ns_to_dt(ts_to_ns))
        if closed_only:
            q = q.where(_T.c.is_closed.is_(True))
        return q

    async def range(self, instrument_id: int, tf: str, ts_from_ns: int | None = None,
                    ts_to_ns: int | None = None, *, after_ns: int | None = None, limit: int = 1_000,
                    closed_only: bool = False, descending: bool = False) -> CandlePage:
        """Свічки з open_time ∈ [ts_from, ts_to), keyset-пагінація за open_time (індекс ix_candle_lookup).

        after_ns — курсор: open_time > after_ns (за зростанням) або < after_ns (descending=True).
        """
        if limit <= 0:
            raise ValueError("limit must be positive")
        q = self._range_query(instrument_id, tf, ts_from_ns, ts_to_ns, closed_only=closed_only, cols=[_T])
        if after_ns is not None:
            cur = ns_to_dt(after_ns)
            q = q.where(_T.c.open_time < cur if descending else _T.c.open_time > cur)
        q = q.order_by(_T.c.open_time.desc() if descending else _T.c.open_time.asc()).limit(limit + 1)
        res = await self.s.execute(q)
        items = [from_mapping(CandleRow, m) for m in res.mappings()]
        more = len(items) > limit
        items = items[:limit]
        return CandlePage(items=items, next_after_ns=items[-1].open_time_ns if more else None)

    async def iter_range(self, instrument_id: int, tf: str, ts_from_ns: int | None = None,
                         ts_to_ns: int | None = None, *, page_size: int = 5_000,
                         closed_only: bool = False) -> list[CandleRow]:
        """Усі свічки діапазону сторінками (зручно для скриптів; порядок — за зростанням часу)."""
        out: list[CandleRow] = []
        after: int | None = None
        while True:
            page = await self.range(instrument_id, tf, ts_from_ns, ts_to_ns, after_ns=after,
                                    limit=page_size, closed_only=closed_only)
            out.extend(page.items)
            if page.next_after_ns is None:
                return out
            after = page.next_after_ns

    async def load_arrays(self, instrument_id: int, tf: str, ts_from_ns: int | None = None,
                          ts_to_ns: int | None = None, *, closed_only: bool = True) -> CandleArrays:
        """Колонки numpy для бектесту.

        Час рахує СУБД точно: EXTRACT(EPOCH) повертає numeric (PG ≥ 14), ×10⁶ і ::bigint — мікросекунди.
        Decimal → float проходить через features.convert.to_float — ту саму єдину точку межі типів.
        """
        us = cast(func.extract("epoch", _T.c.open_time) * 1_000_000, BigInteger).label("t_us")
        cols = [us, _T.c.o, _T.c.h, _T.c.l, _T.c.c, _T.c.volume, _T.c.quote_volume, _T.c.trades_count]
        q = self._range_query(instrument_id, tf, ts_from_ns, ts_to_ns, closed_only=closed_only, cols=cols)
        res = await self.s.execute(q.order_by(_T.c.open_time.asc()))
        rows = res.all()
        n = len(rows)
        if n == 0:
            empty_f = np.empty(0, dtype=np.float64)
            return CandleArrays(np.empty(0, dtype=np.int64), empty_f, empty_f.copy(), empty_f.copy(),
                                empty_f.copy(), empty_f.copy(), empty_f.copy(), np.empty(0, dtype=np.int64))
        t_us, o, h, lo, c, v, qv, cnt = zip(*rows, strict=True)
        return CandleArrays(
            t_ns=np.fromiter(t_us, dtype=np.int64, count=n) * 1_000,
            o=_floats(o, n), h=_floats(h, n), l=_floats(lo, n), c=_floats(c, n),
            v=_floats(v, n), qv=_floats(qv, n),
            n=np.fromiter((0 if x is None else x for x in cnt), dtype=np.int64, count=n),
        )

    async def load_bars(self, instrument_id: int, tf: str, ts_from_ns: int | None = None,
                        ts_to_ns: int | None = None) -> list[Bar]:
        arrays = await self.load_arrays(instrument_id, tf, ts_from_ns, ts_to_ns, closed_only=True)
        return arrays.bars()

    async def count(self, instrument_id: int | None = None, tf: str | None = None) -> int:
        q = select(func.count()).select_from(_T)
        if instrument_id is not None:
            q = q.where(_T.c.instrument_id == instrument_id)
        if tf is not None:
            q = q.where(_T.c.tf == tf)
        return int((await self.s.execute(q)).scalar_one())

    async def latest_open_time_ns(self, instrument_id: int, tf: str, *,
                                  closed_only: bool = True) -> int | None:
        """Останній open_time (для продовження добору); використовує ix_candle_lookup."""
        q = select(_T.c.open_time).where(_T.c.instrument_id == instrument_id, _T.c.tf == tf)
        if closed_only:
            q = q.where(_T.c.is_closed.is_(True))
        res = await self.s.execute(q.order_by(_T.c.open_time.desc()).limit(1))
        dt = res.scalar_one_or_none()
        return None if dt is None else dt_to_ns(dt)

    async def find_gaps(self, instrument_id: int, tf: str, step_ns: int, ts_from_ns: int | None = None,
                        ts_to_ns: int | None = None) -> list[tuple[int, int, int]]:
        """Прогалини всередині наявного ряду.

        Кожна — (перший відсутній open_time_ns, наступний наявний open_time_ns, кількість пропущених).
        """
        inner = self._range_query(instrument_id, tf, ts_from_ns, ts_to_ns, closed_only=False, cols=[
            _T.c.open_time,
            func.lag(_T.c.open_time).over(order_by=_T.c.open_time).label("prev"),
        ]).subquery()
        q = (select(inner.c.prev, inner.c.open_time)
             .where(inner.c.open_time - inner.c.prev > timedelta(microseconds=step_ns // 1_000))
             .order_by(inner.c.open_time))
        out: list[tuple[int, int, int]] = []
        for prev, cur in (await self.s.execute(q)).all():
            p, cu = dt_to_ns(prev), dt_to_ns(cur)
            out.append((p + step_ns, cu, (cu - p) // step_ns - 1))
        return out

    async def _ids_for(self, symbols: set[str]) -> dict[str, int]:
        res = await self.s.execute(select(_I.c.symbol_canon, _I.c.id).where(_I.c.symbol_canon.in_(symbols)))
        ids = {sym: int(i) for sym, i in res.all()}
        missing = symbols - ids.keys()
        if missing:
            raise LookupError(f"unknown instruments {sorted(missing)}; upsert them into instrument first")
        return ids


def _floats(values: Sequence[Decimal | None], n: int) -> NDArray[np.float64]:
    nan = np.nan
    return np.fromiter((nan if x is None else to_float(x) for x in values), dtype=np.float64, count=n)


__all__ = [
    "COPY_THRESHOLD",
    "KEY_COLUMNS",
    "WRITE_COLUMNS",
    "CandleArrays",
    "CandlePage",
    "CandleRepo",
    "CandleRow",
    "UpsertResult",
    "candle_record",
]
