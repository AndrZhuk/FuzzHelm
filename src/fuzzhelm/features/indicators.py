"""Інкрементальні індикатори: один бар — O(1) (амортизовано) роботи, без перерахунку історії.

Найменування: features/indicators.py
Призначення: будівельні блоки FeaturePipeline — EMA, ATR/RSI за Уайлдером, ковзні SMA і Welford,
             монотонні деки (канал Дончіана), волатильність Паркінсона, перцентильний ранг,
             ковзна OLS-регресія (нахил + R²).
Автор: Андрій Жук, 2026.

Спільний контракт класів: `update(...) -> float | None` (None до прогріву) і властивість `value`
(останнє значення або None). Стан — лише в самому об'єкті, жодного настінного часу / випадковості.

Ковзні суми з додаванням і відніманням накопичують похибку округлення (випадкове блукання
~√N·eps·|x|). Тому ковзні класи раз на `resync_every` оновлень точно перераховують стан із
кільцевого буфера (двопрохідна формула, O(n)) — амортизовано O(n/resync_every) = O(1).
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right, insort
from collections import deque

DEFAULT_RESYNC = 1024
# Parkinson (1980): σ_P² = (1/(4 ln2))·E[ln²(h/l)]
_FOUR_LN2 = 4.0 * math.log(2.0)


class EMA:
    """E_t = α·p_t + (1−α)·E_{t−1}, α = 2/(n+1). Ініціалізація — SMA перших n значень (як у TA-Lib)."""

    __slots__ = ("_count", "_sum", "_value", "alpha", "n")

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError("EMA period must be >= 1")
        self.n = n
        self.alpha = 2.0 / (n + 1)
        self._count = 0
        self._sum = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        v = self._value
        if v is not None:
            v += self.alpha * (x - v)
            self._value = v
            return v
        self._count += 1
        self._sum += x
        if self._count == self.n:
            self._value = self._sum / self.n
        return self._value


class WilderATR:
    """ATR за Уайлдером: TR_t = max(h−l, |h−c_{t−1}|, |l−c_{t−1}|), ATR_t = ATR_{t−1} + (TR_t − ATR_{t−1})/n.

    α = 1/n (НЕ 2/(n+1)). Перший TR — на другому барі (потрібне c_{t−1}); ATR ініціалізується
    середнім перших n значень TR, тож перше значення — на барі з індексом n (lookback n, як у TA-Lib).
    """

    __slots__ = ("_count", "_prev_close", "_sum", "_value", "alpha", "n")

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError("ATR period must be >= 1")
        self.n = n
        self.alpha = 1.0 / n
        self._prev_close: float | None = None
        self._count = 0
        self._sum = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, h: float, l: float, c: float) -> float | None:
        pc = self._prev_close
        self._prev_close = c
        if pc is None:
            return None
        tr = max(h - l, abs(h - pc), abs(l - pc))
        v = self._value
        if v is not None:
            v += (tr - v) / self.n
            self._value = v
            return v
        self._count += 1
        self._sum += tr
        if self._count == self.n:
            self._value = self._sum / self.n
        return self._value


class WilderRSI:
    """RSI за Уайлдером: Ḡ_t = Ḡ_{t−1} + (G_t − Ḡ_{t−1})/n (так само L̄), RSI = 100·Ḡ/(Ḡ+L̄).

    100·Ḡ/(Ḡ+L̄) ≡ 100 − 100/(1 + Ḡ/L̄), але визначена і при L̄ = 0 (→ 100). Ḡ = L̄ = 0 (пласка ціна) → 50
    (нейтраль; TA-Lib тут повертає 0, що хибно означало б перепроданість). Ініціалізація — середнє
    перших n змін; перше значення на барі з індексом n.
    """

    __slots__ = ("_avg_g", "_avg_l", "_count", "_prev", "_sum_g", "_sum_l", "_value", "alpha", "n")

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError("RSI period must be >= 1")
        self.n = n
        self.alpha = 1.0 / n
        self._prev: float | None = None
        self._count = 0
        self._sum_g = 0.0
        self._sum_l = 0.0
        self._avg_g = 0.0
        self._avg_l = 0.0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, c: float) -> float | None:
        prev = self._prev
        self._prev = c
        if prev is None:
            return None
        d = c - prev
        g = d if d > 0.0 else 0.0
        lo = -d if d < 0.0 else 0.0
        if self._value is None:
            self._count += 1
            self._sum_g += g
            self._sum_l += lo
            if self._count < self.n:
                return None
            self._avg_g = self._sum_g / self.n
            self._avg_l = self._sum_l / self.n
        else:
            self._avg_g += (g - self._avg_g) / self.n
            self._avg_l += (lo - self._avg_l) / self.n
        tot = self._avg_g + self._avg_l
        v = 100.0 * self._avg_g / tot if tot > 0.0 else 50.0
        self._value = v
        return v


class SMA:
    """Ковзне середнє за n значеннями: ковзна сума + періодична точна ресинхронізація."""

    __slots__ = ("_buf", "_count", "_idx", "_since", "_sum", "_value", "n", "resync_every")

    def __init__(self, n: int, resync_every: int = DEFAULT_RESYNC) -> None:
        if n < 1:
            raise ValueError("SMA period must be >= 1")
        self.n = n
        self.resync_every = resync_every
        self._buf = [0.0] * n
        self._idx = 0
        self._count = 0
        self._sum = 0.0
        self._since = 0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        i = self._idx
        if self._count < self.n:
            self._count += 1
            self._sum += x
        else:
            self._sum += x - self._buf[i]
        self._buf[i] = x
        self._idx = i + 1 if i + 1 < self.n else 0
        if self._count < self.n:
            return None
        self._since += 1
        if self._since >= self.resync_every:
            self._since = 0
            self._sum = math.fsum(self._buf)
        v = self._sum / self.n
        self._value = v
        return v


class RollingWelford:
    """Ковзні середнє і дисперсія за Велфордом (add/remove) у вікні n.

    Додавання: M_k = M_{k−1} + (x−M_{k−1})/k, S_k = S_{k−1} + (x−M_{k−1})(x−M_k).
    Заміна y → x у повному вікні: M' = M + (x−y)/n, S' = S + (x−y)(x − M' + y − M).
    На відміну від наївної Σx² − (Σx)²/n, тут віднімаються величини порядку відхилень, а не
    квадратів рівня — тому немає катастрофічного скорочення на рядах із великим зсувом (1e9 + шум).
    `update` повертає середнє (None до n значень); `var` — популяційна дисперсія S/n (як у Боллінджера).
    """

    __slots__ = ("_buf", "_count", "_idx", "_m2", "_mean", "_since", "n", "resync_every")

    def __init__(self, n: int, resync_every: int = DEFAULT_RESYNC) -> None:
        if n < 1:
            raise ValueError("window must be >= 1")
        self.n = n
        self.resync_every = resync_every
        self._buf = [0.0] * n
        self._idx = 0
        self._count = 0
        self._mean = 0.0
        self._m2 = 0.0
        self._since = 0

    @property
    def ready(self) -> bool:
        return self._count == self.n

    @property
    def value(self) -> float | None:
        return self._mean if self._count == self.n else None

    @property
    def mean(self) -> float | None:
        return self.value

    @property
    def var(self) -> float | None:
        """Популяційна дисперсія S/n."""
        return self._m2 / self.n if self._count == self.n else None

    @property
    def sample_var(self) -> float | None:
        if self._count != self.n or self.n < 2:
            return None
        return self._m2 / (self.n - 1)

    @property
    def std(self) -> float | None:
        return math.sqrt(self._m2 / self.n) if self._count == self.n else None

    def _resync(self) -> None:
        mean = math.fsum(self._buf) / self.n
        self._mean = mean
        self._m2 = math.fsum((x - mean) * (x - mean) for x in self._buf)

    def update(self, x: float) -> float | None:
        i = self._idx
        n = self.n
        if self._count < n:
            k = self._count + 1
            self._count = k
            d = x - self._mean
            self._mean += d / k
            self._m2 += d * (x - self._mean)
        else:
            y = self._buf[i]
            m_old = self._mean
            m_new = m_old + (x - y) / n
            self._mean = m_new
            m2 = self._m2 + (x - y) * (x - m_new + y - m_old)
            self._m2 = m2 if m2 > 0.0 else 0.0
        self._buf[i] = x
        self._idx = i + 1 if i + 1 < n else 0
        if self._count < n:
            return None
        self._since += 1
        if self._since >= self.resync_every:
            self._since = 0
            self._resync()
        return self._mean


class _MonotonicDeque:
    """Ковзний екстремум на монотонному деку.

    Кожен елемент входить у дек і виходить з нього рівно раз → амортизовано O(1) на оновлення.
    """

    __slots__ = ("_i", "_idx", "_val", "n")

    _sign = 1.0  # +1 → максимум, −1 → мінімум (порівнюємо sign·x)

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError("window must be >= 1")
        self.n = n
        self._idx: deque[int] = deque()
        self._val: deque[float] = deque()
        self._i = 0

    @property
    def value(self) -> float | None:
        """Екстремум останніх n значень (None, доки їх менше n)."""
        return self._val[0] if self._i >= self.n else None

    def update(self, x: float) -> float | None:
        vals = self._val
        idxs = self._idx
        if self._sign > 0:
            while vals and vals[-1] <= x:
                vals.pop()
                idxs.pop()
        else:
            while vals and vals[-1] >= x:
                vals.pop()
                idxs.pop()
        vals.append(x)
        idxs.append(self._i)
        self._i += 1
        cutoff = self._i - self.n
        while idxs[0] < cutoff:
            idxs.popleft()
            vals.popleft()
        return vals[0] if self._i >= self.n else None


class MonotonicDequeMax(_MonotonicDeque):
    """max останніх n значень (верхня межа каналу Дончіана)."""

    __slots__ = ()
    _sign = 1.0


class MonotonicDequeMin(_MonotonicDeque):
    """min останніх n значень (нижня межа каналу Дончіана)."""

    __slots__ = ()
    _sign = -1.0


def log_range_sq(h: float, l: float) -> float:
    """ln²(h/l); для непозитивних цін (поза доменом Candle) — 0 (немає інформації про діапазон)."""
    if h > 0.0 and l > 0.0:
        r = math.log(h / l)
        return r * r
    return 0.0


class Parkinson:
    """σ_P = √( (1/(4 ln2 · W)) · Σ_{i=t−W+1..t} ln²(h_i/l_i) ), W = 24 — ковзна сума з ресинхронізацією."""

    __slots__ = ("_buf", "_count", "_idx", "_since", "_sum", "_value", "resync_every", "w")

    def __init__(self, w: int, resync_every: int = DEFAULT_RESYNC) -> None:
        if w < 1:
            raise ValueError("window must be >= 1")
        self.w = w
        self.resync_every = resync_every
        self._buf = [0.0] * w
        self._idx = 0
        self._count = 0
        self._sum = 0.0
        self._since = 0
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, h: float, l: float) -> float | None:
        q = log_range_sq(h, l)
        i = self._idx
        if self._count < self.w:
            self._count += 1
            self._sum += q
        else:
            self._sum += q - self._buf[i]
        self._buf[i] = q
        self._idx = i + 1 if i + 1 < self.w else 0
        if self._count < self.w:
            return None
        self._since += 1
        if self._since >= self.resync_every:
            self._since = 0
            self._sum = math.fsum(self._buf)
        s = self._sum
        v = math.sqrt(s / (_FOUR_LN2 * self.w)) if s > 0.0 else 0.0
        self._value = v
        return v


class PercentileRank:
    """V_t = (1/L)·Σ_{i=t−L+1..t} 1[x_i ≤ x_t] — ранг поточного значення у своєму ж вікні (∈ [1/L; 1]).

    Реалізація — відсортований список + bisect: пошук O(log L), вставка/видалення — зсув масиву
    O(L) (memmove у C, для L = 500 це ~кілобайти пам'яті), тобто чесна складність O(L) на бар
    з дуже малою константою. Порядкова статистика на дереві дала б O(log L), але для L ≤ 500
    програє за константою.
    """

    __slots__ = ("_fifo", "_sorted", "_value", "n")

    def __init__(self, n: int) -> None:
        if n < 1:
            raise ValueError("window must be >= 1")
        self.n = n
        self._fifo: deque[float] = deque()
        self._sorted: list[float] = []
        self._value: float | None = None

    @property
    def value(self) -> float | None:
        return self._value

    def update(self, x: float) -> float | None:
        if math.isnan(x):
            raise ValueError("PercentileRank: NaN input breaks the order statistic")
        fifo = self._fifo
        srt = self._sorted
        if len(fifo) == self.n:
            old = fifo.popleft()
            del srt[bisect_left(srt, old)]
        fifo.append(x)
        insort(srt, x)
        if len(fifo) < self.n:
            return None
        v = bisect_right(srt, x) / self.n
        self._value = v
        return v


class RollingOLS:
    """Ковзна OLS-регресія y на x = 0..n−1 у вікні n: нахил і R².

    x̄ = (n−1)/2 і S_xx = n(n²−1)/12 — сталі. Для y — ковзний Велфорд (ȳ, S_yy). Для S_xy при зсуві
    вікна (усі x зменшуються на 1, найстаріший y₀ виходить, y входить з x = n−1), записано в
    центрованих ỹ = y − ȳ_old (Σỹ = 0):  S_xy' = S_xy + ((n+1)/2)·ỹ₀ + ((n−1)/2)·ỹ_new.
    slope = S_xy/S_xx, R² = S_xy²/(S_xx·S_yy). Якщо розкид y на рівні шуму округлення
    (std ≤ 1e−9·|ȳ|) — пояснювати нічого, R² := 0.
    """

    __slots__ = ("_buf", "_count", "_idx", "_mean", "_since", "_sxx", "_sxy", "_syy",
                 "n", "resync_every")

    REL_TOL = 1e-9

    def __init__(self, n: int, resync_every: int = DEFAULT_RESYNC) -> None:
        if n < 2:
            raise ValueError("OLS window must be >= 2")
        self.n = n
        self.resync_every = resync_every
        self._buf = [0.0] * n
        self._idx = 0
        self._count = 0
        self._mean = 0.0
        self._syy = 0.0
        self._sxy = 0.0
        self._sxx = n * (n * n - 1) / 12.0
        self._since = 0

    def _ordered(self) -> list[float]:
        i = self._idx
        return self._buf[i:] + self._buf[:i]

    def _resync(self) -> None:
        ys = self._ordered()
        n = self.n
        mean = math.fsum(ys) / n
        xbar = (n - 1) / 2.0
        self._mean = mean
        self._syy = math.fsum((y - mean) * (y - mean) for y in ys)
        self._sxy = math.fsum((k - xbar) * (y - mean) for k, y in enumerate(ys))

    @property
    def ready(self) -> bool:
        return self._count == self.n

    @property
    def slope(self) -> float | None:
        return self._sxy / self._sxx if self._count == self.n else None

    @property
    def value(self) -> float | None:
        return self.slope

    @property
    def mean(self) -> float | None:
        return self._mean if self._count == self.n else None

    @property
    def r2(self) -> float | None:
        if self._count != self.n:
            return None
        syy = self._syy
        tol = self.REL_TOL * abs(self._mean)
        if syy <= self.n * tol * tol or syy <= 0.0:
            return 0.0
        r2 = self._sxy * self._sxy / (self._sxx * syy)
        return 1.0 if r2 > 1.0 else r2

    def update(self, y: float) -> float | None:
        n = self.n
        i = self._idx
        if self._count < n:
            self._buf[i] = y
            self._idx = i + 1 if i + 1 < n else 0
            self._count += 1
            if self._count == n:
                self._since = 0
                self._resync()
                return self.slope
            return None
        y0 = self._buf[i]
        m_old = self._mean
        self._sxy += 0.5 * (n + 1) * (y0 - m_old) + 0.5 * (n - 1) * (y - m_old)
        m_new = m_old + (y - y0) / n
        self._mean = m_new
        syy = self._syy + (y - y0) * (y - m_new + y0 - m_old)
        self._syy = syy if syy > 0.0 else 0.0
        self._buf[i] = y
        self._idx = i + 1 if i + 1 < n else 0
        self._since += 1
        if self._since >= self.resync_every:
            self._since = 0
            self._resync()
        return self._sxy / self._sxx
