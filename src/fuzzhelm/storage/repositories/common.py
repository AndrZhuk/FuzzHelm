"""Спільні утиліти репозиторіїв: час ns ↔ TIMESTAMPTZ, числа → NUMERIC, COPY, буферні sink-и.

Найменування: storage/repositories/common.py
Призначення: одна точка конвертації типів на межі «домен ↔ PostgreSQL».
Автор: Андрій Жук, 2026.

Час. Домен працює з наносекундами від епохи UTC (int), а TIMESTAMPTZ зберігає мікросекунди.
Тому ns → TIMESTAMPTZ відкидає субмікросекундну частину (floor), а TIMESTAMPTZ → ns точний.
Для всіх міток, кратних 1 мкс (свічки кратні 1 мс), перетворення туди й назад тотожне.
Наносекундний порядок там, де він потрібен, зберігають колонки BIGINT (event_journal.ts_*_ns).
Арифметика лише цілочисельна: datetime.fromtimestamp(ns / 1e9) втрачає точність уже на 2^53 нс.

Числа. Decimal іде в NUMERIC як є. float (метрики ядра: κ, u, T/R/V, скор якості) перетворює
`sizing.convert.float_to_decimal_exact` — найкоротше десяткове repr, тобто точка конвертації
float → Decimal та сама, що й у решті системи. До масштабу колонки округлює вже PostgreSQL
(«половина від нуля»); там, де результат входить у хеш (крива капіталу), `to_money18` округлює
заздалегідь HALF_EVEN, як core.money. Неcкінченні значення (NaN/±Inf) не пишуться ніколи.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Hashable, Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal
from enum import Enum
from typing import Any

import numpy as np
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.money import DECIMAL_CONTEXT, QUANTUM_INTERNAL, dec
from fuzzhelm.sizing.convert import float_to_decimal_exact

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
NS_PER_US = 1_000
_US_PER_DAY = 86_400 * 1_000_000

# SQLSTATE PostgreSQL, на які спираються викликачі й тести
SQLSTATE_UNIQUE_VIOLATION = "23505"
SQLSTATE_FOREIGN_KEY_VIOLATION = "23503"
SQLSTATE_NOT_NULL_VIOLATION = "23502"
SQLSTATE_CHECK_VIOLATION = "23514"
SQLSTATE_INSUFFICIENT_PRIVILEGE = "42501"


# ---------------------------------------------------------------- час


def ns_to_dt(ns: int) -> datetime:
    """Наносекунди UTC → aware datetime (floor до мікросекунди; точна цілочисельна арифметика)."""
    if isinstance(ns, bool) or not isinstance(ns, int):
        raise TypeError(f"timestamp must be int nanoseconds, got {type(ns).__name__}")
    return EPOCH + timedelta(microseconds=ns // NS_PER_US)


def ns_to_dt_opt(ns: int | None) -> datetime | None:
    return None if ns is None else ns_to_dt(ns)


def dt_to_ns(dt: datetime) -> int:
    """Aware datetime → наносекунди UTC (точно). Naive datetime відкидається: зона невідома."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime has no timezone; expected TIMESTAMPTZ value")
    delta = dt - EPOCH
    return (delta.days * _US_PER_DAY + delta.seconds * 1_000_000 + delta.microseconds) * NS_PER_US


def dt_to_ns_opt(dt: datetime | None) -> int | None:
    return None if dt is None else dt_to_ns(dt)


# ---------------------------------------------------------------- числа


def to_numeric(x: Decimal | float | int | None) -> Decimal | None:
    """Значення для колонки NUMERIC: Decimal як є, int точно, float — через найкоротше repr.

    Неcкінченне значення (float або Decimal: NaN, sNaN, ±Infinity) → ValueError: NUMERIC(p,s) не
    приймає ±Infinity, а NaN PostgreSQL прийняв би мовчки (і NaN > будь-якого числа ламає CHECK-и,
    ST-04); NaN у звітних колонках — помилка викликача, а не значення, тому не пишемо.
    """
    if x is None:
        return None
    if isinstance(x, np.generic):
        # numpy-скаляр → Python-скаляр ДО float-гілки: np.float64 — підклас float, але в numpy 2
        # repr(np.float64(0.25)) == "np.float64(0.25)", і Decimal(repr(x)) упав би
        return to_numeric(x.item())
    if isinstance(x, bool):
        raise TypeError("bool is not a numeric value")
    if isinstance(x, Decimal | int | str):
        d = dec(x)
        if not d.is_finite():
            raise ValueError(f"cannot store non-finite Decimal {d!r} in a NUMERIC column")
        return d
    if isinstance(x, float):
        return float_to_decimal_exact(x)
    raise TypeError(f"cannot store {type(x).__name__} in a NUMERIC column")


def to_money18(x: Decimal | float | int | None) -> Decimal | None:
    """Значення для NUMERIC(38,18), округлене ДО запису: HALF_EVEN до 1e−18 (як core.money.quantize_internal).

    PostgreSQL округлює до масштабу колонки «половину від нуля», а облік і backtest.manifest.equity_hash —
    банківським HALF_EVEN; на рівно-половинному 19-му знаку (…0005) результати розходяться, і хеш кривої,
    перерахований із БД, не збігся б із записаним. Квантуючи тут, БД отримує вже точне значення масштабу 18.
    Явний контекст prec=38: під типовим (prec=28) quantize 20 цілих + 18 дробових розрядів дав би
    InvalidOperation, а понад 20 цілих розрядів — це й так переповнення NUMERIC(38,18).
    """
    d = to_numeric(x)
    if d is None:
        return None
    return d.quantize(QUANTUM_INTERNAL, rounding=ROUND_HALF_EVEN, context=DECIMAL_CONTEXT)


