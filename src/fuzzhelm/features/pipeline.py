"""Конвеєр ознак: закритий бар → Features (усі індикатори за O(1) на бар).

Найменування: features/pipeline.py
Призначення: єдине місце, де живе СТАН ознак (рекурентні фільтри, ковзні вікна, лаги, лічильник
             барів від пробою). Детектори — чисті функції від BarWindow і лише читають Features.
Автор: Андрій Жук, 2026.

Параметри за замовчуванням — з брифінгу §5.1/§5.2 і config/detectors.yaml.
"""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields
from typing import Any

from fuzzhelm.features.convert import Bar
from fuzzhelm.features.indicators import (
    DEFAULT_RESYNC,
    EMA,
    MonotonicDequeMax,
    MonotonicDequeMin,
    Parkinson,
    PercentileRank,
    RollingOLS,
    RollingWelford,
    WilderATR,
    WilderRSI,
)

# Масштабна «нуль-підлога»: розкид/діапазон, менший за 1e−9 від рівня ціни, — це шум
# округлення, а не ринкова величина (для BTC ~1e5 це 1e−4 USDT при тіку 0.1).
REL_FLOOR = 1e-9


@dataclass(frozen=True, slots=True)
class FeatureParams:
    n_ema: int = 21                  # EMA, α = 2/(n+1)
    ema_horizon: int = 5             # e_{t−h} для нахилу EmaSlope
    ema_r2_points: int = 5           # R² OLS по e_{t−4..t}
    n_atr: int = 14                  # Уайлдер, α = 1/n
    n_rsi: int = 14                  # Уайлдер, α = 1/n
    rsi_div_lookback: int = 14       # k для дивергенції ціна↔RSI (RsiExhaustion.div_score)
    bb_n: int = 20                   # SMA₂₀ ± k·σ₂₀
    bb_k: float = 2.0
    bb_rank_window: int = 200        # перцентиль bandwidth → довіра BollingerZ
    donchian_n: int = 20             # канал попередніх n барів
    park_window: int = 24            # W у σ_P Паркінсона
    vol_rank_window: int = 500       # L у V_t
    volume_window: int = 60          # v̄, σ_v для z-score обсягу (вхід калібрування режимів)
    resync_every: int = DEFAULT_RESYNC

    @classmethod
    def from_config(cls, cfg: Mapping[str, Any] | None = None) -> FeatureParams:
        """Структурні параметри з config/detectors.yaml (секції detectors.* і features.*)."""
        if cfg is None:
            from fuzzhelm.config import load_yaml  # noqa: PLC0415 — лише коли конфіг не передали

            cfg = load_yaml("detectors")
        det: Mapping[str, Any] = cfg.get("detectors", {}) or {}
        feat: Mapping[str, Any] = cfg.get("features", {}) or {}
        d = cls()

        def pick(section: str, key: str, default: Any) -> Any:
            sec = det.get(section) or {}
            return sec.get(key, default)

        return cls(
            n_ema=int(pick("ema_slope", "n_ema", d.n_ema)),
            ema_horizon=int(pick("ema_slope", "horizon", d.ema_horizon)),
            ema_r2_points=int(pick("ema_slope", "r2_points", d.ema_r2_points)),
            n_atr=int(feat.get("n_atr", d.n_atr)),
            n_rsi=int(pick("rsi_exhaustion", "n", d.n_rsi)),
            rsi_div_lookback=int(pick("rsi_exhaustion", "div_lookback", d.rsi_div_lookback)),
            bb_n=int(pick("bollinger_z", "n", d.bb_n)),
            bb_k=float(pick("bollinger_z", "k", d.bb_k)),
            bb_rank_window=int(pick("bollinger_z", "bw_rank_window", d.bb_rank_window)),
            donchian_n=int(pick("donchian", "n", d.donchian_n)),
            park_window=int(pick("vol_regime", "window", d.park_window)),
            vol_rank_window=int(pick("vol_regime", "rank_window", d.vol_rank_window)),
            volume_window=int(feat.get("volume_window", d.volume_window)),
        )

    def warmup(self) -> dict[str, int]:
        """Скільки барів (рахуючи з першого) потрібно, щоб поле Features стало не-None.

        Поля пробою Дончіана (donch_ref, donch_dir, bars_since_breakout) залежать від даних
        (з'являються після першого пробою) і тут не наведені.
        """
        k = self.rsi_div_lookback
        return {
            "ema": self.n_ema,
            "ema_lag": self.n_ema + self.ema_horizon,
            "ema_r2": self.n_ema + self.ema_r2_points - 1,
            "atr": self.n_atr + 1,
            "rsi": self.n_rsi + 1,
            "rsi_delta": self.n_rsi + 1 + k,
            "close_delta": k + 1,
            "sma": self.bb_n,
            "sigma": self.bb_n,
            "bb_upper": self.bb_n,
            "bb_lower": self.bb_n,
            "bb_bw": self.bb_n,
            "bb_bw_rank": self.bb_n + self.bb_rank_window - 1,
            "donch_hi": self.donchian_n + 1,
            "donch_lo": self.donchian_n + 1,
            "park_sigma": self.park_window,
            "vol_rank": self.park_window + self.vol_rank_window - 1,
            "vol_mean": self.volume_window,
            "vol_z": self.volume_window,
        }

    @property
    def max_lookback(self) -> int:
        return max(self.warmup().values())


