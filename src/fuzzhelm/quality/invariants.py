"""Інваріанти ринкових подій: свічка, угода, снапшот книги.

Найменування: quality/invariants.py
Призначення: друга лінія перевірки даних після нормалізації. DTO `Candle` уже відкидає неузгоджений
OHLC у конструкторі (ті самі CHECK, що й у таблиці candle), але не знає специфікації інструмента:
кратність ціни tick_size, кількості step_size, межі quote_volume. Тут — повний перелік, що
застосовується і до подій, які прийшли в обхід валідації (рядок БД, `model_construct`, стара фікстура).
Автор: Андрій Жук, 2026.

Результат — список кодів порушень (порожній = подія валідна). Код має вигляд `CODE` або
`CODE:поле` (напр. `PRICE_OFF_TICK:h`), щоб лічильник N_invalid і журнал могли агрегувати за кодом.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from itertools import pairwise
from typing import Final, Protocol

from fuzzhelm.core.dto import BookSnapshot, Instrument, Trade
from fuzzhelm.core.money import D0
from fuzzhelm.ingest.normalize import NS_PER_MS, TF_MS

# коди порушень (стабільні рядки — потрапляють у журнал і звіт)
HIGH_BELOW_LOW: Final = "HIGH_BELOW_LOW"
HIGH_BELOW_MAX_OC: Final = "HIGH_BELOW_MAX_OC"          # h < max(o, c)
LOW_ABOVE_MIN_OC: Final = "LOW_ABOVE_MIN_OC"            # l > min(o, c)
NONPOSITIVE_PRICE: Final = "NONPOSITIVE_PRICE"
PRICE_OFF_TICK: Final = "PRICE_OFF_TICK"                # ціна не кратна tick_size
QTY_OFF_STEP: Final = "QTY_OFF_STEP"                    # обсяг/кількість не кратні step_size
NEGATIVE_VOLUME: Final = "NEGATIVE_VOLUME"
NEGATIVE_QUOTE_VOLUME: Final = "NEGATIVE_QUOTE_VOLUME"
QUOTE_VOLUME_OUT_OF_RANGE: Final = "QUOTE_VOLUME_OUT_OF_RANGE"   # qv ∉ [v·l, v·h]
NEGATIVE_TRADES_COUNT: Final = "NEGATIVE_TRADES_COUNT"
VWAP_OUT_OF_RANGE: Final = "VWAP_OUT_OF_RANGE"
CLOSE_TIME_MISMATCH: Final = "CLOSE_TIME_MISMATCH"      # close_time ≠ open_time + tf − 1 мс
OPEN_TIME_UNALIGNED: Final = "OPEN_TIME_UNALIGNED"      # open_time не на сітці tf
NONPOSITIVE_QTY: Final = "NONPOSITIVE_QTY"
BOOK_CROSSED: Final = "BOOK_CROSSED"                    # best bid ≥ best ask
BOOK_UNSORTED: Final = "BOOK_UNSORTED"


class CandleLike(Protocol):
    """Будь-що зі складом свічки (Candle, рядок БД, `Candle.model_construct(...)`)."""

    @property
    def tf(self) -> str: ...
    @property
    def open_time_ns(self) -> int: ...
    @property
    def close_time_ns(self) -> int: ...
    @property
    def o(self) -> Decimal: ...
    @property
    def h(self) -> Decimal: ...
    @property
    def l(self) -> Decimal: ...  # noqa: E743 — ім'я поля DTO Candle (і колонки таблиці candle)
    @property
    def c(self) -> Decimal: ...
    @property
    def volume(self) -> Decimal: ...
    @property
    def quote_volume(self) -> Decimal: ...
    @property
    def trades_count(self) -> int: ...
    @property
    def vwap(self) -> Decimal | None: ...


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """Мінімум специфікації для перевірок (Instrument його має; тести можуть задати напряму)."""

    tick_size: Decimal
    step_size: Decimal | None = None


def _on_grid(x: Decimal, step: Decimal) -> bool:
    # точна перевірка без округлення: залишок від ділення Decimal
    return x % step == D0


def _spec(instrument: Instrument | InstrumentSpec | None) -> InstrumentSpec | None:
    if instrument is None:
        return None
    if isinstance(instrument, InstrumentSpec):
        return instrument
    return InstrumentSpec(instrument.tick_size, instrument.step_size)


def check_candle(c: CandleLike, instrument: Instrument | InstrumentSpec | None = None, *,
                 check_volume_step: bool = True) -> list[str]:
    """Усі порушені інваріанти свічки (порожній список — свічка валідна).

    Без `instrument` перевіряються лише структурні інваріанти (OHLC, знаки, час, qv-межі).
    """
    out: list[str] = []
    o, h, l, cl = c.o, c.h, c.l, c.c
    if min(o, h, l, cl) <= D0:
        out.append(NONPOSITIVE_PRICE)
    if h < l:
        out.append(HIGH_BELOW_LOW)
    if h < max(o, cl):
        out.append(HIGH_BELOW_MAX_OC)
    if l > min(o, cl):
        out.append(LOW_ABOVE_MIN_OC)
    if c.volume < D0:
        out.append(NEGATIVE_VOLUME)
    qv = c.quote_volume
    if qv < D0:
        out.append(NEGATIVE_QUOTE_VOLUME)
    # qv = Σ pᵢ·qᵢ, pᵢ ∈ [l, h] ⇒ v·l ≤ qv ≤ v·h. qv = 0 при v > 0 означає «джерело не надає» (Kraken).
    elif qv > D0 and c.volume >= D0 and not (c.volume * l <= qv <= c.volume * h):
        out.append(QUOTE_VOLUME_OUT_OF_RANGE)
    if c.trades_count < 0:
        out.append(NEGATIVE_TRADES_COUNT)
    if c.vwap is not None and not (l <= c.vwap <= h):
        out.append(VWAP_OUT_OF_RANGE)
    step_ms = TF_MS.get(c.tf)
    if step_ms is not None:
        step_ns = step_ms * NS_PER_MS
        if c.open_time_ns % step_ns:
            out.append(OPEN_TIME_UNALIGNED)
        if c.close_time_ns != c.open_time_ns + step_ns - NS_PER_MS:
            out.append(CLOSE_TIME_MISMATCH)
    spec = _spec(instrument)
    if spec is not None:
        for name, px in (("o", o), ("h", h), ("l", l), ("c", cl)):
            if px > D0 and not _on_grid(px, spec.tick_size):
                out.append(f"{PRICE_OFF_TICK}:{name}")
        if (check_volume_step and spec.step_size is not None and c.volume >= D0
                and not _on_grid(c.volume, spec.step_size)):
            out.append(f"{QTY_OFF_STEP}:volume")
    return out


def check_trade(t: Trade, instrument: Instrument | InstrumentSpec | None = None) -> list[str]:
    out: list[str] = []
    if t.price <= D0:
        out.append(NONPOSITIVE_PRICE)
    if t.qty <= D0:
        out.append(NONPOSITIVE_QTY)
    spec = _spec(instrument)
    if spec is not None:
        if t.price > D0 and not _on_grid(t.price, spec.tick_size):
            out.append(f"{PRICE_OFF_TICK}:price")
        if spec.step_size is not None and t.qty > D0 and not _on_grid(t.qty, spec.step_size):
            out.append(f"{QTY_OFF_STEP}:qty")
    return out


def check_book(b: BookSnapshot, instrument: Instrument | InstrumentSpec | None = None) -> list[str]:
    out: list[str] = []
    bids = [lv.price for lv in b.bids]
    asks = [lv.price for lv in b.asks]
    if any(x <= y for x, y in pairwise(bids)) or any(x >= y for x, y in pairwise(asks)):
        out.append(BOOK_UNSORTED)
    if bids and asks and bids[0] >= asks[0]:
        out.append(BOOK_CROSSED)
    spec = _spec(instrument)
    if spec is not None and any(not _on_grid(p, spec.tick_size) for p in (*bids, *asks)):
        out.append(f"{PRICE_OFF_TICK}:book")
    return out


def is_valid_candle(c: CandleLike, instrument: Instrument | InstrumentSpec | None = None) -> bool:
    return not check_candle(c, instrument)


def violation_code(v: str) -> str:
    """`PRICE_OFF_TICK:h` → `PRICE_OFF_TICK` (для агрегування лічильників)."""
    return v.split(":", 1)[0]


__all__ = [
    "BOOK_CROSSED", "BOOK_UNSORTED", "CLOSE_TIME_MISMATCH", "HIGH_BELOW_LOW", "HIGH_BELOW_MAX_OC",
    "LOW_ABOVE_MIN_OC", "NEGATIVE_QUOTE_VOLUME", "NEGATIVE_TRADES_COUNT", "NEGATIVE_VOLUME",
    "NONPOSITIVE_PRICE", "NONPOSITIVE_QTY", "OPEN_TIME_UNALIGNED", "PRICE_OFF_TICK", "QTY_OFF_STEP",
    "QUOTE_VOLUME_OUT_OF_RANGE", "VWAP_OUT_OF_RANGE", "CandleLike", "InstrumentSpec", "check_book",
    "check_candle", "check_trade", "is_valid_candle", "violation_code",
]
