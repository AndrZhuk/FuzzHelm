"""Вікно барів і ознак з охоронцем від зазирання в майбутнє (look-ahead).

Найменування: features/window.py
Призначення: BarWindow — кільцевий буфер останніх `capacity` барів і їхніх Features, єдиний
             вхід детекторів; GuardedArray / LookaheadGuard — обгортка повного масиву для бектесту,
             що забороняє читати індекс, більший за курсор.
Автор: Андрій Жук, 2026.

Конвенція лагів: lag = 0 — поточний (щойно закритий) бар t, lag = k — бар t−k. Від'ємний лаг
означає майбутнє → LookaheadError. Лаг, глибший за наявну історію, — це не майбутнє, а нестача
даних → IndexError.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any, overload

import numpy as np
import numpy.typing as npt

from fuzzhelm.core.errors import LookaheadError
from fuzzhelm.features.convert import Bar

if TYPE_CHECKING:
    from fuzzhelm.features.pipeline import Features

BAR_FIELDS: frozenset[str] = frozenset({"o", "h", "l", "c", "v", "qv", "n", "t_ns"})
# Цілі поля Bar: t_ns ~1.8e18 > 2^53, тож у float64 втратив би точність (ulp = 256 нс).
_INT_BAR_FIELDS: frozenset[str] = frozenset({"n", "t_ns"})


class BarWindow:
    """Кільцевий буфер (Bar, Features) ємністю `capacity`; `t` — індекс поточного бару (−1 до першого)."""

    __slots__ = ("_bars", "_count", "_feats", "_head", "_t", "capacity")

    def __init__(self, capacity: int) -> None:
        if capacity < 1:
            raise ValueError("capacity must be >= 1")
        self.capacity = capacity
        self._bars: list[Bar | None] = [None] * capacity
        self._feats: list[Features | None] = [None] * capacity
        self._head = -1          # позиція поточного бару в буфері
        self._count = 0
        self._t = -1

    def __len__(self) -> int:
        return self._count

    @property
    def t(self) -> int:
        """Абсолютний індекс поточного бару від початку потоку (0-based)."""
        return self._t

    def append(self, bar: Bar, feats: Features) -> None:
        h = self._head + 1
        if h == self.capacity:
            h = 0
        self._head = h
        self._bars[h] = bar
        self._feats[h] = feats
        if self._count < self.capacity:
            self._count += 1
        self._t += 1

    def _pos(self, lag: int) -> int:
        if lag < 0:
            raise LookaheadError(f"lag={lag} < 0 reads the future (t={self._t})")
        if lag >= self._count:
            raise IndexError(f"lag={lag} beyond history of {self._count} bars")
        p = self._head - lag
        return p + self.capacity if p < 0 else p

    def bar(self, lag: int = 0) -> Bar:
        b = self._bars[self._pos(lag)]
        assert b is not None
        return b

    def feats(self, lag: int = 0) -> Features:
        f = self._feats[self._pos(lag)]
        assert f is not None
        return f

    def feat(self, name: str, lag: int = 0) -> float | None:
        return getattr(self.feats(lag), name)  # type: ignore[no-any-return]

    def series(self, name: str, n: int) -> npt.NDArray[Any]:
        """Останні n значень поля бару (o,h,l,c,v,...) або ознаки — від старого до нового; None → NaN.

        dtype: int64 для цілих полів бару (`t_ns`, `n`), інакше float64.
        """
        if n < 0:
            raise LookaheadError(f"series length n={n} < 0")
        if n > self._count:
            raise IndexError(f"series({name!r}, {n}) beyond history of {self._count} bars")
        if name in _INT_BAR_FIELDS:
            return np.array([getattr(self._bars[self._pos(n - 1 - k)], name) for k in range(n)],
                            dtype=np.int64)
        src: Sequence[Any] = self._bars if name in BAR_FIELDS else self._feats
        out = np.empty(n, dtype=np.float64)
        for k in range(n):
            v = getattr(src[self._pos(n - 1 - k)], name)
            out[k] = np.nan if v is None else v
        return out


class GuardedArray:
    """Повний масив (напр. усі бари бектесту) з курсором: читати можна лише індекси ≤ cursor.

    Індексування — як у numpy (від'ємні індекси відраховуються від кінця ПОВНОГО масиву, тобто
    здебільшого від майбутнього — і тому відхиляються охоронцем). Зрізи перевіряються за
    найбільшим індексом, який вони зачеплять.
    """

    __slots__ = ("_arr", "_cursor")

    def __init__(self, arr: npt.ArrayLike, cursor: int = 0) -> None:
        self._arr = np.asarray(arr)
        if self._arr.ndim == 0:
            raise ValueError("GuardedArray needs at least a 1-D array")
        self._cursor = -1
        self.cursor = cursor

    @property
    def cursor(self) -> int:
        return self._cursor

    @cursor.setter
    def cursor(self, value: int) -> None:
        if not -1 <= value < len(self._arr):
            raise IndexError(f"cursor {value} outside [-1, {len(self._arr) - 1}]")
        self._cursor = value

    def advance(self, steps: int = 1) -> int:
        self.cursor = self._cursor + steps
        return self._cursor

    def __len__(self) -> int:
        """Скільки елементів уже видно (cursor + 1), а не довжина повного масиву."""
        return self._cursor + 1

    def _check_index(self, i: int) -> None:
        n = len(self._arr)
        j = i + n if i < 0 else i
        if j > self._cursor:
            raise LookaheadError(f"index {i} (→{j}) > cursor {self._cursor}")

    @overload
    def __getitem__(self, key: int) -> Any: ...
    @overload
    def __getitem__(self, key: slice) -> npt.NDArray[Any]: ...

    def __getitem__(self, key: int | slice | tuple[Any, ...]) -> Any:
        first = key[0] if isinstance(key, tuple) else key
        if isinstance(first, slice):
            rng = range(*first.indices(len(self._arr)))
            top = max(rng[0], rng[-1]) if len(rng) > 0 else -1
            if top > self._cursor:
                raise LookaheadError(f"slice {first} reaches index {top} > cursor {self._cursor}")
        elif isinstance(first, int | np.integer):
            self._check_index(int(first))
        else:
            raise TypeError(f"unsupported index type {type(first).__name__} for GuardedArray")
        out = self._arr[key]
        if isinstance(out, np.ndarray):
            out = out.view()
            out.flags.writeable = False
        return out

    def visible(self) -> npt.NDArray[Any]:
        """Незмінний view на arr[:cursor+1] — усе минуле й поточне."""
        v = self._arr[: self._cursor + 1].view()
        v.flags.writeable = False
        return v


# Назва з брифінгу (§4.1 «BarWindow[LookaheadGuard]»): той самий охоронець.
LookaheadGuard = GuardedArray