@dataclass(frozen=True, slots=True)
class Features:
    """Ознаки на закритті бару t. None — ще не прогріто (або подія, напр. пробій, ще не траплялась)."""

    ema: float | None = None                  # E_t
    ema_lag: float | None = None              # E_{t−h}, h = ema_horizon
    ema_r2: float | None = None               # R² OLS по останніх ema_r2_points значеннях E
    atr: float | None = None                  # ATR_t (Уайлдер)
    rsi: float | None = None                  # RSI_t (Уайлдер)
    rsi_delta: float | None = None            # RSI_t − RSI_{t−k}
    close_delta: float | None = None          # c_t − c_{t−k}
    sma: float | None = None                  # SMA₂₀ (середнє Велфорда)
    sigma: float | None = None                # σ₂₀ (популяційне)
    bb_upper: float | None = None             # SMA + k·σ
    bb_lower: float | None = None             # SMA − k·σ
    bb_bw: float | None = None                # bandwidth = 2σ/μ (брифінг §5.1)
    bb_bw_rank: float | None = None           # ρ_t — перцентильний ранг bw за bb_rank_window барів
    donch_hi: float | None = None             # U_t = max(h_{t−n..t−1}) — канал ПОПЕРЕДНІХ n барів
    donch_lo: float | None = None             # L_t = min(l_{t−n..t−1})
    donch_ref: float | None = None            # рівень останнього пробою (пробитий U або L)
    donch_dir: float | None = None            # +1 — пробій угору, −1 — униз
    bars_since_breakout: float | None = None  # 0 на барі пробою, далі +1 за бар
    park_sigma: float | None = None           # σ_P (Паркінсон, W барів)
    vol_rank: float | None = None             # V_t — перцентильний ранг σ_P за L барів
    vol_mean: float | None = None             # v̄ за volume_window барів
    vol_z: float | None = None                # (v_t − v̄)/σ_v

    def as_dict(self) -> dict[str, float | None]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


FEATURE_NAMES: tuple[str, ...] = tuple(f.name for f in fields(Features))


