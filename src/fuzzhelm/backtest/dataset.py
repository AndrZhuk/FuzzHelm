"""Набір даних бектесту: свічки як numpy-масиви в пам'яті + специфікація інструмента + ряд фандингу.

Найменування: backtest/dataset.py
Призначення: єдиний вхід рушія бектесту і воркерів grid/walk-forward (чисті numpy-масиви, pickle без
    БД і без event loop); послідовна подача барів через LookaheadGuard (бар t бачить лише індекси ≤ t).
Автор: Андрій Жук, 2026.

Колонки (однаковий порядок і dtype з storage.CandleArrays): t_ns (open_time, int64), o, h, l, c, v, qv
(float64), n (int64). Ціни в біржі кратні tick_size, тому float64 → Decimal через
sizing.convert.price_to_decimal(x, tick) точний: repr(float) — найкоротший рядок, що читається назад у той
самий double, а квантування HALF_EVEN до tick повертає саме ту десяткову ціну, з якої float був отриманий.
Обсяг — через float_to_decimal_exact (repr), він tick-у не має.

dataset_hash = backtest.manifest.dataset_hash над (t_ns, o, h, l, c, v) — тими самими колонками, що й
CandleArrays.columns() зі сховища, тож хеш набору з БД і з масивів збігається; якщо є ряд фандингу, до хешу
додаються funding_t_ns і funding_rate (формат ingest.funding.funding_columns): інша ставка — інший датасет.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from fuzzhelm.config import FIXTURES_DIR
from fuzzhelm.core.clock import NS_PER_MIN, NS_PER_SEC
from fuzzhelm.core.dto import Candle, Instrument
from fuzzhelm.execution.paper_broker import DecBar
from fuzzhelm.features.convert import Bar, to_float
from fuzzhelm.features.window import LookaheadGuard
from fuzzhelm.sizing.convert import float_to_decimal_exact, price_to_decimal

NS_PER_MS = 1_000_000
TF_NS: dict[str, int] = {"1m": NS_PER_MIN, "5m": 5 * NS_PER_MIN, "15m": 15 * NS_PER_MIN,
                         "1h": 60 * NS_PER_MIN}
# Періодів на рік для 24/7 ринку (1m: 525 600 — як A у §5.8)
BARS_PER_YEAR: dict[str, int] = {tf: (365 * 86_400 * NS_PER_SEC) // ns for tf, ns in TF_NS.items()}

FLOAT_COLUMNS: tuple[str, ...] = ("o", "h", "l", "c", "v", "qv")
INT_COLUMNS: tuple[str, ...] = ("t_ns", "n")
HASH_COLUMNS: tuple[str, ...] = ("t_ns", "o", "h", "l", "c", "v")   # = storage CandleArrays.columns()

DEFAULT_KLINES = FIXTURES_DIR / "rest" / "binance_klines.json.gz"
DEFAULT_EXCHANGE_INFO = FIXTURES_DIR / "rest" / "exchange_info.json"

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]


def _as_int(a: Any, name: str) -> IntArray:
    arr = np.ascontiguousarray(np.asarray(a), dtype=np.int64)
    if arr.ndim != 1:
        raise ValueError(f"column {name!r} must be 1-D")
    return arr


def _as_float(a: Any, name: str) -> FloatArray:
    arr = np.ascontiguousarray(np.asarray(a), dtype=np.float64)
    if arr.ndim != 1:
        raise ValueError(f"column {name!r} must be 1-D")
    if not np.isfinite(arr).all():
        raise ValueError(f"column {name!r} contains NaN/inf")
    return arr


@dataclass(frozen=True, eq=False)
class Dataset:
    """Незмінний набір закритих барів інструмента (стовпчики однакової довжини, t_ns строго зростає)."""

    instrument: Instrument
    tf: str
    t_ns: IntArray
    o: FloatArray
    h: FloatArray
    l: FloatArray
    c: FloatArray
    v: FloatArray
    qv: FloatArray
    n: IntArray
    funding_t_ns: IntArray | None = None
    funding_rate: FloatArray | None = None
    source: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if self.tf not in TF_NS:
            raise ValueError(f"unsupported timeframe {self.tf!r}; expected one of {sorted(TF_NS)}")
        size = self.t_ns.shape[0]
        for name in (*FLOAT_COLUMNS, *INT_COLUMNS):
            if getattr(self, name).shape != (size,):
                raise ValueError(f"column {name!r} has shape {getattr(self, name).shape}, expected ({size},)")
        if size and not (np.diff(self.t_ns) > 0).all():
            raise ValueError("t_ns must be strictly increasing (closed bars in time order)")
        if size and not ((self.o > 0).all() and (self.l > 0).all()):
            raise ValueError("prices must be > 0")
        hi_ok = (self.h >= np.maximum(self.o, self.c)).all() and (self.l <= np.minimum(self.o, self.c)).all()
        if size and not hi_ok:
            raise ValueError("inconsistent OHLC (need l <= min(o,c) <= max(o,c) <= h)")
        if size and ((self.v < 0).any() or (self.qv < 0).any() or (self.n < 0).any()):
            raise ValueError("volumes and trade counts must be >= 0")
        if (self.funding_t_ns is None) != (self.funding_rate is None):
            raise ValueError("funding_t_ns and funding_rate go together")
        if self.funding_t_ns is not None and self.funding_rate is not None:
            if self.funding_t_ns.shape != self.funding_rate.shape:
                raise ValueError("funding_t_ns and funding_rate lengths differ")
            if self.funding_t_ns.size and not (np.diff(self.funding_t_ns) > 0).all():
                raise ValueError("funding_t_ns must be strictly increasing")

    # ------------------------------------------------------------------ побудова

    @classmethod
    def from_arrays(cls, instrument: Instrument, *, t_ns: Any, o: Any, h: Any, l: Any, c: Any,
                    v: Any, qv: Any = None, n: Any = None, tf: str = "1m",
                    funding_t_ns: Any = None, funding_rate: Any = None, source: str = "arrays") -> Dataset:
        """Зі стовпчиків (напр. поля storage.CandleArrays); qv/n за відсутності — нулі."""
        t = _as_int(t_ns, "t_ns")
        size = t.shape[0]
        return cls(
            instrument=instrument, tf=tf, t_ns=t,
            o=_as_float(o, "o"), h=_as_float(h, "h"), l=_as_float(l, "l"), c=_as_float(c, "c"),
            v=_as_float(v, "v"),
            qv=_as_float(np.zeros(size) if qv is None else qv, "qv"),
            n=_as_int(np.zeros(size, dtype=np.int64) if n is None else n, "n"),
            funding_t_ns=None if funding_t_ns is None else _as_int(funding_t_ns, "funding_t_ns"),
            funding_rate=None if funding_rate is None else _as_float(funding_rate, "funding_rate"),
            source=source,
        )

    @classmethod
    def from_candles(cls, candles: Sequence[Candle], instrument: Instrument, *,
                     funding_t_ns: Any = None, funding_rate: Any = None) -> Dataset:
        """З DTO-свічок (Decimal → float лише через features.convert.to_float); лише закриті, один tf."""
        if not candles:
            raise ValueError("no candles")
        tf = candles[0].tf
        for cd in candles:
            if not cd.is_closed:
                raise ValueError(f"candle {cd.open_time_ns} is not closed")
            if cd.tf != tf or cd.instrument != instrument.symbol_canon:
                raise ValueError("candles must share instrument and timeframe")
        return cls.from_arrays(
            instrument, tf=tf,
            t_ns=[cd.open_time_ns for cd in candles],
            o=[to_float(cd.o) for cd in candles], h=[to_float(cd.h) for cd in candles],
            l=[to_float(cd.l) for cd in candles], c=[to_float(cd.c) for cd in candles],
            v=[to_float(cd.volume) for cd in candles], qv=[to_float(cd.quote_volume) for cd in candles],
            n=[cd.trades_count for cd in candles],
            funding_t_ns=funding_t_ns, funding_rate=funding_rate, source="candles",
        )

    # ------------------------------------------------------------------ властивості

    def __len__(self) -> int:
        return int(self.t_ns.shape[0])

    @property
    def tf_ns(self) -> int:
        return TF_NS[self.tf]

    @property
    def bars_per_year(self) -> int:
        return BARS_PER_YEAR[self.tf]

    def close_time_ns(self, i: int) -> int:
        """Час закриття бару за конвенцією Binance: open + tf − 1 мс (так само в Candle.close_time_ns)."""
        return self.t_ns[i].item() + self.tf_ns - NS_PER_MS

    def columns(self) -> dict[str, npt.NDArray[Any]]:
        """Колонки для backtest.manifest.dataset_hash (свічки + фандинг, якщо є)."""
        cols: dict[str, npt.NDArray[Any]] = {k: getattr(self, k) for k in HASH_COLUMNS}
        if self.funding_t_ns is not None and self.funding_rate is not None:
            cols["funding_t_ns"] = self.funding_t_ns
            cols["funding_rate"] = self.funding_rate
        return cols

    @cached_property
    def dataset_hash(self) -> str:
        from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415 — manifest тягне git/subprocess

        return dataset_hash(self.columns())

    # ------------------------------------------------------------------ доступ до барів

    def bar(self, i: int) -> Bar:
        """Float-бар для конвеєра ознак (той самий, що features.convert.bar_from_candle для цієї свічки)."""
        return Bar(t_ns=self.t_ns[i].item(), o=self.o[i].item(), h=self.h[i].item(), l=self.l[i].item(),
                   c=self.c[i].item(), v=self.v[i].item(), qv=self.qv[i].item(), n=self.n[i].item())

    def dec_bar(self, i: int) -> DecBar:
        return self.dec_bars[i]

    @cached_property
    def dec_bars(self) -> list[DecBar]:
        """Decimal-бари для брокера (один раз на набір; кеш процесу за dataset_hash — клітинки сітки у
        воркері діляться ними). Порядок і вміст не залежать від того, хто перший їх побудував."""
        key = self.dataset_hash
        bars = _DEC_CACHE.get(key)
        if bars is None:
            bars = [dec_bar_of(self.bar(i), self.instrument) for i in range(len(self))]
            if len(_DEC_CACHE) >= _DEC_CACHE_SIZE:
                _DEC_CACHE.pop(next(iter(_DEC_CACHE)))
            _DEC_CACHE[key] = bars
        return bars

    def slice(self, start: int, stop: int) -> Dataset:
        """Бари [start, stop) (копії масивів — pickle задачі не тягне весь набір). Фандинг — за вікном часу
        [t_start − 1 доба, t_end + 1 хв]: цього досить для будь-якого моменту 00/08/16 усередині вікна."""
        size = len(self)
        if not 0 <= start < stop <= size:
            raise ValueError(f"slice [{start}, {stop}) outside [0, {size})")
        ft, fr = self.funding_t_ns, self.funding_rate
        if ft is not None and fr is not None:
            lo = self.t_ns[start].item() - 86_400 * NS_PER_SEC
            hi = self.t_ns[stop - 1].item() + NS_PER_MIN
            m = (ft >= lo) & (ft <= hi)
            ft, fr = ft[m].copy(), fr[m].copy()
        return Dataset(
            instrument=self.instrument, tf=self.tf,
            t_ns=self.t_ns[start:stop].copy(), o=self.o[start:stop].copy(), h=self.h[start:stop].copy(),
            l=self.l[start:stop].copy(), c=self.c[start:stop].copy(), v=self.v[start:stop].copy(),
            qv=self.qv[start:stop].copy(), n=self.n[start:stop].copy(),
            funding_t_ns=ft, funding_rate=fr, source=f"{self.source}[{start}:{stop}]",
        )

    def feed(self) -> BarFeed:
        return BarFeed(self)

    # ------------------------------------------------------------------ pickle-вантаж для воркерів

    def to_payload(self) -> dict[str, Any]:
        """Плаский словник (numpy-масиви + dict інструмента) для ProcessPoolExecutor."""
        out: dict[str, Any] = {k: getattr(self, k) for k in (*INT_COLUMNS, *FLOAT_COLUMNS)}
        out["tf"] = self.tf
        out["instrument"] = self.instrument.model_dump(mode="json")
        out["funding_t_ns"] = self.funding_t_ns
        out["funding_rate"] = self.funding_rate
        out["source"] = self.source
        return out

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> Dataset:
        inst = Instrument.model_validate(payload["instrument"])
        return cls.from_arrays(
            inst, tf=str(payload.get("tf", "1m")), t_ns=payload["t_ns"], o=payload["o"], h=payload["h"],
            l=payload["l"], c=payload["c"], v=payload["v"], qv=payload.get("qv"), n=payload.get("n"),
            funding_t_ns=payload.get("funding_t_ns"), funding_rate=payload.get("funding_rate"),
            source=str(payload.get("source", "payload")),
        )


def dec_bar_of(bar: Bar, instrument: Instrument) -> DecBar:
    """Decimal-бар для брокера з float-бару: ціни — price_to_decimal (точно для tick-кратних цін)."""
    tick = instrument.tick_size
    return DecBar(instrument.symbol_canon, bar.t_ns,
                  price_to_decimal(bar.o, tick), price_to_decimal(bar.h, tick),
                  price_to_decimal(bar.l, tick), price_to_decimal(bar.c, tick),
                  float_to_decimal_exact(bar.v))


_DEC_CACHE: dict[str, list[DecBar]] = {}
_DEC_CACHE_SIZE = 4


class BarFeed:
    """Послідовна подача барів через LookaheadGuard: на кроці t читаються лише індекси ≤ t.

    Рушій (TradingLoop) отримує бари по одному і сам масивів не бачить; охоронець тут робить те саме
    правило явним для драйвера: будь-яке читання індексу > курсора → LookaheadError.
    """

    __slots__ = ("_ds", "_f", "_i", "_tn")

    def __init__(self, ds: Dataset) -> None:
        self._ds = ds
        self._f = LookaheadGuard(np.column_stack([getattr(ds, k) for k in FLOAT_COLUMNS]), cursor=-1)
        self._i = LookaheadGuard(np.column_stack([ds.t_ns, ds.n]), cursor=-1)
        self._tn = ds.tf_ns - NS_PER_MS

    @property
    def cursor(self) -> int:
        return self._f.cursor

    def history(self, column: str) -> FloatArray:
        """Уся видима історія колонки (індекси ≤ курсора), лише для читання."""
        j = FLOAT_COLUMNS.index(column)
        return self._f.visible()[:, j]

    def peek(self, i: int) -> Any:
        """Прямий доступ до рядка i (для перевірки охоронця: i > курсора → LookaheadError)."""
        return self._f[i]

    def __len__(self) -> int:
        return len(self._ds)

    def __iter__(self) -> Iterator[tuple[Bar, DecBar, int]]:
        """(float-бар, Decimal-бар, close_time_ns) по черзі; курсор охоронця — на поточному барі."""
        dec_bars = self._ds.dec_bars
        for i in range(len(self._ds)):
            self._f.advance()
            self._i.advance()
            o, h, lo, c, v, qv = self._f[i].tolist()
            t, n = self._i[i].tolist()
            yield Bar(t_ns=t, o=o, h=h, l=lo, c=c, v=v, qv=qv, n=n), dec_bars[i], t + self._tn


# ---------------------------------------------------------------- завантаження фікстур


def load_klines_json(path: str | Path, instrument: Instrument, *, tf: str = "1m",
                     funding_t_ns: Any = None, funding_rate: Any = None) -> Dataset:
    """Сирі рядки Binance `/fapi/v1/klines` (JSON, опційно .gz) → Dataset.

    Рядки біржі — десяткові рядки; float64 з них — коректно округлений double (той самий, що
    to_float(Decimal(рядок))), тож дані ідентичні шляху «REST → Candle → Dataset.from_candles».
    """
    p = Path(path)
    raw = gzip.decompress(p.read_bytes()) if p.suffix == ".gz" else p.read_bytes()
    rows = json.loads(raw)
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{p}: expected a non-empty JSON array of kline rows")
    step_ms = TF_NS[tf] // NS_PER_MS
    for r in rows:
        if r[6] != r[0] + step_ms - 1:
            raise ValueError(f"{p}: row {r[0]} is not a {tf} kline (closeTime mismatch)")
    return Dataset.from_arrays(
        instrument, tf=tf,
        t_ns=np.array([r[0] for r in rows], dtype=np.int64) * NS_PER_MS,
        o=_col(rows, 1), h=_col(rows, 2), l=_col(rows, 3), c=_col(rows, 4), v=_col(rows, 5), qv=_col(rows, 7),
        n=np.array([r[8] for r in rows], dtype=np.int64),
        funding_t_ns=funding_t_ns, funding_rate=funding_rate, source=p.name,
    )


def _col(rows: Sequence[Sequence[Any]], j: int) -> FloatArray:
    # numpy розбирає десяткові рядки біржі в коректно округлений double (як to_float(Decimal(рядок)))
    return np.array([r[j] for r in rows], dtype=np.float64)


def load_exchange_instrument(symbol: str = "BTCUSDT", path: str | Path = DEFAULT_EXCHANGE_INFO) -> Instrument:
    """Специфікація інструмента з записаного `/fapi/v1/exchangeInfo` (tick/step/minNotional) тим самим
    нормалізатором, що й живий REST-інжест."""
    from fuzzhelm.ingest.normalize import normalize_exchange_info  # noqa: PLC0415 — лише для фікстур

    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return normalize_exchange_info(data, [symbol])[symbol]


def load_fixture_dataset(symbol: str = "BTCUSDT", path: str | Path = DEFAULT_KLINES) -> Dataset:
    """3000 реальних 1m-барів BTCUSDT з fixtures/rest (швидкі тести і замір швидкодії)."""
    return load_klines_json(path, load_exchange_instrument(symbol))


def decimal_ohlc(ds: Dataset, i: int) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """(o, h, l, c) бару i як Decimal (для тестів і звітів)."""
    b = ds.dec_bar(i)
    return b.o, b.h, b.l, b.c
