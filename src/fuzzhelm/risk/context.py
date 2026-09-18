"""Контекст ризик-перевірки і трекер капіталу (пік, просадка, денний PnL з UTC-північчю).

Найменування: risk/context.py
Призначення: усе, що бачать правила ризик-ланцюга, — один незмінний RiskContext; усе, що
накопичується в часі (running peak, E_open дня), — EquityTracker. Час — лише мітки подій (ts_ns
від ін'єктованого Clock або часу бару), настінного годинника тут немає.
Автор: Андрій Жук, 2026.

Семантика заявки. Правила гейтять лише ПРИРІСТ експозиції: зменшення/закриття дозволене завжди.
Для поточної позиції q_cur і цільової q_tgt (обидві зі знаком):
    той самий бік або q_cur = 0:  base = min(|q_cur|, |q_tgt|),  increase = max(0, |q_tgt| − |q_cur|)
    розворот (знаки різні):        base = 0,                      increase = |q_tgt|   (закриття — редукція)
    post = base + increase = |q_tgt|
Правило-обмеження «post ≤ q_max» перетворюється на вердикт через cap_increase(): ALLOW, якщо вміщається;
SHRINK((q_max − base)/increase), якщо вміщається лише частина приросту; VETO, якщо місця немає зовсім.
Оскільки кожне обмеження замкнене донизу (менший приріст теж його задовольняє), добуток множників
задовольняє всі обмеження одночасно — це і є сенс алгебри вердиктів.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction

from fuzzhelm.core.clock import utc_day_start_ns
from fuzzhelm.core.enums import RiskState
from fuzzhelm.core.money import D0, D1, dec
from fuzzhelm.risk.verdict import ALLOW, VETO, Verdict, shrink

DEFAULT_MMR = Decimal("0.005")
DEFAULT_LEVERAGE_SETTING = Decimal(3)


# ---------------------------------------------------------------- трекер капіталу


@dataclass(frozen=True, slots=True)
class EquitySnapshot:
    ts_ns: int
    equity: Decimal
    peak: Decimal                 # max_{τ≤t} E_τ (після rebase — від моменту rebase)
    drawdown: Decimal             # DD_t = 1 − E_t / peak
    day_start_ns: int             # початок поточної UTC-доби
    equity_day_open: Decimal      # E_open: капітал, з яким почалася доба
    pnl_day: Decimal              # E_t − E_open

    @property
    def day_return(self) -> Decimal:
        """PnL_day / E_open (0, якщо E_open ≤ 0 — тоді спрацьовує просадка, а не денний ліміт)."""
        if self.equity_day_open <= 0:
            return D0
        return self.pnl_day / self.equity_day_open


class EquityTracker:
    """Running peak і денний PnL. Мітки часу — неспадні (як у ManualClock), інакше ValueError.

    E_open нової доби = останній капітал, що спостерігався ДО межі UTC-північчі (капітал, «перенесений»
    через північ); якщо попередніх спостережень немає — перше спостереження доби.
    """

    __slots__ = ("_day", "_day_open", "_last_equity", "_last_ts", "_peak", "_snap")

    def __init__(self) -> None:
        self._peak: Decimal | None = None
        self._day: int | None = None
        self._day_open: Decimal = D0
        self._last_equity: Decimal | None = None
        self._last_ts: int | None = None
        self._snap: EquitySnapshot | None = None

    @property
    def snapshot(self) -> EquitySnapshot | None:
        return self._snap

    def update(self, ts_ns: int, equity: Decimal) -> EquitySnapshot:
        if self._last_ts is not None and ts_ns < self._last_ts:
            raise ValueError(f"equity timestamps must be non-decreasing: {ts_ns} < {self._last_ts}")
        day = utc_day_start_ns(ts_ns)
        if self._day is None:
            self._day_open = equity
        elif day != self._day:
            # скидання денного ліміту рівно на межі UTC-доби за часом події, не за настінним годинником
            self._day_open = self._last_equity if self._last_equity is not None else equity
        self._day = day
        if self._peak is None or equity > self._peak:
            self._peak = equity
        self._last_equity = equity
        self._last_ts = ts_ns
        self._snap = self._make(ts_ns, equity)
        return self._snap

    def rebase(self) -> EquitySnapshot | None:
        """Перебазувати пік і E_open на поточний капітал (після ручного зняття HALTED адміністратором).

        Без цього засувка спрацювала б знову на наступному барі: просадка рахується від історичного
        піку, а пласка (закрита) книга не може його відіграти.
        """
        if self._last_equity is None or self._last_ts is None:
            return None
        self._peak = self._last_equity
        self._day_open = self._last_equity
        self._snap = self._make(self._last_ts, self._last_equity)
        return self._snap

    def _make(self, ts_ns: int, equity: Decimal) -> EquitySnapshot:
        peak = self._peak if self._peak is not None else equity
        dd = D1 - equity / peak if peak > 0 else D1
        day = self._day if self._day is not None else utc_day_start_ns(ts_ns)
        return EquitySnapshot(ts_ns, equity, peak, dd, day, self._day_open, equity - self._day_open)


# ---------------------------------------------------------------- контекст перевірки


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


@dataclass(frozen=True, slots=True)
class RiskContext:
    """Знімок усього, що потрібно шести правилам, для однієї заявки на одному інструменті.

    Грошові/цінові величини — Decimal. `target_qty`/`current_qty` — зі знаком (LONG > 0, SHORT < 0).
    `stop_distance` — Δ_stop захисного стопа в одиницях ціни (у сайзері χ·ATR).
    `last_data_ns` — мітка останньої ринкової події (None → дорівнює ts_ns: лаг 0, бар-синхронний
    бектест).
    `day_start_ns` — доба, до якої належать pnl_day/equity_day_open (None → доба ts_ns).
    `leverage_setting` — біржове плече позиції L_set (початкова маржа = номінал / L_set).
    Похідні (не параметри конструктора): `base_qty` — незмінна частина позиції, `increase_qty` — приріст,
    який гейтять правила (0 ⇒ заявка лише зменшує/закриває), `post_qty` = |target_qty|.
    """

    ts_ns: int
    instrument: str
    price: Decimal
    equity: Decimal
    current_qty: Decimal
    target_qty: Decimal
    atr: Decimal
    stop_distance: Decimal
    drawdown: Decimal = D0
    pnl_day: Decimal = D0
    equity_day_open: Decimal | None = None
    day_start_ns: int | None = None
    gross_notional_other: Decimal = D0
    mmr: Decimal = DEFAULT_MMR
    maint_amount: Decimal = D0
    leverage_setting: Decimal = DEFAULT_LEVERAGE_SETTING
    last_data_ns: int | None = None
    dq_score: Decimal = D1
    risk_state: RiskState = RiskState.NORMAL
    step_size: Decimal | None = None
    # похідні величини заявки обчислюються один раз (їх читає кожне з 7 правил ланцюга)
    base_qty: Decimal = field(init=False, repr=False, compare=False)
    increase_qty: Decimal = field(init=False, repr=False, compare=False)
    post_qty: Decimal = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.price <= 0:
            raise ValueError(f"price must be > 0, got {self.price}")
        if self.atr < 0 or self.stop_distance < 0:
            raise ValueError("atr and stop_distance must be >= 0")
        if self.leverage_setting <= 0:
            raise ValueError("leverage_setting must be > 0")
        if self.gross_notional_other < 0:
            raise ValueError("gross_notional_other must be >= 0")
        cur, tgt = abs(self.current_qty), abs(self.target_qty)
        if self.target_qty == 0:
            base, inc = D0, D0                          # закриття — чиста редукція
        elif self.is_flip:
            base, inc = D0, tgt                         # розворот: закрити (редукція) + відкрити (приріст)
        else:
            base, inc = min(cur, tgt), max(D0, tgt - cur)
        object.__setattr__(self, "base_qty", base)      # frozen: встановлюємо рівно один раз
        object.__setattr__(self, "increase_qty", inc)
        object.__setattr__(self, "post_qty", tgt)

    @classmethod
    def from_snapshot(cls, snap: EquitySnapshot, **fields: object) -> RiskContext:
        """Контекст, у якому equity/drawdown/pnl_day/E_open/доба взяті з EquityTracker."""
        base: dict[str, object] = {
            "ts_ns": snap.ts_ns, "equity": snap.equity, "drawdown": snap.drawdown,
            "pnl_day": snap.pnl_day, "equity_day_open": snap.equity_day_open,
            "day_start_ns": snap.day_start_ns,
        }
        base.update(fields)
        return cls(**base)  # type: ignore[arg-type]

    # --- похідні величини заявки
    @property
    def target_side(self) -> int:
        return _sign(self.target_qty)

    @property
    def is_flip(self) -> bool:
        return _sign(self.current_qty) * _sign(self.target_qty) < 0

    @property
    def post_notional(self) -> Decimal:
        return self.post_qty * self.price

    @property
    def lag_ns(self) -> int:
        return 0 if self.last_data_ns is None else self.ts_ns - self.last_data_ns

    @property
    def effective_day_open(self) -> Decimal:
        return self.equity if self.equity_day_open is None else self.equity_day_open


def cap_increase(ctx: RiskContext, q_max_post: Decimal) -> Verdict:
    """Обмеження «post ≤ q_max_post» → вердикт на ПРИРІСТ (див. докстрінг модуля)."""
    inc = ctx.increase_qty
    if inc <= 0 or ctx.post_qty <= q_max_post:
        return ALLOW
    room = q_max_post - ctx.base_qty
    if room <= 0:
        return VETO                   # SHRINK(f→0) вироджується у VETO
    return shrink(Fraction(room) / Fraction(inc))


def ratio(num: Decimal, den: Decimal) -> Decimal | None:
    """num/den або None, якщо знаменник ≤ 0 (спостереження не визначене)."""
    return None if den <= 0 else num / den


def dec_ns_to_s(ns: int) -> Decimal:
    return dec(ns) / dec(1_000_000_000)
