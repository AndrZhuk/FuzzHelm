"""Торговий цикл (один код для бектесту і live/replay) і подієвий рушій бектесту bar-close → open(t+1).

Найменування: backtest/engine.py
Призначення: TradingLoop — єдиний шлях «закрита свічка → ознаки → детектори → нечітке ядро → κ → u_final →
    сайзер → гістерезис → ризик-ланцюг + автомат станів + kill-switch → маршрутизатор → PaperBroker →
    облік» (брифінг §4.1); run_backtest — прогін цього циклу над Dataset з метриками і паспортом прогону.
Автор: Андрій Жук, 2026.

Часова дисципліна (§4.1, §5.14). Бар t закрито в момент close_t = open_t + tf − 1 мс. На кроці t:
  1) брокер виконує заявки, подані на закритті t−1, за open_t (+ модель витрат), потім стопи/TP/ліквідацію
     в межах [l_t, h_t] (песимістичний шлях), фандинг 00/08/16 UTC — на позицію до виконань;
  2) облік: фандинг, виконання, ціна ліквідації нової позиції (від фактичної ціни входу), оцінка за c_t;
  3) ознаки/детектори бачать лише бари ≤ t; σ-оцінка vol-target; автомат ризику бачить капітал E_t;
  4) рішення на закритті t → заявки з ts_created = close_t, які брокер виконає не раніше open_{t+1}.
Жодна величина кроку t не залежить від бару t+1: цикл отримує бари по одному і масивів не бачить.

М'яке пропонує, жорстке вирішує: ядро видає лише u_final; ціль позиції дає сайзер (× κ_mode автомата),
а дозволену ціль — RiskGuard (алгебра вердиктів: правила гейтять лише приріст експозиції).

Політики, яких брифінг не задає (розходження — docs/deviations.d/engine.md):
  * розмір фіксується на вході: поки гістерезис тримає бік позиції, заявок немає (без доторговування, яке
    лише спалювало б комісії на кожній зміні |u|); ризик-ланцюг викликається на НАМІР заявки;
  * стоп — reduce-only STOP_MARKET на P_ref ∓ Δ_stop (Δ_stop = χ·ATR сайзера, P_ref = c_t — ціна, за якою
    зроблено сайзинг), подається разом із заявкою входу; тейк-профіт — рівень P_ref ± m·Δ_stop,
    m = take_profit.multiple_of_stop (config/engine.yaml, джерело expert);
  * σ_base для σ_ann/σ_base автомата — медіана σ_ann на вікні прогріву (до першого рішення);
  * стан гістерезису перед кожним рішенням синхронізується з фактичною позицією (після стопа/TP/ліквідації,
    вето чи відмови брокера повторний вхід потребує |u| ≥ u_enter);
  * виходи: SIGNAL (гістерезис/розворот), STOP/TP/LIQUIDATION (брокер), HALT (flatten-all у HALTED або
    за сигналом halt ланцюга), RISK_VETO (розворот, новий бік якого ризик-ланцюг відхилив цілком — позиція
    закривається, але не перевертається).
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from functools import cached_property
from typing import Any, Literal
from uuid import UUID

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from fuzzhelm.backtest.dataset import BARS_PER_YEAR, TF_NS, Dataset, dec_bar_of
from fuzzhelm.backtest.manifest import RunManifest, build_manifest, canonicalize_config, config_hash
from fuzzhelm.backtest.metrics import compute_metrics, moments, psr, returns_from_equity, sharpe_ratio
from fuzzhelm.config import load_yaml
from fuzzhelm.core.clock import NS_PER_MIN, ManualClock, SeededIdGenerator
from fuzzhelm.core.digest import canonical_json
from fuzzhelm.core.dto import Candle, Fill, Instrument, OrderAck, OrderRequest
from fuzzhelm.core.enums import ExitReason, OrderStatus, OrderType, RejectCode, RiskState, Role, RunKind, Side
from fuzzhelm.core.errors import ConfigValidationError
from fuzzhelm.core.journal import EventJournal, JournalEntry
from fuzzhelm.core.money import D0, D1, dec, floor_qty, quantize_price
from fuzzhelm.core.ports import IdGenerator
from fuzzhelm.decision.core import DecisionCore
from fuzzhelm.decision.trace import DecisionTrace
from fuzzhelm.detectors.registry import DETECTOR_NAMES, MIN_WINDOW_CAPACITY, build_detectors, max_warmup
from fuzzhelm.execution.cost_model import CostModel, FundingCharge
from fuzzhelm.execution.paper_broker import DecBar, PaperBroker, dec_bar_from_candle
from fuzzhelm.execution.portfolio import ClosedTrade, Portfolio
from fuzzhelm.execution.router import OrderRouter
from fuzzhelm.features.convert import Bar, bar_from_candle, to_float
from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline
from fuzzhelm.features.window import BarWindow
from fuzzhelm.fuzzy.base import InferenceEngine
from fuzzhelm.fuzzy.linear import LinearVoteEngine
from fuzzhelm.fuzzy.mamdani import MamdaniEngine
from fuzzhelm.fuzzy.membership import load_membership
from fuzzhelm.fuzzy.rules import load_rulebase
from fuzzhelm.risk.config import RiskConfig, load_risk_config
from fuzzhelm.risk.context import RiskContext
from fuzzhelm.risk.guard import GuardResult, RiskGuard
from fuzzhelm.risk.journal import AuditSink, RiskEventRecord, RiskJournal
from fuzzhelm.risk.margin import liq_price, side_liq_price
from fuzzhelm.risk.state import RiskObservation, RiskStateMachine, Transition
from fuzzhelm.sizing.convert import float_to_decimal_exact, to_decimal
from fuzzhelm.sizing.hysteresis import HysteresisGate
from fuzzhelm.sizing.sizer import PositionSizer, SizingInput, SizingParams, SizingResult
from fuzzhelm.sizing.vol_target import VolTarget

RECORD_MODES: tuple[str, ...] = ("all", "trades", "none")
ENGINE_KINDS: tuple[str, ...] = ("mamdani", "linear")
COST_MODES: tuple[str, ...] = ("zero", "sqrt_impact", "full")
GRID_KEYS: tuple[str, ...] = ("n_atr", "chi", "u_enter", "rho_base", "lam")
TREE_FILES: dict[str, str] = {
    "engine": "engine", "risk_limits": "risk_limits", "detectors": "detectors",
    "cost_model": "cost_model", "membership": "membership", "rules": "rules_mamdani",
}
_NON_IDENTITY = ("record_traces", "check_invariants", "invariant_tolerance")
_FINAL_STATUSES = frozenset({OrderStatus.FILLED, OrderStatus.REJECTED, OrderStatus.CANCELED})


class EngineInvariantError(AssertionError):
    """Порушено інваріант рушія: тотожність обліку, узгодженість брокер↔облік, HALTED без нової експозиції."""


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def _dec(x: Any) -> Decimal:
    """YAML-число/рядок → Decimal (float — через найкоротший repr, як у pydantic-схемах конфігурацій)."""
    if isinstance(x, float):
        return float_to_decimal_exact(x)
    return dec(x)


def _f(x: Any) -> float:
    """YAML-число (int | float) → float без float(<expr>) (межа типів пакета backtest)."""
    if isinstance(x, bool) or not isinstance(x, int | float):
        raise TypeError(f"expected a number, got {type(x).__name__}")
    return x + 0.0


# ====================================================================== конфігурація


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class _TakeProfitCfg(_Strict):
    multiple_of_stop: float = Field(2.0, gt=0)


class _SigmaBaseCfg(_Strict):
    method: Literal["warmup_median"] = "warmup_median"     # єдиний реалізований спосіб (ENG-02)


class _WarmupCfg(_Strict):
    bars: int | None = Field(None, ge=1)


class _FundingCfg(_Strict):
    fallback_rate: float | str = 0.0001
    match_window_s: float = Field(60, gt=0)


class _LinearCfg(_Strict):
    weights: dict[str, float]


class EngineTreeCfg(_Strict):
    """Схема config/engine.yaml (невідомий ключ або значення поза доменом → ConfigValidationError)."""

    version: int = 1
    engine: Literal["mamdani", "linear"] = "mamdani"
    cost_mode: Literal["zero", "sqrt_impact", "full"] | None = None
    initial_equity: float | str = "10000"
    take_profit: _TakeProfitCfg = _TakeProfitCfg()
    sigma_base: _SigmaBaseCfg = _SigmaBaseCfg()
    warmup: _WarmupCfg = _WarmupCfg()
    funding: _FundingCfg = _FundingCfg()
    linear: _LinearCfg | None = None
    record_traces: Literal["all", "trades", "none"] = "trades"
    check_invariants: bool = False
    invariant_tolerance: float | str = "1E-9"


def validate_engine_tree(tree: Mapping[str, Any]) -> EngineTreeCfg:
    try:
        return EngineTreeCfg.model_validate(dict(tree))
    except ValidationError as e:
        err = e.errors()[0]
        path = "engine." + ".".join(str(x) for x in err["loc"]) if err["loc"] else "engine"
        raise ConfigValidationError(err["msg"], path=path) from e


@dataclass(frozen=True)
class BacktestConfig:
    """Параметри прогону. None у скалярному полі → значення з конфігураційних дерев (`trees`).

    `trees` — розібрані YAML (engine, risk_limits, detectors, cost_model, membership, rules); відсутні
    дерева читаються з config/ у __post_init__, тож конфіг герметичний: воркер grid отримує його як dict
    (to_dict) і не читає файлів. Параметри сітки (n_atr, χ, u_enter, ρ_base, λ) і перемикачі експерименту
    (рушій, модель витрат, підмножина/ваги детекторів) перекривають відповідні місця дерев
    (`resolved_trees`); саме перекриті дерева входять у config_hash.
    """

    engine: str | None = None
    cost_mode: str | None = None
    initial_equity: Decimal | None = None
    n_atr: int | None = None
    chi: float | None = None
    u_enter: float | None = None
    u_exit: float | None = None
    rho_base: float | None = None
    lam: float | None = None
    tp_multiple: float | None = None
    detectors: tuple[str, ...] | None = None
    detector_weights: tuple[tuple[str, float], ...] = ()
    linear_weights: tuple[tuple[str, float], ...] | None = None
    warmup_bars: int | None = None
    funding_fallback_rate: Decimal | None = None
    funding_match_ns: int | None = None
    record_traces: str | None = None
    check_invariants: bool | None = None
    invariant_tolerance: Decimal | None = None
    trees: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        trees = dict(self.trees)
        for key, fname in TREE_FILES.items():
            if key not in trees:
                trees[key] = load_yaml(fname)
        s = object.__setattr__
        s(self, "trees", trees)
        eng = trees["engine"]
        validate_engine_tree(eng)
        risk = trees["risk_limits"]
        sizing = risk.get("sizing", {})
        hyst = risk.get("hysteresis", {})
        feats = trees["detectors"].get("features", {}) or {}

        def pick(name: str, value: Any) -> None:
            if getattr(self, name) is None:
                s(self, name, value)

        pick("engine", str(eng.get("engine", "mamdani")))
        pick("cost_mode", str(eng.get("cost_mode") or trees["cost_model"].get("mode", "full")))
        pick("initial_equity", _dec(eng.get("initial_equity", "10000")))
        pick("n_atr", int(feats.get("n_atr", 14)))
        pick("chi", _f(sizing.get("chi_atr", 2.0)))
        pick("rho_base", _f(sizing.get("rho_base", 0.005)))
        pick("lam", _f(sizing.get("ewma_lambda", 0.94)))
        pick("u_enter", _f(hyst.get("enter", 0.25)))
        pick("u_exit", _f(hyst.get("exit", 0.12)))
        pick("tp_multiple", _f((eng.get("take_profit") or {}).get("multiple_of_stop", 2.0)))
        lw = (eng.get("linear") or {}).get("weights", {"T": 0.5, "R": 0.5})
        pick("linear_weights", tuple(sorted((str(k), _f(v)) for k, v in lw.items())))
        warm = (eng.get("warmup") or {}).get("bars")
        if self.warmup_bars is None and warm is not None:
            s(self, "warmup_bars", int(warm))
        fund = eng.get("funding") or {}
        pick("funding_fallback_rate", _dec(fund.get("fallback_rate", "0.0001")))
        pick("funding_match_ns", int(_f(fund.get("match_window_s", 60)) * 1_000_000_000))
        pick("record_traces", str(eng.get("record_traces", "trades")))
        pick("check_invariants", bool(eng.get("check_invariants", False)))
        pick("invariant_tolerance", _dec(str(eng.get("invariant_tolerance", "1E-9"))))
        if self.engine not in ENGINE_KINDS:
            raise ValueError(f"engine must be one of {ENGINE_KINDS}, got {self.engine!r}")
        if self.cost_mode not in COST_MODES:
            raise ValueError(f"cost_mode must be one of {COST_MODES}, got {self.cost_mode!r}")
        if self.record_traces not in RECORD_MODES:
            raise ValueError(f"record_traces must be one of {RECORD_MODES}, got {self.record_traces!r}")
        if self.tp_multiple is not None and not self.tp_multiple > 0:
            raise ValueError("tp_multiple must be > 0")
        if self.initial_equity is not None and not self.initial_equity > 0:
            raise ValueError("initial_equity must be > 0")
        known = set(DETECTOR_NAMES)
        if self.detectors is not None:
            bad = set(self.detectors) - known
            if bad or not self.detectors:
                raise ValueError(f"detector subset must be a non-empty subset of {DETECTOR_NAMES}, got {bad}")
        bad_w = {k for k, _ in self.detector_weights} - known
        if bad_w:
            raise ValueError(f"unknown detectors in weight overrides: {sorted(bad_w)}")

    # ------------------------------------------------------------------ фабрики

    @classmethod
    def from_profile(cls, profile: str | Mapping[str, Any] | None = None, **overrides: Any) -> BacktestConfig:
        """Ключі рушія з config/profiles/<profile>.yaml (engine, initial_equity, cost_mode, record_traces,
        check_invariants) + явні перекриття."""
        prof: Mapping[str, Any] = {}
        if isinstance(profile, str):
            prof = load_yaml(f"profiles/{profile}")
        elif profile is not None:
            prof = profile
        kw: dict[str, Any] = {}
        for key in ("engine", "cost_mode", "record_traces", "check_invariants"):
            if prof.get(key) is not None:
                kw[key] = prof[key]
        if prof.get("initial_equity") is not None:
            kw["initial_equity"] = _dec(prof["initial_equity"])
        kw.update(overrides)
        return cls(**kw)

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> BacktestConfig:
        kw = dict(d)
        for k in ("initial_equity", "funding_fallback_rate", "invariant_tolerance"):
            if kw.get(k) is not None:
                kw[k] = dec(str(kw[k]))
        if kw.get("detectors") is not None:
            kw["detectors"] = tuple(kw["detectors"])
        for k in ("detector_weights", "linear_weights"):
            if isinstance(kw.get(k), Mapping):
                kw[k] = tuple(sorted((str(a), _f(b)) for a, b in kw[k].items()))
        return cls(**kw)

    def with_params(self, **params: Any) -> BacktestConfig:
        """Клітинка сітки backtest.grid (n_atr, chi, u_enter, rho_base, lam) або будь-які інші поля."""
        return dataclasses.replace(self, **params)

    def to_dict(self) -> dict[str, Any]:
        """Плаский словник (pickle/JSON): скаляри + сирі дерева; from_dict(to_dict()) == self."""
        out: dict[str, Any] = {f.name: getattr(self, f.name) for f in dataclasses.fields(self)}
        for k in ("initial_equity", "funding_fallback_rate", "invariant_tolerance"):
            out[k] = str(out[k])
        out["detectors"] = None if self.detectors is None else list(self.detectors)
        out["detector_weights"] = dict(self.detector_weights)
        out["linear_weights"] = dict(self.linear_weights or ())
        out["trees"] = copy.deepcopy(dict(self.trees))
        return out

    def identity_dict(self) -> dict[str, Any]:
        """Те, що визначає результат (вхід config_hash): без перемикачів запису; дерева перекриті."""
        d = self.to_dict()
        for k in _NON_IDENTITY:
            d.pop(k)
        trees = self.resolved_trees()
        d["trees"] = {k: v for k, v in trees.items() if k != "engine"}
        return d

    @cached_property
    def config_hash(self) -> str:
        return config_hash(self.identity_dict())

    def resolved_trees(self) -> dict[str, Any]:
        t = copy.deepcopy(dict(self.trees))
        det = t["detectors"]
        det.setdefault("features", {})
        det["features"] = {**(det["features"] or {}), "n_atr": self.n_atr}
        for name, w in self.detector_weights:
            det["detectors"][name] = {**det["detectors"][name], "weight": w}
        risk = t["risk_limits"]
        risk["sizing"] = {**(risk.get("sizing") or {}), "rho_base": self.rho_base, "chi_atr": self.chi,
                          "ewma_lambda": self.lam}
        risk["hysteresis"] = {**(risk.get("hysteresis") or {}), "enter": self.u_enter, "exit": self.u_exit}
        t["cost_model"] = {**t["cost_model"], "mode": self.cost_mode}
        return t

    # ------------------------------------------------------------------ збирання компонентів

    def risk_config(self) -> RiskConfig:
        return load_risk_config(self.resolved_trees()["risk_limits"])

    def feature_params(self) -> FeatureParams:
        return FeatureParams.from_config(self.resolved_trees()["detectors"])

    def build_engine(self) -> InferenceEngine:
        if self.engine == "linear":
            return LinearVoteEngine(dict(self.linear_weights or ()))
        return _mamdani(canonical_json(canonicalize_config([self.trees["membership"], self.trees["rules"]])),
                        self.trees["membership"], self.trees["rules"])

    def resolved_warmup(self) -> int:
        """Барів до першого рішення: повний прогрів ознак і детекторів (vol_rank: 24 + 500 − 1 = 523)."""
        if self.warmup_bars is not None:
            return max(1, self.warmup_bars)
        det_cfg = self.resolved_trees()["detectors"]
        return max(FeatureParams.from_config(det_cfg).max_lookback, max_warmup(build_detectors(det_cfg)))


_ENGINE_CACHE: dict[bytes, MamdaniEngine] = {}
_ENGINE_CACHE_SIZE = 16     # різних баз правил у процесі (API з CRUD стратегій живе довго)


def _mamdani(key: bytes, membership: Mapping[str, Any], rules: Mapping[str, Any]) -> MamdaniEngine:
    """Рушій Мамдані за вмістом дерев (кеш у процесі: 45 правил компілюються раз на воркер).

    MamdaniEngine не має стану між викликами, тож спільний екземпляр не зв'язує прогони між собою.
    """
    eng = _ENGINE_CACHE.get(key)
    if eng is None:
        mem = load_membership(dict(membership))
        eng = MamdaniEngine(mem, load_rulebase(dict(rules), mem, production=True))
        if len(_ENGINE_CACHE) >= _ENGINE_CACHE_SIZE:
            _ENGINE_CACHE.pop(next(iter(_ENGINE_CACHE)))
        _ENGINE_CACHE[key] = eng
    return eng


# ====================================================================== записи (форма рядків DDL)


@dataclass(frozen=True, slots=True)
class DecisionEntry:
    """Рішення на закритті бару → рядок `decision` (trace — джерело /explain; None у режимі none)."""

    open_time_ns: int
    decided_at_ns: int                    # close_t (ts_created заявок)
    u_raw: float
    kappa: float
    u_final: float
    gate_side: int                        # стан тригера Шмітта після рішення
    current_qty: Decimal                  # позиція на момент рішення (зі знаком)
    requested_qty: Decimal | None         # ціль сайзера зі знаком (None — ризик не оцінювався)
    target_side: int
    target_qty: Decimal                   # дозволена ціль за модулем
    binding_constraint: str | None
    stop_price: Decimal | None
    tp_price: Decimal | None
    liq_price: Decimal | None             # оцінка P_liq дозволеної цілі (крос-маржа: E_t, P_ref)
    action: str                           # hold | enter | exit | flip | flatten | none
    verdict: str | None                   # ALLOW | SHRINK | VETO (композиція ланцюга) або None
    order_ids: tuple[UUID, ...]
    trace: DecisionTrace | None = None


@dataclass(slots=True)
class OrderRecord:
    """Рядок `sim_order`: заявка + її поточний стан (оновлюється виконаннями)."""

    request: OrderRequest
    decision_ns: int | None               # open_time рішення, що породило заявку (FK decision)
    role: str                             # entry | exit | flip | flatten | stop | tp | liquidation
    status: OrderStatus
    reject_code: str | None
    venue_order_id: str | None
    ts_created_ns: int
    filled_qty: Decimal = D0
    avg_fill_price: Decimal | None = None
    fee: Decimal = D0
    slippage_bps: Decimal | None = None
    liquidity: str | None = None
    ts_filled_ns: int | None = None
    intent_reason: ExitReason | None = None


@dataclass(slots=True)
class PositionRecord:
    """Рядок `position`: від відкриття з нуля до закриття (або розвороту)."""

    instrument: str
    side: int
    opened_at_ns: int
    opening_decision_ns: int | None
    avg_entry: Decimal
    qty: Decimal                          # максимальний |q| угоди
    leverage: Decimal | None
    allocated_margin: Decimal | None      # номінал / L_set (початкова маржа)
    stop_price: Decimal | None
    tp_price: Decimal | None
    liq_price: Decimal | None
    closed_at_ns: int | None = None
    exit_reason: ExitReason | None = None
    realized_pnl: Decimal | None = None   # валовий PnL − комісії (нетто = realized_pnl − funding_paid)
    funding_paid: Decimal | None = None
    fees: Decimal | None = None
    max_adverse_excursion: Decimal | None = None
    exit_price: Decimal | None = None


@dataclass(frozen=True, slots=True)
class EquityPointRecord:
    """Рядок `equity_point` (kappa = κ_mode автомата; VaR/CVaR — звітна метрика, рахується окремо)."""

    ts_ns: int
    equity: Decimal
    cash: Decimal
    unrealized: Decimal
    gross_exposure: Decimal
    leverage: Decimal | None
    drawdown: Decimal
    risk_state: RiskState
    kappa: Decimal
    position_qty: Decimal
    var95: Decimal | None = None
    cvar95: Decimal | None = None


@dataclass(frozen=True, slots=True)
class StepResult:
    """Усе, що породив один бар (live-воркер пише це в БД 1:1)."""

    index: int
    open_time_ns: int
    close_time_ns: int
    equity: Decimal
    position_qty: Decimal
    risk_state: RiskState
    kappa_mode: Decimal
    decided: bool
    fills: tuple[Fill, ...]
    funding: tuple[FundingCharge, ...]
    decision: DecisionEntry | None
    orders: tuple[OrderRecord, ...]
    opened_positions: tuple[PositionRecord, ...]
    closed_positions: tuple[PositionRecord, ...]
    risk_events: tuple[RiskEventRecord, ...]
    transition: Transition | None
    equity_point: EquityPointRecord | None
    # заявки попередніх кроків, стан яких змінився на цьому барі (виконання, скасування стопа OCO/«сироти»);
    # нові заявки цього кроку — в `orders` (уже з поточним станом)
    order_updates: tuple[OrderRecord, ...] = ()


# ====================================================================== цикл


@dataclass(slots=True)
class _Entry:
    decision_ns: int
    stop: Decimal | None
    tp: Decimal | None


class TradingLoop:
    """Детермінований покроковий торговий цикл одного інструмента (стан між барами — лише тут).

    Порти ін'єктуються: `clock` (ManualClock, що йде за часом барів), `ids` (SeededIdGenerator(seed)),
    `venue` (PaperBroker; для нього рушій також задає TP, ціну ліквідації і ставку фандингу).
    `keep_records=False` — записи лише в StepResult (live-воркер), інакше ще й накопичуються (бектест).
    `trade_start` — індекс бару, з якого дозволені рішення (≥ прогріву): вікно оцінки OOS у walk-forward.
    """

    def __init__(self, instrument: Instrument, cfg: BacktestConfig | None = None, *, seed: int,
                 tf: str = "1m", clock: ManualClock | None = None, ids: IdGenerator | None = None,
                 venue: PaperBroker | None = None, keep_records: bool = True, trade_start: int = 0,
                 run_id: UUID | None = None, event_journal: EventJournal | None = None,
                 audit_sink: AuditSink | None = None) -> None:
        if tf not in TF_NS:
            raise ValueError(f"unsupported timeframe {tf!r}")
        self.cfg = cfg = cfg or BacktestConfig()
        self.instrument = instrument
        self.seed = seed
        self.tf = tf
        self._sym = instrument.symbol_canon
        self._tick = instrument.tick_size
        self._step_size = instrument.step_size
        self._min_notional = instrument.min_notional
        self._lev_setting = dec(instrument.max_leverage)
        self._record = cfg.record_traces or "trades"
        self._keep = keep_records
        self._check = bool(cfg.check_invariants)
        self._tol = cfg.invariant_tolerance or Decimal("1E-9")
        self._clock = clock or ManualClock(0)
        self._ids: IdGenerator = ids or SeededIdGenerator(seed, b"fuzzhelm.engine")
        self._ej = event_journal

        trees = cfg.resolved_trees()
        det_cfg = trees["detectors"]
        self._fp = FeatureParams.from_config(det_cfg)
        self._pipe = FeaturePipeline(self._fp)
        self._window = BarWindow(max(MIN_WINDOW_CAPACITY, 8))
        detectors = build_detectors(det_cfg)
        if cfg.detectors is not None:
            keep = set(cfg.detectors)
            detectors = [d for d in detectors if d.name in keep]
        self.core = DecisionCore.from_config(detectors, cfg.build_engine(), det_cfg)
        self.warmup_bars = cfg.resolved_warmup()
        self._decide_from = max(self.warmup_bars - 1, trade_start)

        risk_cfg = load_risk_config(trees["risk_limits"])
        self.risk_cfg = risk_cfg
        self._risk_tree: dict[str, Any] = copy.deepcopy(dict(trees["risk_limits"]))
        self._vt = VolTarget.from_config(risk_cfg.sizing)
        self._sizer = PositionSizer(SizingParams.from_config(risk_cfg.sizing))
        self._gate = HysteresisGate(risk_cfg.hysteresis.enter, risk_cfg.hysteresis.exit)
        # записи ризик-ланцюга будуються лише на НАМІР заявки (≈ 7 на рішення з заявкою) — у всіх режимах
        self._step_risk: list[RiskEventRecord] = []
        self.journal = RiskJournal(run_id, sink=self._step_risk.append, event_journal=event_journal,
                                   keep=False)
        self._step_transition: Transition | None = None
        self.fsm = RiskStateMachine(risk_cfg.state_machine, clock=self._clock,
                                    on_transition=self._on_transition, on_audit=audit_sink)
        self.guard = RiskGuard.from_config(risk_cfg, self.journal, killswitch=self.fsm.killswitch)
        self.cost_model = CostModel.from_config(trees["cost_model"], seed=seed)
        self.broker = venue or PaperBroker(self.cost_model, [instrument], self._ids, self._clock)
        self.router = OrderRouter(self.broker, self._clock)
        self._funding_fallback = cfg.funding_fallback_rate or Decimal("0.0001")
        self.broker.set_funding_rate(self._sym, self._funding_fallback)   # без ряду ставок — fallback
        self.portfolio = Portfolio(cfg.initial_equity or Decimal("10000"))

        # стан між барами
        self._index = -1
        self._prev_close: float | None = None
        self._sigma_ann = math.nan
        self._sigma_hist: list[float] = []
        self.sigma_base: float | None = None
        self._funding_t: list[int] = []
        self._funding_r: list[Decimal] = []
        self._funding_ptr = 0
        self._last_open: int | None = None
        self._orders: dict[UUID, OrderRecord] = {}
        self._open_orders: dict[UUID, OrderRecord] = {}    # ще не фінальні (NEW/PARTIAL) — для order_updates
        self._touched: set[UUID] = set()                    # заявки, які виконувались на цьому барі
        self._reason: dict[UUID, ExitReason] = {}
        self._entries: dict[UUID, _Entry] = {}
        self._open_pos: PositionRecord | None = None
        self._halt_index: int | None = None

        # накопичувачі (бектест)
        self.equity: list[Decimal] = []
        self.equity_ts: list[int] = []
        self.position_series: list[Decimal] = []
        self.equity_points: list[EquityPointRecord] = []
        self.decisions: list[DecisionEntry] = []
        self.orders: list[OrderRecord] = []
        self.fills: list[Fill] = []
        self.funding: list[FundingCharge] = []
        self.positions: list[PositionRecord] = []
        self.risk_events: list[RiskEventRecord] = []
        self.transitions: list[Transition] = []

    # ------------------------------------------------------------------ властивості

    @property
    def index(self) -> int:
        """Індекс останнього обробленого бару (−1 до першого)."""
        return self._index

    @property
    def decide_from(self) -> int:
        """Перший індекс бару, на закритті якого рушій приймає рішення."""
        return self._decide_from

    @property
    def open_position(self) -> PositionRecord | None:
        return self._open_pos

    @property
    def window(self) -> BarWindow:
        """Вікно барів і ознак, на якому приймається рішення (лише читання: для /explain і перевірок)."""
        return self._window

    @property
    def halted_at(self) -> int | None:
        """Індекс бару, на якому автомат уперше увійшов у HALTED (None — не входив)."""
        return self._halt_index

    # ------------------------------------------------------------------ входи

    def set_funding_series(self, t_ns: Sequence[int] | np.ndarray | None,
                           rates: Sequence[float] | np.ndarray | None) -> None:
        """Історичні ставки фандингу (час фіксації біржею, ставка); без ряду — fallback з engine.yaml."""
        if t_ns is None or rates is None:
            self._funding_t, self._funding_r = [], []
            return
        ts = [x.item() if isinstance(x, np.generic) else int(x) for x in t_ns]
        rs = [float_to_decimal_exact(x.item() if isinstance(x, np.generic) else x) for x in rates]
        if len(ts) != len(rs):
            raise ValueError("funding times and rates differ in length")
        self._funding_t, self._funding_r, self._funding_ptr = ts, rs, 0

    def on_candle(self, candle: Candle, *, dq_score: Decimal = D1,
                  last_data_ns: int | None = None) -> StepResult:
        """Live/replay: закрита свічка DTO → крок (той самий код, що й у бектесті)."""
        if not candle.is_closed:
            raise ValueError("TradingLoop consumes closed candles only")
        if candle.instrument != self._sym:
            raise ValueError(f"candle for {candle.instrument!r}, loop trades {self._sym!r}")
        return self.step(bar_from_candle(candle), candle.close_time_ns, dbar=dec_bar_from_candle(candle),
                         dq_score=dq_score, last_data_ns=last_data_ns)

    def release_halt(self, actor_role: Role | str, *, actor: str | None = None) -> Transition | None:
        """Ручне зняття HALTED (лише admin; інакше PermissionDeniedError) — делегується автомату.

        Зняття відбувається МІЖ барами (команда адміністратора через API → воркер), тож його запис
        risk_event (HALTED → COOLDOWN) не губиться: він лишається «позакроковим» і потрапляє або в
        `take_pending_risk_events()` (воркер пише його одразу), або в StepResult наступного бару.
        """
        tr = self.fsm.release(actor_role, actor=actor)
        if tr is not None and self._keep:
            self.transitions.append(tr)
        return tr

    def take_pending_risk_events(self) -> tuple[RiskEventRecord, ...]:
        """Забрати записи risk_event, що з'явились поза кроком (зняття HALTED між барами)."""
        out = tuple(self._step_risk)
        self._step_risk.clear()
        if self._keep:
            self.risk_events.extend(out)
        return out

    def apply_risk_limits(self, tree: Mapping[str, Any]) -> RiskConfig:
        """Гаряча заміна лімітів (`limits`) і порогів автомата (`state_machine`) з config/risk_limits.yaml.

        PUT /risk/limits → NOTIFY fuzzhelm_control → воркер. Секції `sizing` і `hysteresis` — параметри
        стратегії прогону (входять у config_hash, їх перекривають поля BacktestConfig) і посеред прогону
        не змінюються: для них потрібен новий прогін. Стан автомата (режим, пік, dwell) і kill-switch
        зберігаються; нові пороги діють з наступного бару. Невалідне дерево → ConfigValidationError, стан
        циклу при цьому не змінюється.
        """
        merged = {**self._risk_tree, "limits": tree["limits"], "state_machine": tree["state_machine"]}
        new = load_risk_config(merged)
        self._risk_tree = copy.deepcopy(merged)
        self.risk_cfg = new
        self.guard = RiskGuard.from_config(new, self.journal, killswitch=self.fsm.killswitch)
        self.fsm.cfg = new.state_machine
        return new

    # ------------------------------------------------------------------ крок

    def step(self, bar: Bar, close_ns: int, *, dbar: DecBar | None = None, dq_score: Decimal = D1,
             last_data_ns: int | None = None) -> StepResult:
        """Обробити закритий бар t: виконання заявок t−1 → облік → ознаки → ризик-стан → рішення."""
        i = self._index + 1
        sym = self._sym
        pf = self.portfolio
        t_open = bar.t_ns
        if dbar is None:
            dbar = dec_bar_of(bar, self.instrument)
        self._clock.set(t_open)
        if self._funding_t and self._last_open is not None:
            self._apply_funding_rate(t_open, close_ns)
        # записи ризику, що з'явились між барами (зняття HALTED) і не забрані take_pending_risk_events(),
        # ідуть у StepResult цього бару; крокові записи накопичуються далі в тому самому списку
        self._step_transition = None
        # засувка (HALTED або kill-switch, зокрема спрацьований між барами оператором чи API): заявки на
        # вхід/розворот, що чекають open_t, не виконуються — HALTED ⇒ жодної нової експозиції (ENG-20)
        latched = self.fsm.state is RiskState.HALTED or self.fsm.killswitch.is_tripped
        if latched and self._open_orders:
            self._cancel_pending_increases()
        exposure_before = abs(pf.position_qty(sym))

        # 1) виконання: MARKET t−1 → open_t; стопи/TP/ліквідація в межах бару; фандинг
        fills = self.router.on_bar(dbar)  # type: ignore[arg-type]  # DecBar — BarLike брокера
        charges = self.broker.drain_funding()
        for ch in charges:
            pf.apply_funding(ch)
        opened: list[PositionRecord] = []
        closed: list[PositionRecord] = []
        new_orders: list[OrderRecord] = []
        self._touched.clear()
        for f in fills:
            self._apply_fill(f, opened, closed, new_orders)
        order_updates = self._sync_open_orders() if self._open_orders else ()
        if (fills or charges) and pf.position_qty(sym) != 0:
            self._refresh_liquidation()

        # 2) оцінка за закриттям t
        self._clock.set(close_ns)
        equity = pf.mark({sym: dbar.c})
        if self._check:
            self._check_accounting(latched=latched, exposure_before=exposure_before)

        # 3) ознаки (лише бари ≤ t), σ-оцінка, автомат ризику
        self._window.append(bar, self._pipe.update(bar))
        if self._prev_close is not None:
            self._sigma_ann, _, _ = self._vt.update(bar.c / self._prev_close - 1.0)
        self._prev_close = bar.c
        if self.sigma_base is None:
            if i < self._decide_from:
                if math.isfinite(self._sigma_ann):
                    self._sigma_hist.append(self._sigma_ann)
            else:
                # σ_base — медіана σ_ann на прогріві: «нормальна» волатильність до першого рішення
                self.sigma_base = np.median(self._sigma_hist).item() if self._sigma_hist else None
                self._sigma_hist = []
        sb = self.sigma_base
        vol_ratio = (self._sigma_ann / sb if sb is not None and sb > 0.0 and math.isfinite(self._sigma_ann)
                     else None)
        self.fsm.update(RiskObservation(close_ns, equity, vol_ratio))
        if self._halt_index is None and self.fsm.state is RiskState.HALTED:
            self._halt_index = i

        # 4) рішення на закритті t
        decision: DecisionEntry | None = None
        decided = i >= self._decide_from
        if decided:
            decision = self._decide(bar, dbar, close_ns, equity=equity, dq_score=dq_score,
                                    last_data_ns=last_data_ns, new_orders=new_orders)

        # 5) записи
        self._index = i
        self._last_open = t_open
        pos_qty = pf.position_qty(sym)
        state = self.fsm.state
        kappa_mode = self.fsm.kappa_mode
        point: EquityPointRecord | None = None
        if self._record != "none":
            snap = self.fsm.snapshot
            point = EquityPointRecord(
                ts_ns=close_ns, equity=equity, cash=pf.cash, unrealized=pf.unrealized_pnl,
                gross_exposure=pf.gross_exposure, leverage=pf.leverage,
                drawdown=snap.drawdown if snap is not None else D0, risk_state=state, kappa=kappa_mode,
                position_qty=pos_qty,
            )
        risk_events = tuple(self._step_risk)
        self._step_risk.clear()
        if self._keep:
            self.equity.append(equity)
            self.equity_ts.append(close_ns)
            self.position_series.append(pos_qty)
            if point is not None:
                self.equity_points.append(point)
            if decision is not None and (self._record == "all" or decision.order_ids):
                self.decisions.append(decision)
            self.orders.extend(new_orders)
            self.fills.extend(fills)
            self.funding.extend(charges)
            self.positions.extend(closed)
            self.risk_events.extend(risk_events)
            if self._step_transition is not None:
                self.transitions.append(self._step_transition)
        return StepResult(
            index=i, open_time_ns=t_open, close_time_ns=close_ns, equity=equity, position_qty=pos_qty,
            risk_state=state, kappa_mode=kappa_mode, decided=decided, fills=tuple(fills),
            funding=tuple(charges), decision=decision, orders=tuple(new_orders),
            opened_positions=tuple(opened),
            closed_positions=tuple(closed), risk_events=risk_events, transition=self._step_transition,
            equity_point=point, order_updates=order_updates,
        )

    # ------------------------------------------------------------------ виконання й облік

    def _apply_funding_rate(self, t_open: int, close_ns: int) -> None:
        """Ставка для моментів фандингу в (попередній open, open_t]: запис біржі, зафіксований у межах
        match-вікна від моменту (fundingTime Binance має мілісекундний «хвіст»); інакше — fallback.
        Запис пізніший за close_t не береться: на кроці t відомі лише дані до закриття бару t."""
        assert self._last_open is not None
        moments = self.cost_model.funding_times(self._last_open, t_open)
        if not moments:
            return
        m = moments[-1]
        horizon = min(m + (self.cfg.funding_match_ns or NS_PER_MIN), close_ns)
        ts, rs = self._funding_t, self._funding_r
        p = self._funding_ptr
        while p < len(ts) and ts[p] <= horizon:
            p += 1
        self._funding_ptr = p
        rate = self._funding_fallback
        if p > 0 and ts[p - 1] > m - (self.cfg.funding_match_ns or NS_PER_MIN):
            rate = rs[p - 1]
        self.broker.set_funding_rate(self._sym, rate)

    def _order_record_for_fill(self, f: Fill) -> OrderRecord:
        rec = self._orders.get(f.client_order_id)
        if rec is not None:
            return rec
        # синтетична заявка брокера (TP / ліквідація): рядок sim_order прив'язується до рішення,
        # що відкрило позицію (decision_id NOT NULL, ST-01)
        req = self.broker.order_request(f.client_order_id)
        if req is None:  # pragma: no cover — брокер завжди знає свої заявки
            raise EngineInvariantError(f"fill for unknown order {f.client_order_id}")
        role = (req.decision_ref or "tp").lower()
        pos = self._open_pos
        rec = OrderRecord(request=req, decision_ns=None if pos is None else pos.opening_decision_ns,
                          role=role, status=OrderStatus.NEW, reject_code=None,
                          venue_order_id=f.venue_order_id,
                          ts_created_ns=req.ts_created_ns)
        self._orders[f.client_order_id] = rec
        return rec

    def _apply_fill(self, f: Fill, opened: list[PositionRecord], closed: list[PositionRecord],
                    new_orders: list[OrderRecord]) -> None:
        pf = self.portfolio
        sym = self._sym
        known = f.client_order_id in self._orders
        rec = self._order_record_for_fill(f)
        self._touched.add(f.client_order_id)
        if not known:
            new_orders.append(rec)
        # sim_order: накопичення виконань (як OrderRepo.apply_fill)
        q_new = rec.filled_qty + f.qty
        rec.avg_fill_price = f.price if rec.avg_fill_price is None else (
            (rec.avg_fill_price * rec.filled_qty + f.price * f.qty) / q_new)
        rec.filled_qty = q_new
        rec.fee += f.fee
        rec.slippage_bps = f.slippage_bps
        rec.liquidity = f.liquidity.value
        rec.ts_filled_ns = f.ts_fill_ns
        rec.status = OrderStatus.FILLED if q_new >= rec.request.qty else OrderStatus.PARTIAL
        reason = self.broker.exit_reason(f.client_order_id) or self._reason.get(f.client_order_id)
        before = pf.position_qty(sym)
        trades = pf.apply_fill(f, reason)
        after = pf.position_qty(sym)
        for ct in trades:
            closed.append(self._close_position(ct, f))
        if after != 0 and (before == 0 or _sign(after) != _sign(before)):
            opened.append(self._open_position(f, after))
        if self._ej is not None:
            self._ej.append("fill", {"fill": f, "exit_reason": reason}, f.ts_fill_ns, f.ts_fill_ns)

    def _open_position(self, f: Fill, qty: Decimal) -> PositionRecord:
        pf = self.portfolio
        meta = self._entries.pop(f.client_order_id, None)
        pos = pf.positions[self._sym]
        avg = pos.avg_entry if pos.avg_entry is not None else f.price
        notional = abs(qty) * avg
        eq = pf.equity
        rec = PositionRecord(
            instrument=self._sym, side=_sign(qty), opened_at_ns=f.ts_fill_ns,
            opening_decision_ns=None if meta is None else meta.decision_ns, avg_entry=avg, qty=abs(qty),
            leverage=None if eq <= 0 else notional / eq, allocated_margin=notional / self._lev_setting,
            stop_price=None if meta is None else meta.stop, tp_price=None if meta is None else meta.tp,
            liq_price=None,
        )
        self._open_pos = rec
        return rec

    def _close_position(self, ct: ClosedTrade, f: Fill) -> PositionRecord:
        rec = self._open_pos
        if rec is None:  # pragma: no cover — угода не може закритись, не відкрившись
            raise EngineInvariantError("closed trade without an open position record")
        rec.qty = ct.qty
        rec.avg_entry = ct.entry_price
        rec.closed_at_ns = ct.closed_at_ns
        rec.exit_reason = ct.exit_reason
        rec.realized_pnl = ct.gross_pnl - ct.fees
        rec.funding_paid = ct.funding
        rec.fees = ct.fees
        rec.max_adverse_excursion = ct.max_adverse_excursion
        rec.exit_price = ct.exit_price
        self._open_pos = None
        return rec

    def _sync_open_orders(self) -> tuple[OrderRecord, ...]:
        """Стан заявок попередніх кроків після виконань бару: виконані (оновлені в _apply_fill) і ті, що
        брокер скасував сам (стоп закритої позиції — OCO, стоп-«сирота»). Фінальні — з нагляду геть."""
        out: list[OrderRecord] = []
        touched = self._touched
        for coid, rec in list(self._open_orders.items()):
            changed = coid in touched
            if rec.status not in _FINAL_STATUSES:
                ack = self.broker.order_ack(coid)
                if ack is not None and ack.status is not rec.status:
                    rec.status = ack.status
                    rec.reject_code = None if ack.reject_code is None else str(ack.reject_code)
                    changed = True
            if rec.status in _FINAL_STATUSES:
                del self._open_orders[coid]
            if changed:
                out.append(rec)
        return tuple(out)

    def _refresh_liquidation(self) -> None:
        """P_liq з умови Equity = MM для фактичної позиції: W — баланс гаманця, P_e — середня ціна входу
        (margin.py: на крос-маржі це тотожно розрахунку від поточних E і P). Лонг з P_liq ≤ 0 — недосяжна."""
        pf = self.portfolio
        pos = pf.positions[self._sym]
        q = pos.qty
        entry = pos.avg_entry
        if q == 0 or entry is None:
            return
        inst = self.instrument
        raw = liq_price(q, entry, pf.wallet_balance, inst.mmr, inst.maint_amount)
        lp = quantize_price(raw, self._tick) if raw > 0 else None
        self.broker.set_liquidation_price(self._sym, lp)
        if self._open_pos is not None and self._open_pos.liq_price is None:
            self._open_pos.liq_price = lp

    def _check_accounting(self, *, latched: bool, exposure_before: Decimal) -> None:
        pf = self.portfolio
        res = pf.identity_residual()
        if abs(res) > self._tol:
            raise EngineInvariantError(f"accounting identity broken at bar {self._index + 1}: residual {res}")
        if self.broker.position(self._sym) != pf.position_qty(self._sym):
            raise EngineInvariantError(
                f"broker position {self.broker.position(self._sym)} != "
                f"portfolio {pf.position_qty(self._sym)}")
        pos = abs(pf.position_qty(self._sym))
        if latched and pos > exposure_before:
            raise EngineInvariantError(
                f"new exposure {exposure_before} -> {pos} while HALTED / kill-switch latched "
                f"(bar {self._index + 1})")

    def _cancel_pending_increases(self) -> None:
        """Засувка: зняти заявки, що збільшили б |позицію| (вхід/розворот), і стопи, подані разом із ними
        (вони захищали б позицію, якої не буде). Стоп і рівень ліквідації відкритої позиції лишаються;
        TP нової позиції знімається — flatten-all подається на закритті цього ж бару."""
        cancelled: set[int | None] = set()
        for coid, rec in list(self._open_orders.items()):
            req = rec.request
            if req.otype is OrderType.MARKET and not req.reduce_only and self.broker.cancel(coid):
                cancelled.add(rec.decision_ns)
                self._entries.pop(coid, None)
        if not cancelled:
            return
        for coid, rec in list(self._open_orders.items()):
            if rec.role == "stop" and rec.decision_ns in cancelled:
                self.broker.cancel(coid)
        self.broker.set_take_profit(self._sym, None)

    def _on_transition(self, tr: Transition) -> None:
        self.journal.record_transition(tr, self._sym)
        self._step_transition = tr

    # ------------------------------------------------------------------ рішення

    def _decide(self, bar: Bar, dbar: DecBar, close_ns: int, *, equity: Decimal, dq_score: Decimal,
                last_data_ns: int | None, new_orders: list[OrderRecord]) -> DecisionEntry:
        pf = self.portfolio
        sym = self._sym
        fsm = self.fsm
        cur = pf.position_qty(sym)
        cur_side = _sign(cur)
        gate = self._gate
        if int(gate.side) != cur_side:
            gate.reset(Side(cur_side))           # стан тригера = фактична позиція (стоп/TP/вето/відмова)
        full = self._record == "all"
        trace: DecisionTrace | None = None
        if full:
            trace = self.core.decide(self._window, bar.t_ns)
            u_raw, kappa, u_final = trace.u_raw, trace.kappa, trace.u_final
        else:
            u_raw, kappa, u_final = self.core.intent(self._window)
        gate_side = int(gate.update(u_final))
        halted = fsm.state is RiskState.HALTED or fsm.killswitch.is_tripped
        desired = 0 if halted else gate_side
        intent = desired != cur_side
        price = dbar.c
        sizing: SizingResult | None = None
        if intent or full:
            atr = self._window.feats(0).atr
            sizing = self._sizer.size(SizingInput(
                u_final=u_final, equity=equity, price=price, atr=math.nan if atr is None else atr,
                s_t=self._vt.s_t, kappa_mode=fsm.kappa_mode_float, step_size=self._step_size,
                min_notional=self._min_notional, gross_notional=D0, sigma_ann=self._sigma_ann,
            ))
        requested = D0 if sizing is None or desired == 0 else sizing.qty * desired
        if not intent or requested == cur:
            # немає наміру змінювати позицію — або сайзер сам відмовив у вході (BELOW_MIN_NOTIONAL /
            # ZERO_QTY: requested = 0 = cur): ризик-ланцюг не оцінюється, заявок немає
            action = "hold" if cur_side != 0 else "none"
            if trace is not None:
                risk: dict[str, Any] = {"state": fsm.state.value, "kappa_mode": str(fsm.kappa_mode),
                                        "evaluated": False, "approved_qty": str(cur), "vetoes": []}
                if intent and sizing is not None and sizing.reject_code is not None:
                    risk["note"] = sizing.reject_code.value
                trace = trace.with_sizing(None if sizing is None else sizing.to_dict()).with_risk(risk)
            return DecisionEntry(
                open_time_ns=bar.t_ns, decided_at_ns=close_ns, u_raw=u_raw, kappa=kappa, u_final=u_final,
                gate_side=gate_side, current_qty=cur, requested_qty=None, target_side=cur_side,
                target_qty=abs(cur),
                binding_constraint=None if sizing is None else sizing.binding_constraint.value,
                stop_price=None, tp_price=None, liq_price=None, action=action, verdict=None, order_ids=(),
                trace=trace,
            )
        assert sizing is not None
        snap = fsm.snapshot
        if snap is None:  # pragma: no cover — автомат оновлено вище на цьому ж барі
            raise EngineInvariantError("risk state machine has no equity snapshot at decision time")
        stop_dist = sizing.stop_distance
        atr_f = self._window.feats(0).atr
        ctx = RiskContext.from_snapshot(
            snap, instrument=sym, price=price, current_qty=cur, target_qty=requested,
            atr=D0 if atr_f is None or not math.isfinite(atr_f) else to_decimal(atr_f, self._tick),
            stop_distance=D0 if not math.isfinite(stop_dist) else to_decimal(stop_dist, self._tick),
            mmr=self.instrument.mmr, maint_amount=self.instrument.maint_amount,
            leverage_setting=self._lev_setting, last_data_ns=last_data_ns, dq_score=dq_score,
            risk_state=fsm.state, step_size=self._step_size,
        )
        res = self.guard.evaluate(ctx)
        approved = res.approved_qty
        below_min = approved != 0 and abs(approved) * price < self._min_notional
        if below_min:
            approved = D0                         # стиснута ціль нижча за minNotional: не відкривати
        new_side = _sign(approved)
        opens_new = new_side not in (0, cur_side)
        if res.flatten_all:
            action, reason = "flatten", ExitReason.HALT
        elif cur_side != 0 and requested != 0 and _sign(requested) != cur_side and approved == 0:
            action, reason = "exit", ExitReason.RISK_VETO      # розворот, новий бік відхилено
        elif cur_side not in (0, new_side):
            action, reason = ("flip" if opens_new else "exit"), ExitReason.SIGNAL
        else:
            action, reason = ("enter" if opens_new else "none"), ExitReason.SIGNAL
        stop = tp = liq = None
        if opens_new:
            s = new_side
            d_stop = to_decimal(stop_dist, self._tick) if math.isfinite(stop_dist) else D0
            if d_stop > 0:
                st_p = quantize_price(price - s * d_stop, self._tick)
                stop = st_p if st_p > 0 else None
                tp_d = to_decimal((self.cfg.tp_multiple or 2.0) * stop_dist, self._tick)
                tp_p = quantize_price(price + s * tp_d, self._tick)
                tp = tp_p if tp_p > 0 and tp_d > 0 else None
            wallet = equity
            if wallet > 0:
                raw = side_liq_price(Side(s), abs(approved), price, wallet, self.instrument.mmr,
                                     maint_amount=self.instrument.maint_amount)
                liq = quantize_price(raw, self._tick) if raw > 0 else None
        order_ids = self._submit(bar.t_ns, close_ns, cur=cur, approved=approved, reason=reason, action=action,
                                 stop=stop, tp=tp, new_orders=new_orders)
        if trace is None and self._record == "trades" and order_ids:
            trace = self.core.decide(self._window, bar.t_ns)
            if self._check and trace.u_final != u_final:
                raise EngineInvariantError("fast-path u_final differs from the traced decision")
        if trace is not None:
            risk = self._risk_dict(res, approved, below_min)
            trace = trace.with_sizing(sizing.to_dict()).with_risk(risk)
        if self._ej is not None and order_ids:
            self._ej.append("decision", {
                "open_time_ns": bar.t_ns, "u_final": float_to_decimal_exact(u_final), "action": action,
                "current_qty": cur, "requested_qty": requested, "approved_qty": approved,
                "verdict": res.verdict.kind, "orders": list(order_ids),
            }, close_ns, close_ns)
        return DecisionEntry(
            open_time_ns=bar.t_ns, decided_at_ns=close_ns, u_raw=u_raw, kappa=kappa, u_final=u_final,
            gate_side=gate_side, current_qty=cur, requested_qty=requested, target_side=new_side,
            target_qty=abs(approved), binding_constraint=sizing.binding_constraint.value, stop_price=stop,
            tp_price=tp, liq_price=liq, action=action, verdict=res.verdict.kind.value, order_ids=order_ids,
            trace=trace,
        )

    def _risk_dict(self, res: GuardResult, approved: Decimal, below_min: bool) -> dict[str, Any]:
        d = res.to_dict()
        d["state"] = self.fsm.state.value
        d["kappa_mode"] = str(self.fsm.kappa_mode)
        d["evaluated"] = True
        d["approved_qty"] = str(approved)
        d["vetoes"] = [
            {"rule": r["rule"], "observed": r["observed"], "limit": r["limit"]}
            for r in d["records"] if r["verdict"] == "VETO"
        ]
        if below_min:
            d["note"] = RejectCode.BELOW_MIN_NOTIONAL.value
        return d

    def _submit(self, decision_ns: int, close_ns: int, *, cur: Decimal, approved: Decimal, reason: ExitReason,
                action: str, stop: Decimal | None, tp: Decimal | None,
                new_orders: list[OrderRecord]) -> tuple[UUID, ...]:
        delta = approved - cur
        if delta == 0:
            return ()
        sym = self._sym
        cur_side, new_side = _sign(cur), _sign(approved)
        pure_reduction = new_side == 0 or (new_side == cur_side and abs(approved) < abs(cur))
        role = action if action in ("enter", "exit", "flip", "flatten") else "entry"
        ids: list[UUID] = []
        req = OrderRequest.model_construct(   # вхід уже провалідовано: qty кратна step і > 0, бік ≠ FLAT
            client_order_id=self._ids.next_uuid(), instrument=sym,
            side=Side.LONG if delta > 0 else Side.SHORT, otype=OrderType.MARKET,
            qty=floor_qty(abs(delta), self._step_size), stop_price=None, reduce_only=pure_reduction,
            decision_ref=str(decision_ns), ts_created_ns=close_ns,
        )
        ids.append(self._send(req, decision_ns, role, reason if cur_side != 0 else None, new_orders))
        opens_new = new_side not in (0, cur_side)
        if opens_new:
            self._entries[req.client_order_id] = _Entry(decision_ns, stop, tp)
            if stop is not None:
                sreq = OrderRequest.model_construct(
                    client_order_id=self._ids.next_uuid(), instrument=sym, side=Side(-new_side),
                    otype=OrderType.STOP_MARKET, qty=abs(approved), stop_price=stop, reduce_only=True,
                    decision_ref=str(decision_ns), ts_created_ns=close_ns,
                )
                ids.append(self._send(sreq, decision_ns, "stop", ExitReason.STOP, new_orders))
            # TP прив'язаний до боку нової позиції: переживає закриття старої при розвороті (EXE-03)
            self.broker.set_take_profit(sym, tp, side=Side(new_side) if tp is not None else None)
        return tuple(ids)

    def _send(self, req: OrderRequest, decision_ns: int, role: str, reason: ExitReason | None,
              new_orders: list[OrderRecord]) -> UUID:
        ack: OrderAck = self.router.submit(req)
        coid = req.client_order_id
        rec = OrderRecord(request=req, decision_ns=decision_ns, role=role, status=ack.status,
                          reject_code=None if ack.reject_code is None else str(ack.reject_code),
                          venue_order_id=ack.venue_order_id, ts_created_ns=req.ts_created_ns,
                          intent_reason=reason)
        self._orders[coid] = rec
        if ack.status not in _FINAL_STATUSES:
            self._open_orders[coid] = rec
        if reason is not None and req.otype is OrderType.MARKET:
            self._reason[coid] = reason
        new_orders.append(rec)
        if self._ej is not None:
            self._ej.append("order", {"request": req, "status": ack.status, "reject_code": ack.reject_code,
                                      "role": role}, req.ts_created_ns, req.ts_created_ns)
        return coid

    # ------------------------------------------------------------------ завершення

    def refresh_order_statuses(self) -> None:
        """Фінальні статуси заявок з брокера (скасовані стопи, відхилені) — для рядків sim_order."""
        for coid, rec in self._orders.items():
            ack = self.broker.order_ack(coid)
            if ack is not None and rec.status is not OrderStatus.FILLED:
                rec.status = ack.status
                rec.reject_code = None if ack.reject_code is None else str(ack.reject_code)


