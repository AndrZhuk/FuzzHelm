"""Облік рахунку безстрокових ф'ючерсів (USDT-маржинальних): позиції, PnL, комісії, фандинг, капітал.

Найменування: execution/portfolio.py
Призначення: єдине джерело кривої капіталу для бектесту/реплею; тотожність обліку перевіряється
після КОЖНОГО кроку (test_equity_accounting_identity).
Автор: Андрій Жук, 2026.

Для лінійного (USDT-M) перпа обидва записи капіталу еквівалентні:

  маржинальна форма:  E = W₀ + realized − fees − funding + Σ qᵢ·(markᵢ − avg_entryᵢ)
                          └──────── wallet_balance ────────┘ └────── unrealized ──────┘
  касова форма:       E = cash + Σ qᵢ·markᵢ − fees,   cash := W₀ − Σ_fills dq·price − funding

де dq — підписана кількість виконання (купівля > 0). Касова форма — це формула брифінгу
«cash + Σq·p − Σfees == equity», адаптована до перпа: `cash` тут — «спот-еквівалентні» гроші
(рух номіналу угод і фандингу, БЕЗ комісій), а не баланс гаманця; маржа — лише забезпечення і
на капітал не впливає. Рівність двох форм — не тавтологія: маржинальна форма рахується через
середню ціну входу і реалізований PnL (з частковими закриттями та розворотами через нуль),
касова — лише з сирих грошових потоків. Помилка в знаку шорта чи в розвороті розриває тотожність.

Реалізований PnL — метод середньої ціни (як на Binance). Середня ціна зберігається як
cost_basis = Σ dq·price відкритої частини (без ділення); ділення лише при частковому закритті.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from fuzzhelm.core.dto import Fill
from fuzzhelm.core.enums import ExitReason, Side
from fuzzhelm.core.money import D0
from fuzzhelm.execution.cost_model import FundingCharge


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


@dataclass(slots=True)
class Position:
    """Відкрита позиція за інструментом + накопичувачі поточної угоди (від відкриття до нуля)."""

    instrument: str
    qty: Decimal = D0                  # зі знаком: лонг > 0, шорт < 0
    cost_basis: Decimal = D0           # Σ dq·price відкритої частини (= qty·avg_entry)
    realized_pnl: Decimal = D0         # валовий реалізований PnL за весь час по інструменту
    funding_paid: Decimal = D0         # за весь час по інструменту
    fees_paid: Decimal = D0            # за весь час по інструменту
    opened_at_ns: int | None = None
    max_adverse_excursion: Decimal = D0   # мінімум нереалізованого PnL поточної угоди (≤ 0)
    # накопичувачі поточної угоди
    _t_side: Side = Side.FLAT
    _t_max_qty: Decimal = D0
    _t_realized: Decimal = D0
    _t_fees: Decimal = D0
    _t_funding: Decimal = D0
    _t_entry_notional: Decimal = D0
    _t_entry_qty: Decimal = D0
    _t_exit_notional: Decimal = D0
    _t_exit_qty: Decimal = D0

    @property
    def side(self) -> Side:
        return Side(_sign(self.qty))

    @property
    def avg_entry(self) -> Decimal | None:
        return None if self.qty == 0 else self.cost_basis / self.qty

    def unrealized(self, mark: Decimal) -> Decimal:
        return self.qty * mark - self.cost_basis


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """Завершена угода: від відкриття позиції з нуля до повернення в нуль (або розвороту)."""

    instrument: str
    side: Side
    qty: Decimal                       # максимальний |q| за життя угоди
    entry_price: Decimal               # VWAP нарощувань
    exit_price: Decimal                # VWAP зменшень
    gross_pnl: Decimal
    fees: Decimal
    funding: Decimal
    pnl: Decimal                       # gross_pnl − fees − funding
    entry_notional: Decimal
    exit_notional: Decimal
    opened_at_ns: int
    closed_at_ns: int
    exit_reason: ExitReason | None
    max_adverse_excursion: Decimal


@dataclass(frozen=True, slots=True)
class PortfolioSnapshot:
    """Рядок для equity_point (усе Decimal; ts — час оцінки)."""

    ts_ns: int
    equity: Decimal
    cash: Decimal                      # спот-еквівалентні гроші (для тотожності)
    wallet_balance: Decimal            # баланс гаманця перпа
    unrealized: Decimal
    gross_exposure: Decimal
    net_exposure: Decimal
    leverage: Decimal | None           # gross/equity; None при equity ≤ 0
    fees_paid: Decimal
    funding_paid: Decimal
    realized_pnl: Decimal


class Portfolio:
    """Рахунок USDT-M перпів. `cash` у конструкторі — початковий депозит W₀."""

    def __init__(self, cash: Decimal) -> None:
        if cash < 0:
            raise ValueError(f"initial cash must be >= 0, got {cash}")
        self.initial = cash
        self._cash = cash
        self.fees_paid = D0
        self.funding_paid = D0
        self.realized_pnl = D0
        self.traded_notional = D0          # Σ|qty·price| усіх виконань (для turnover)
        self.n_fills = 0
        self.positions: dict[str, Position] = {}
        self.marks: dict[str, Decimal] = {}
        self.closed_trades: list[ClosedTrade] = []

    # ------------------------------------------------------------ події

    def apply_fill(self, fill: Fill, exit_reason: ExitReason | None = None) -> list[ClosedTrade]:
        """Застосувати виконання. Повертає угоди, закриті цим виконанням (0 або 1)."""
        inst = fill.instrument
        price = fill.price
        sgn = int(fill.side)
        if sgn == 0:
            raise ValueError("fill side must be LONG or SHORT")
        qty = fill.qty
        dq = sgn * qty
        fee = fill.fee
        pos = self.positions.get(inst)
        if pos is None:
            pos = self.positions[inst] = Position(inst)
        self._cash -= dq * price
        self.fees_paid += fee
        pos.fees_paid += fee
        self.traded_notional += qty * price
        self.n_fills += 1
        if inst not in self.marks:
            self.marks[inst] = price       # до першої оцінки mark = остання ціна угоди
        q = pos.qty
        closed_out: list[ClosedTrade] = []
        if q == 0 or _sign(q) == sgn:
            if q == 0:
                self._start_trade(pos, Side(sgn), fill.ts_fill_ns)
            pos.cost_basis += dq * price
            pos.qty = q + dq
            pos._t_fees += fee
            pos._t_entry_notional += qty * price
            pos._t_entry_qty += qty
            pos._t_max_qty = max(pos._t_max_qty, abs(pos.qty))
            return closed_out
        # зменшення / закриття / розворот
        abs_q = abs(q)
        closed = min(qty, abs_q)
        cost_removed = pos.cost_basis if closed == abs_q else pos.cost_basis * closed / abs_q
        pnl = _sign(q) * closed * price - cost_removed
        self.realized_pnl += pnl
        pos.realized_pnl += pnl
        pos.cost_basis -= cost_removed
        pos.qty = q + sgn * closed
        fee_close = fee if closed == qty else fee * closed / qty
        pos._t_realized += pnl
        pos._t_fees += fee_close
        pos._t_exit_notional += closed * price
        pos._t_exit_qty += closed
        if pos.qty == 0:
            pos.cost_basis = D0
            closed_out.append(self._finish_trade(pos, fill.ts_fill_ns, exit_reason))
        remainder = qty - closed
        if remainder > 0:
            # розворот через нуль: залишок відкриває нову угоду протилежного напряму
            self._start_trade(pos, Side(sgn), fill.ts_fill_ns)
            pos.cost_basis = sgn * remainder * price
            pos.qty = sgn * remainder
            pos._t_fees += fee - fee_close
            pos._t_entry_notional += remainder * price
            pos._t_entry_qty += remainder
            pos._t_max_qty = remainder
        return closed_out

    def apply_funding(self, charge: FundingCharge) -> None:
        """Фандинг: amount > 0 — рахунок сплатив (лонг при додатній ставці), < 0 — отримав."""
        amount = charge.amount
        self._cash -= amount
        self.funding_paid += amount
        pos = self.positions.get(charge.instrument)
        if pos is None:
            pos = self.positions[charge.instrument] = Position(charge.instrument)
        pos.funding_paid += amount
        pos._t_funding += amount

    def mark(self, prices: Mapping[str, Decimal]) -> Decimal:
        """Оновити mark-ціни, MAE відкритих угод; повертає капітал."""
        for inst, p in prices.items():
            if p <= 0:
                raise ValueError(f"mark price must be > 0 for {inst}, got {p}")
            self.marks[inst] = p
            pos = self.positions.get(inst)
            if pos is not None and pos.qty != 0:
                u = pos.qty * p - pos.cost_basis
                pos.max_adverse_excursion = min(pos.max_adverse_excursion, u)
        return self.equity

    # ------------------------------------------------------------ величини

    @property
    def cash(self) -> Decimal:
        """Спот-еквівалентні гроші: W₀ − Σ dq·price − Σ funding (комісії НЕ включені)."""
        return self._cash

    @property
    def wallet_balance(self) -> Decimal:
        """Баланс гаманця перпа: W₀ + realized − fees − funding."""
        return self.initial + self.realized_pnl - self.fees_paid - self.funding_paid

    @property
    def unrealized_pnl(self) -> Decimal:
        total = D0
        for inst, pos in self.positions.items():
            if pos.qty != 0:
                total += pos.qty * self._mark_of(inst) - pos.cost_basis
        return total

    @property
    def equity(self) -> Decimal:
        """Маржинальна форма: wallet_balance + unrealized."""
        return self.wallet_balance + self.unrealized_pnl

    @property
    def equity_cash_form(self) -> Decimal:
        """Касова форма брифінгу: cash + Σ q·p − Σ fees."""
        holdings = D0
        for inst, pos in self.positions.items():
            if pos.qty != 0:
                holdings += pos.qty * self._mark_of(inst)
        return self._cash + holdings - self.fees_paid

    def identity_residual(self) -> Decimal:
        """(cash + Σq·p − Σfees) − equity; має бути 0 (з точністю контексту Decimal)."""
        return self.equity_cash_form - self.equity

    @property
    def gross_exposure(self) -> Decimal:
        return sum((abs(p.qty) * self._mark_of(i) for i, p in self.positions.items() if p.qty != 0), D0)

    @property
    def net_exposure(self) -> Decimal:
        return sum((p.qty * self._mark_of(i) for i, p in self.positions.items() if p.qty != 0), D0)

    @property
    def leverage(self) -> Decimal | None:
        eq = self.equity
        return None if eq <= 0 else self.gross_exposure / eq

    def position_qty(self, instrument: str) -> Decimal:
        pos = self.positions.get(instrument)
        return D0 if pos is None else pos.qty

    def snapshot(self, ts_ns: int) -> PortfolioSnapshot:
        return PortfolioSnapshot(
            ts_ns=ts_ns, equity=self.equity, cash=self._cash, wallet_balance=self.wallet_balance,
            unrealized=self.unrealized_pnl, gross_exposure=self.gross_exposure,
            net_exposure=self.net_exposure, leverage=self.leverage, fees_paid=self.fees_paid,
            funding_paid=self.funding_paid, realized_pnl=self.realized_pnl,
        )

    # ------------------------------------------------------------ внутрішнє

    def _mark_of(self, inst: str) -> Decimal:
        m = self.marks.get(inst)
        if m is None:
            raise KeyError(f"no mark price for {inst!r}")
        return m

    @staticmethod
    def _start_trade(pos: Position, side: Side, ts_ns: int) -> None:
        pos.opened_at_ns = ts_ns
        pos.max_adverse_excursion = D0
        pos._t_side = side
        pos._t_max_qty = D0
        pos._t_realized = D0
        pos._t_fees = D0
        pos._t_funding = D0
        pos._t_entry_notional = D0
        pos._t_entry_qty = D0
        pos._t_exit_notional = D0
        pos._t_exit_qty = D0

    def _finish_trade(self, pos: Position, ts_ns: int, exit_reason: ExitReason | None) -> ClosedTrade:
        entry_px = pos._t_entry_notional / pos._t_entry_qty if pos._t_entry_qty > 0 else D0
        exit_px = pos._t_exit_notional / pos._t_exit_qty if pos._t_exit_qty > 0 else D0
        trade = ClosedTrade(
            instrument=pos.instrument, side=pos._t_side, qty=pos._t_max_qty,
            entry_price=entry_px, exit_price=exit_px, gross_pnl=pos._t_realized,
            fees=pos._t_fees, funding=pos._t_funding,
            pnl=pos._t_realized - pos._t_fees - pos._t_funding,
            entry_notional=pos._t_entry_notional, exit_notional=pos._t_exit_notional,
            opened_at_ns=pos.opened_at_ns if pos.opened_at_ns is not None else ts_ns,
            closed_at_ns=ts_ns, exit_reason=exit_reason,
            max_adverse_excursion=pos.max_adverse_excursion,
        )
        self.closed_trades.append(trade)
        pos.opened_at_ns = None
        pos._t_side = Side.FLAT
        return trade