def trim_decimal(x: Decimal) -> Decimal:
    """Прибрати хвостові нулі масштабу NUMERIC(38,18) без експоненти: 0.100000 → 0.1, 100.000 → 100.

    NUMERIC(p,s) не зберігає масштаб вхідного Decimal (100.50 і 100.5 повертаються однаково), тому
    мінімальний масштаб — єдина однозначна форма для відновлення DTO з БД.
    """
    # явний контекст prec=38: під типовим контекстом Python (prec=28) normalize() округлив би
    # 38-значне значення NUMERIC(38,18) — тихо втратив би молодші розряди
    n = x.normalize(context=DECIMAL_CONTEXT)
    exp = n.as_tuple().exponent
    if isinstance(exp, int) and exp > 0:
        return n.quantize(Decimal(1), context=DECIMAL_CONTEXT)
    return n


def enum_value(x: Any) -> Any:
    return x.value if isinstance(x, Enum) else x


def as_bytes(h: bytes | str | None) -> bytes | None:
    """Хеш для BYTEA: bytes як є, hex-рядок → bytes (RunManifest зберігає хеші hex-рядками)."""
    if h is None or isinstance(h, bytes):
        return h
    if isinstance(h, bytearray | memoryview):
        return bytes(h)
    if isinstance(h, str):
        return bytes.fromhex(h)
    raise TypeError(f"expected bytes or hex str, got {type(h).__name__}")


# ---------------------------------------------------------------- рядки ↔ dataclass


def from_mapping[T](cls: type[T], row: Mapping[Any, Any]) -> T:
    """Побудувати dataclass-рядок із мапи колонок БД.

    Поле `x_ns` без однойменної колонки береться з TIMESTAMPTZ-колонки `x` і конвертується в ns;
    поле з однойменною колонкою (напр. BIGINT ts_event_ns) береться як є.
    """
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):  # type: ignore[arg-type]
        if f.name in row:
            kwargs[f.name] = row[f.name]
        elif f.name.endswith("_ns") and f.name[:-3] in row:
            kwargs[f.name] = dt_to_ns_opt(row[f.name[:-3]])
    return cls(**kwargs)


# ---------------------------------------------------------------- пакетна вставка


def split_unique_rounds[T, K: Hashable](items: Iterable[T], key: Callable[[T], K]) -> list[list[T]]:
    """Розкласти послідовність на раунди з унікальними ключами, зберігши порядок входження.

    Раунд i містить i-те входження кожного ключа. Послідовне застосування раундів еквівалентне
    послідовному застосуванню рядків (для кожного ключа порядок збережено), а в межах раунду
    ключі різні — тож один INSERT ... ON CONFLICT DO UPDATE не зачепить той самий рядок двічі
    (інакше PostgreSQL: «ON CONFLICT DO UPDATE command cannot affect row a second time»).
    """
    rounds: list[list[T]] = []
    seen: dict[K, int] = {}
    for it in items:
        k = key(it)
        i = seen.get(k, 0)
        seen[k] = i + 1
        if i == len(rounds):
            rounds.append([])
        rounds[i].append(it)
    return rounds


async def driver_connection(session: AsyncSession) -> Any:
    """Сире з'єднання asyncpg у межах поточної транзакції сесії (для COPY)."""
    conn = await session.connection()
    raw = await conn.get_raw_connection()
    return raw.driver_connection


def supports_copy(session: AsyncSession) -> bool:
    bind = session.get_bind()
    return bind.dialect.driver == "asyncpg"


async def copy_records(session: AsyncSession, table: str, columns: Sequence[str],
                       records: Iterable[Sequence[Any]]) -> int:
    """COPY ... FROM STDIN (binary) у таблицю в поточній транзакції; повертає кількість рядків."""
    conn = await driver_connection(session)
    status: str = await conn.copy_records_to_table(table, records=records, columns=list(columns))
    return int(status.rsplit(maxsplit=1)[-1])


def chunks[T](items: Sequence[T], size: int) -> Iterable[Sequence[T]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


# ---------------------------------------------------------------- sink-и та помилки


class BufferedSink[T]:
    """Синхронний sink для EventJournal/RiskJournal: накопичує записи, репозиторій потім пише пачкою.

    `EventJournal(run_id, sink=buf)` → ... → `await JournalRepo(s).append_many(buf.drain())`.
    """

    def __init__(self) -> None:
        self._items: list[T] = []

    def __call__(self, item: T) -> None:
        self._items.append(item)

    def __len__(self) -> int:
        return len(self._items)

    def drain(self) -> list[T]:
        out, self._items = self._items, []
        return out


def sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE помилки PostgreSQL з винятку SQLAlchemy/драйвера (None, якщо це не помилка БД)."""
    cur: BaseException | None = exc
    while cur is not None:
        orig = cur.orig if isinstance(cur, DBAPIError) else cur
        code = getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)
        if isinstance(code, str):
            return code
        cur = cur.__cause__
    return None


def constraint_name(exc: BaseException) -> str | None:
    """Ім'я порушеного обмеження (asyncpg передає його в полі constraint_name)."""
    cur: BaseException | None = exc
    while cur is not None:
        name = getattr(cur, "constraint_name", None)
        if isinstance(name, str):
            return name
        nxt = cur.orig if isinstance(cur, DBAPIError) else None
        cur = nxt if nxt is not None and nxt is not cur else cur.__cause__
    return None