# ====================================================================== прогін бектесту


@dataclass(frozen=True)
class BacktestResult:
    config: BacktestConfig
    seed: int
    manifest: RunManifest
    metrics: dict[str, float]            # 17 метрик backtest.metrics.METRIC_NAMES (вікно оцінки)
    extras: dict[str, float]             # PSR, Шарп за період, моменти, лічильники
    equity: list[Decimal]                # капітал на закритті кожного бару
    equity_ts: list[int]
    position_series: list[Decimal]
    equity_points: list[EquityPointRecord]
    trades: list[ClosedTrade]
    positions: list[PositionRecord]      # закриті + (остання) відкрита
    orders: list[OrderRecord]
    fills: list[Fill]
    funding: list[FundingCharge]
    decisions: list[DecisionEntry]
    risk_events: list[RiskEventRecord]
    transitions: list[Transition]
    warmup_bars: int
    eval_start: int                      # індекс першого бару вікна оцінки
    halted_at: int | None
    final_state: RiskState
    killswitch_tripped: bool

    @property
    def equity_hash(self) -> str:
        h = self.manifest.equity_hash
        if h is None:
            raise ValueError("equity hash was not computed (run_backtest(hash_equity=False))")
        return h


def _metrics(equity: Sequence[Decimal], trades: Sequence[ClosedTrade], positions: Sequence[Decimal],
             fills: Sequence[Fill], periods: int) -> tuple[dict[str, float], dict[str, float]]:
    notional = sum((f.qty * f.price for f in fills), D0)
    m = compute_metrics(equity, trades, periods, positions=positions, traded_notional=notional)
    r = returns_from_equity(equity)
    g3, g4 = moments(r)
    sr = sharpe_ratio(r, 1.0)
    try:
        p = psr(sr, int(r.size), g3, g4) if r.size >= 2 else math.nan
    except ValueError:
        p = math.nan              # ŜR = ±inf або невизначений підкореневий вираз — PSR не визначений
    extras = {"sr_period": sr, "skew": g3, "kurt": g4, "psr": p, "n_obs": r.size + 0.0,
              "n_fills": len(fills) + 0.0, "traded_notional": to_float(notional)}
    return m, extras