class FeaturePipeline:
    """Стан усіх індикаторів. `update(bar)` — O(1) на бар.

    Амортизовано; перцентильні ранги — O(L) memmove з малою константою (див. PercentileRank).
    """

    def __init__(self, params: FeatureParams | None = None) -> None:
        p = params or FeatureParams()
        self.params = p
        r = p.resync_every
        self._ema = EMA(p.n_ema)
        self._ema_hist: deque[float] = deque(maxlen=p.ema_horizon + 1)
        self._ema_ols = RollingOLS(p.ema_r2_points, resync_every=r)
        self._atr = WilderATR(p.n_atr)
        self._rsi = WilderRSI(p.n_rsi)
        self._rsi_hist: deque[float] = deque(maxlen=p.rsi_div_lookback + 1)
        self._close_hist: deque[float] = deque(maxlen=p.rsi_div_lookback + 1)
        self._bb = RollingWelford(p.bb_n, resync_every=r)
        self._bw_rank = PercentileRank(p.bb_rank_window)
        self._dmax = MonotonicDequeMax(p.donchian_n)
        self._dmin = MonotonicDequeMin(p.donchian_n)
        self._brk_ref: float | None = None
        self._brk_dir: float | None = None
        self._brk_bars: float | None = None
        self._park = Parkinson(p.park_window, resync_every=r)
        self._vol_rank = PercentileRank(p.vol_rank_window)
        self._volume = RollingWelford(p.volume_window, resync_every=r)
        self._n = 0

    @property
    def max_lookback(self) -> int:
        """Бари до повного прогріву (для embargo walk-forward). Рекурентні EMA/ATR/RSI мають
        нескінченну, але геометрично згасаючу пам'ять: (1−1/14)^523 ≈ 1.6e−17."""
        return self.params.max_lookback

    @property
    def bars_seen(self) -> int:
        return self._n

    def update(self, bar: Bar) -> Features:
        p = self.params
        c = bar.c
        h = bar.h
        lo = bar.l
        self._n += 1

        # --- тренд: EMA, її лаг і якість лінійної апроксимації (R²)
        ema = self._ema.update(c)
        ema_lag: float | None = None
        ema_r2: float | None = None
        if ema is not None:
            hist = self._ema_hist
            hist.append(ema)
            if len(hist) == hist.maxlen:
                ema_lag = hist[0]
            if self._ema_ols.update(ema) is not None:
                ema_r2 = self._ema_ols.r2

        # --- Уайлдер
        atr = self._atr.update(h, lo, c)
        rsi = self._rsi.update(c)
        rsi_delta: float | None = None
        ch = self._close_hist
        ch.append(c)
        close_delta = c - ch[0] if len(ch) == ch.maxlen else None
        if rsi is not None:
            rh = self._rsi_hist
            rh.append(rsi)
            if len(rh) == rh.maxlen:
                rsi_delta = rsi - rh[0]

        # --- Боллінджер: μ, σ за Велфордом; bandwidth і його перцентиль
        sma = self._bb.update(c)
        sigma = bb_up = bb_dn = bw = bw_rank = None
        if sma is not None:
            sigma = self._bb.std
            assert sigma is not None
            bb_up = sma + p.bb_k * sigma
            bb_dn = sma - p.bb_k * sigma
            den = abs(sma)
            bw = 2.0 * sigma / den if den > 0.0 else 0.0
            bw_rank = self._bw_rank.update(bw)

        # --- Дончіан: канал попередніх n барів (без поточного), потім поточний бар входить у вікно
        u = self._dmax.value
        d = self._dmin.value
        self._dmax.update(h)
        self._dmin.update(lo)
        if u is not None and d is not None:
            # Пробій — подія «закриття поза каналом попередніх n барів» (правило Дончіана/«черепах»)
            if c > u:
                self._brk_ref, self._brk_dir, self._brk_bars = u, 1.0, 0.0
            elif c < d:
                self._brk_ref, self._brk_dir, self._brk_bars = d, -1.0, 0.0
            elif self._brk_bars is not None:
                self._brk_bars += 1.0

        # --- волатильність Паркінсона і її перцентильний ранг V_t
        park = self._park.update(h, lo)
        vol_rank = self._vol_rank.update(park) if park is not None else None

        # --- обсяг: v̄ і z-score
        vm = self._volume.update(bar.v)
        vz: float | None = None
        if vm is not None:
            vs = self._volume.std
            assert vs is not None
            vz = (bar.v - vm) / vs if vs > REL_FLOOR * abs(vm) and vs > 0.0 else 0.0

        return Features(
            ema=ema, ema_lag=ema_lag, ema_r2=ema_r2,
            atr=atr, rsi=rsi, rsi_delta=rsi_delta, close_delta=close_delta,
            sma=sma, sigma=sigma, bb_upper=bb_up, bb_lower=bb_dn, bb_bw=bw, bb_bw_rank=bw_rank,
            donch_hi=u, donch_lo=d, donch_ref=self._brk_ref, donch_dir=self._brk_dir,
            bars_since_breakout=self._brk_bars,
            park_sigma=park, vol_rank=vol_rank, vol_mean=vm, vol_z=vz,
        )

    def run(self, bars: Iterable[Bar]) -> list[Features]:
        """Пакетний прогін (бектест/калібрування): той самий інкрементальний код, що й у live."""
        return [self.update(b) for b in bars]


def is_finite(x: float | None) -> bool:
    return x is not None and math.isfinite(x)