def run_order_ids(seed: int, run_id: UUID) -> SeededIdGenerator:
    """Генератор client_order_id прогону, що ЗБЕРІГАЄТЬСЯ (API, скрипти, воркери): простір імен — з run_id.

    client_order_id — UNIQUE у sim_order і ключ ідемпотентності виконавця, тож два збережені прогони з тим
    самим seed не можуть мати тих самих id (дефолтний SeededIdGenerator(seed) давав однакові — знайдено на
    робочій БД, W-10). Детермінізм зберігається: той самий (seed, run_id) → ті самі id.
    """
    return SeededIdGenerator(seed, b"fuzzhelm.engine" + run_id.bytes)


def deterministic_run_id(cfg_hash: str, ds_hash: str, seed: int) -> UUID:
    """Ідентифікатор прогону з його ідентичності (той самий прогін → той самий run_id і хеш журналу)."""
    h = hashlib.blake2b(digest_size=16)
    h.update(bytes.fromhex(cfg_hash))
    h.update(bytes.fromhex(ds_hash))
    h.update(seed.to_bytes(16, "big", signed=True))
    return UUID(bytes=h.digest(), version=4)


def run_backtest(dataset: Dataset, cfg: BacktestConfig | None = None, seed: int = 0, *, eval_start: int = 0,
                 git: bool = False, kind: RunKind | str = RunKind.BACKTEST, run_id: UUID | None = None,
                 hash_equity: bool = True,
                 on_step: Callable[[StepResult], None] | None = None,
                 journal_sink: Callable[[JournalEntry], None] | None = None) -> BacktestResult:
    """Подієвий бектест: TradingLoop над барами датасету (через LookaheadGuard), метрики і паспорт.

    `eval_start` — перший індекс, з якого дозволено торгувати (≥ прогріву); метрики рахуються з бару
    першого можливого рішення. Хеш кривої (equity_hash) — над (close_ts, E) кожного бару.
    `journal_sink` отримує кожен запис хеш-ланцюга EventJournal (заявки, виконання, рішення із заявками,
    risk_event) — для запису event_journal прогону (workers.persist); голова ланцюга = journal_head_hash.
    """
    cfg = cfg or BacktestConfig()
    ds_hash = dataset.dataset_hash
    rid = run_id or deterministic_run_id(cfg.config_hash, ds_hash, seed)
    ej = EventJournal(rid, sink=journal_sink, keep=False) if cfg.record_traces != "none" else None
    loop = TradingLoop(dataset.instrument, cfg, seed=seed, tf=dataset.tf, trade_start=eval_start,
                       run_id=rid, event_journal=ej,
                       ids=None if run_id is None else run_order_ids(seed, run_id))
    loop.set_funding_series(dataset.funding_t_ns, dataset.funding_rate)
    step = loop.step
    for bar, dbar, close_ns in dataset.feed():
        sr = step(bar, close_ns, dbar=dbar)
        if on_step is not None:
            on_step(sr)
    loop.refresh_order_statuses()
    start = min(loop.decide_from, max(0, len(loop.equity) - 1))
    eq = loop.equity[start:]
    fills_eval = [f for f in loop.fills if f.ts_fill_ns > loop.equity_ts[start]] if loop.equity_ts else []
    metrics, extras = _metrics(eq, loop.portfolio.closed_trades, loop.position_series[start:], fills_eval,
                               BARS_PER_YEAR[dataset.tf])
    extras["halted"] = 0.0 if loop.halted_at is None else 1.0
    manifest = build_manifest(kind=kind, engine=cfg.engine or "mamdani", seed=seed,
                              config=cfg.identity_dict(), dataset=dataset.columns(),
                              journal_head=None if ej is None else ej.head,
                              equity=loop.equity if hash_equity else None,
                              equity_ts_ns=loop.equity_ts if hash_equity else None, git=git)
    positions = list(loop.positions)
    if loop.open_position is not None:
        positions.append(loop.open_position)
    return BacktestResult(
        config=cfg, seed=seed, manifest=manifest, metrics=metrics, extras=extras, equity=loop.equity,
        equity_ts=loop.equity_ts, position_series=loop.position_series, equity_points=loop.equity_points,
        trades=list(loop.portfolio.closed_trades), positions=positions, orders=list(loop.orders),
        fills=loop.fills, funding=loop.funding, decisions=loop.decisions, risk_events=loop.risk_events,
        transitions=loop.transitions, warmup_bars=loop.warmup_bars, eval_start=start,
        halted_at=loop.halted_at, final_state=loop.fsm.state,
        killswitch_tripped=loop.fsm.killswitch.is_tripped,
    )
