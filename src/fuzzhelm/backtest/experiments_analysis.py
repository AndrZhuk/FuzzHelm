"""Аналітичні обчислювальні експерименти фази 7: моделі витрат, ablation, VaR/Купець, ціна гістерезису.

Найменування: backtest/experiments_analysis.py
Призначення: чисті помічники й воркери для scripts/{cost_models, ablation, var_backtest, hysteresis_cost,
    plot_equity, plot_var, export_report_tables}.py (брифінг §5.9, §5.13–§5.16, §12 фаза 7, §14 підрозділи
    2.6–2.8, 2.11, 2.12): варіанти конфігурації, OOS-сегменти walk-forward, воркер сегмента (чиста функція над
    масивами — без БД і event loop, pickle для ProcessPoolExecutor), агрегування фолдів, VaR/CVaR і тест Купця
    на підмножинах барів, будівники таблиць і паспорт виводу, розбір брифінгу для зведення тестів і таблиці
    трасування, рисунки (matplotlib Agg, лише 2D), завантаження джерел (фікстура / БД — ліниві імпорти).
Автор: Андрій Жук, 2026.

Протокол оцінки (docs/api/exp_analysis.md):
  * фолди — ті самі, що в сітці/walk-forward: backtest.walkforward.folds_from_profile (профіль backtest,
    embargo = 2·max_lookback); OOS-сегмент фолду = [oos_start − W, oos_end) з вікном оцінки від W (прогрів
    W = 523 бари лежить усередині embargo) — так само, як runner.run_walkforward;
  * кожен OOS-фолд — окремий прогін з E₀ = 10 000 USDT і NORMAL-станом автомата; зведення по OOS —
    зчеплена крива E₀·Π(1 + r_t) з конкатенованих дохідностей фолдів (Шарп, дохідність, MaxDD), сума
    комісій/угод, оборот — зважене за барами середнє оборотів фолдів (та сама формула, що в compute_metrics);
  * «fixed» — ті самі параметри на всіх фолдах; «is_grid» — на IS кожного фолду сітка клітинок і вибір
    правилом фронту Парето (ε-обмеження: max SR серед недомінованих клітинок з MaxDD ≤ dd_cap — те саме
    правило, що в walk-forward exp_search; альтернатива --rule sharpe — runner.select_cell), на OOS — вибрана
    клітинка. Параметри, обрані за OOS-метриками тих самих фолдів (робоча точка сітки exp_search,
    front_space = oos), позначаються у виводі: OOS-зведення для них — не чиста поза-вибіркова оцінка.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import csv
import dataclasses
import json
import math
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from statistics import NormalDist
from typing import Any
from uuid import UUID

import numpy as np
import numpy.typing as npt

from fuzzhelm.backtest.dataset import BARS_PER_YEAR, Dataset, load_exchange_instrument, load_klines_json
from fuzzhelm.backtest.engine import GRID_KEYS, BacktestConfig, StepResult, run_backtest
from fuzzhelm.backtest.grid import make_grid
from fuzzhelm.backtest.manifest import read_git_sha
from fuzzhelm.backtest.metrics import EULER_GAMMA, max_drawdown, moments, psr, psr_from_returns, sharpe_ratio
from fuzzhelm.backtest.parallel import run_parallel
from fuzzhelm.backtest.pareto import pareto_front
from fuzzhelm.backtest.runner import select_cell
from fuzzhelm.backtest.walkforward import Fold, folds_from_profile, make_folds
from fuzzhelm.config import ROOT, load_yaml
from fuzzhelm.core.enums import Liquidity, RiskState
from fuzzhelm.core.money import D0
from fuzzhelm.decision.aggregator import V_DEFAULT
from fuzzhelm.detectors.registry import DETECTOR_NAMES
from fuzzhelm.features.convert import to_float
from fuzzhelm.risk.kupiec import CHI2_1_95, kupiec_pof
from fuzzhelm.risk.var import historical_var_cvar, rolling_var_breaches

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

VAR_WINDOW = 500                     # §5.13: W = 500
VAR_ALPHA = 0.05                     # VaR₉₅
V_NEUTRAL = V_DEFAULT                # V без VolRegime: сподівання перцентильного рангу (decision.aggregator)
BRIEF_HYSTERESIS_CLAIM_PCT = 1.15    # §5.9 дослівно: «−1.15 % капіталу на добу» — твердження, не вимір
STATE_ORDER: tuple[RiskState, ...] = (RiskState.NORMAL, RiskState.WARNING, RiskState.COOLDOWN,
                                      RiskState.HALTED)
STATE_CODE: dict[RiskState, int] = {s: i for i, s in enumerate(STATE_ORDER)}
PARAM_KEYS: tuple[str, ...] = (*GRID_KEYS, "u_exit", "tp_multiple", "cooldown_policy")
DETECTOR_LABELS: dict[str, str] = {
    "ema_slope": "EmaSlope", "donchian": "Donchian", "rsi_exhaustion": "RsiExhaustion",
    "bollinger_z": "BollingerZ", "candle_geometry": "CandleGeometry", "vol_regime": "VolRegime",
}
COST_LABELS: dict[str, str] = {
    "zero": "zero (без витрат)", "sqrt_impact": "sqrt_impact (спред + √-імпакт)",
    "full": "full (√-імпакт + комісії + фандинг)",
}
DEFAULT_OUT = ROOT / "artifacts" / "tmp"
TBD_FMT = "<<TBD:{}>>"


def tbd(name: str) -> str:
    """Маркер відсутнього числа (contracts.md §0 п. 9): жодних вигаданих значень."""
    return TBD_FMT.format(name)


def profile_seed(profile: str = "backtest") -> int:
    return int(load_yaml(f"profiles/{profile}")["seed"])


# ====================================================================== параметри і варіанти


def parse_params(text: str | None) -> dict[str, Any]:
    """`--params`: JSON-об'єкт або `@шлях` до JSON-файлу.

    У файлі параметри можуть лежати в корені або під ключем `params` / `chosen` / `selected_params` /
    `choice.params` (вихід сітки exp_search з робочою точкою фронту Парето). Дозволені ключі — PARAM_KEYS;
    невідомий ключ → ValueError.
    """
    if not text:
        return {}
    raw: Any = json.loads(Path(text[1:]).read_text(encoding="utf-8") if text.startswith("@") else text)
    if isinstance(raw, Mapping) and isinstance(raw.get("choice"), Mapping):
        raw = raw["choice"]
    if isinstance(raw, Mapping):
        for key in ("params", "chosen", "selected_params"):
            if isinstance(raw.get(key), Mapping):
                raw = raw[key]
                break
    if not isinstance(raw, Mapping):
        raise ValueError("--params must be a JSON object")
    bad = set(raw) - set(PARAM_KEYS)
    if bad:
        raise ValueError(f"unknown parameters {sorted(bad)}; allowed {PARAM_KEYS}")
    out: dict[str, Any] = {}
    for k, v in raw.items():
        if k == "n_atr":
            out[k] = int(v)
        elif k == "cooldown_policy":
            out[k] = str(v)
        else:
            if isinstance(v, bool) or not isinstance(v, int | float):
                raise ValueError(f"parameter {k} must be a number, got {v!r}")
            out[k] = v + 0.0
    return out


def params_source(text: str | None) -> dict[str, Any]:
    """Походження `--params` для паспорта: звідки параметри і чи обирались вони за OOS-метриками.

    Вихід сітки exp_search (`choice` + `front_space`) обирає робочу точку за метриками конкатенованого OOS тих
    самих фолдів walk-forward (§5.16: фронт у критеріях SR_OOS, MaxDD_OOS, Turnover_OOS). Тоді OOS-зведення
    цих параметрів тут — не чиста поза-вибіркова оцінка (вибіркове зміщення; його враховує DSR з N прогонів
    сітки), і вивід мусить це сказати: `selected_on_oos = True`.
    """
    if not text:
        return {"kind": "profile", "selected_on_oos": False}
    if not text.startswith("@"):
        return {"kind": "inline", "selected_on_oos": False}
    path = Path(text[1:])
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Any] = {"kind": "file", "path": str(path), "selected_on_oos": False}
    if isinstance(raw, Mapping):
        choice = raw.get("choice") if isinstance(raw.get("choice"), Mapping) else None
        space = raw.get("front_space")
        out.update({"front_space": space, "selection_rule": raw.get("selection_rule"),
                    "choice_rule": choice.get("rule") if choice else None,
                    "choice_index": choice.get("index") if choice else None,
                    "dd_cap": choice.get("dd_cap") if choice else raw.get("dd_cap")})
        prov = raw.get("provenance") if isinstance(raw.get("provenance"), Mapping) else {}
        if prov:
            out.update({"source_git_sha": prov.get("git_sha"), "source_command": prov.get("command")})
        out["selected_on_oos"] = bool(choice) and str(space).lower() == "oos"
    return out


def selection_caveat(passport_: Mapping[str, Any]) -> list[str]:
    """Застереження для markdown: параметри обрано за OOS тих самих фолдів ⇒ OOS тут не чистий."""
    src = passport_.get("params_source") or {}
    if passport_.get("selection") == "is_grid" or not src.get("selected_on_oos"):
        return []
    return [f"**Увага: параметри `--params` обрано за OOS-метриками тих самих фолдів** (`{src.get('path')}`, "
            f"front_space = {src.get('front_space')}, правило {src.get('choice_rule')}). OOS-зведення цієї "
            "конфігурації — не чиста поза-вибіркова оцінка (вибіркове зміщення, яке DSR враховує через N "
            "прогонів сітки); у порівняннях варіантів зміщення на користь базової конфігурації. Чиста "
            "OOS-оцінка — `--selection is_grid` (вибір на IS кожного фолду, для кожного варіанта окремо).",
            ""]


def is_rule_text(passport_: Mapping[str, Any]) -> str:
    """Правило вибору клітинки на IS для markdown (з паспорта)."""
    if passport_.get("is_rule") == "sharpe":
        return "вибір argmax SR (рівність → менший оборот; `--rule sharpe`)"
    return (f"вибір на фронті Парето (SR↑, MaxDD↓, оборот↓) методом ε-обмеження: max SR серед клітинок "
            f"фронту з MaxDD ≤ {fmt(passport_.get('dd_cap'))} (cool_enter; жодної — мінімальна MaxDD фронту)")


def base_config(params: Mapping[str, Any] | None = None, *, profile: str = "backtest") -> BacktestConfig:
    """Профіль рушія (Мамдані, cost_mode, E₀) + перекриття параметрів; валідує ризик-дерево одразу."""
    cfg = BacktestConfig.from_profile(profile).with_params(**dict(params or {}))
    cfg.risk_config()          # u_exit > u_enter тощо → помилка тут, а не у воркері
    return cfg


@dataclass(frozen=True)
class Variant:
    """Один варіант експерименту: конфігурація + чи прив'язувати u_exit до u_enter у клітинках сітки."""

    key: str
    label: str
    config: BacktestConfig
    tie_exit: bool = False
    note: str = ""


def _with_trees(cfg: BacktestConfig, mutate: Callable[[dict[str, Any]], None]) -> BacktestConfig:
    trees = copy.deepcopy(dict(cfg.trees))
    mutate(trees)
    return dataclasses.replace(cfg, trees=trees)


def kappa_off(cfg: BacktestConfig) -> BacktestConfig:
    """κ ≡ 1: κ_min = 1 ⇒ κ = κ_min + (1 − κ_min)·A_g^ν = 1 для будь-якої узгодженості (§5.4)."""
    def mutate(t: dict[str, Any]) -> None:
        det = t["detectors"]
        det["agreement"] = {**(det.get("agreement") or {}), "kappa_min": 1.0}
    return _with_trees(cfg, mutate)


def hysteresis_off(cfg: BacktestConfig) -> BacktestConfig:
    """Компаратор без петлі: u_exit = u_enter (вхід і вихід на одному порозі, §5.9)."""
    return cfg.with_params(u_exit=cfg.u_enter)


# Послаблений контур ризику — ЛИШЕ для ізоляції ефекту в експерименті (не конфігурація системи): пороги
# автомата й денних/просадкових лімітів винесено до 0.94–0.97, тригер волатильності — до 1e9, щоб HALTED/
# COOLDOWN не обрізали торгівлю і вимір витрат не розбавлявся пласкими днями після засувки.
RELAXED_STATE_MACHINE: dict[str, Any] = {
    "warn_exit": 0.93, "warn_enter": 0.94, "warn_vol_ratio": 1e9, "cool_exit": 0.94, "cool_enter": 0.95,
    "halt_enter": 0.97, "cool_daily_loss": 0.95, "halt_daily_loss": 0.97,
}
RELAXED_LIMITS: dict[str, float] = {"max_daily_loss": 0.95, "max_drawdown_halt": 0.97}


def relaxed_risk(cfg: BacktestConfig) -> BacktestConfig:
    def mutate(t: dict[str, Any]) -> None:
        risk = t["risk_limits"]
        risk["state_machine"] = {**(risk.get("state_machine") or {}), **RELAXED_STATE_MACHINE}
        limits = {**(risk.get("limits") or {})}
        for name, value in RELAXED_LIMITS.items():
            limits[name] = {**(limits.get(name) or {}), "value": value}
        risk["limits"] = limits
    out = _with_trees(cfg, mutate)
    out.risk_config()
    return out


def apply_risk_loop(cfg: BacktestConfig, mode: str) -> BacktestConfig:
    if mode == "default":
        return cfg
    if mode == "relaxed":
        return relaxed_risk(cfg)
    raise ValueError(f"risk loop mode must be default|relaxed, got {mode!r}")


def ablation_variants(cfg: BacktestConfig) -> list[Variant]:
    """Базова лінія + 6 × «без детектора» + лінійне голосування + κ ≡ 1 + без гістерезису (10 варіантів)."""
    out = [Variant("baseline", "базова (6 детекторів, Мамдані, κ, гістерезис)", cfg)]
    for name in DETECTOR_NAMES:
        keep = tuple(n for n in DETECTOR_NAMES if n != name)
        note = (f"V ≡ {V_NEUTRAL} (нейтральне значення: decision.aggregator.V_DEFAULT)"
                if name == "vol_regime" else "")
        out.append(Variant(f"no_{name}", f"без {DETECTOR_LABELS[name]}", cfg.with_params(detectors=keep),
                           note=note))
    out.append(Variant("linear", "LinearVoteEngine замість Мамдані", cfg.with_params(engine="linear"),
                       note="u = 0.5·T + 0.5·R (config/engine.yaml linear.weights)"))
    out.append(Variant("kappa_off", "κ ≡ 1 (без гасіння узгодженістю)", kappa_off(cfg),
                       note="agreement.kappa_min = 1.0"))
    out.append(Variant("hysteresis_off", "без гістерезису (u_exit = u_enter)", hysteresis_off(cfg),
                       tie_exit=True))
    return out


def cost_variants(cfg: BacktestConfig) -> list[Variant]:
    """Три рівні деградації моделі витрат §5.14 на тій самій конфігурації."""
    return [Variant(m, COST_LABELS[m], cfg.with_params(cost_mode=m)) for m in ("zero", "sqrt_impact", "full")]


def hysteresis_variants(cfg: BacktestConfig) -> list[Variant]:
    return [
        Variant("with", f"тригер Шмітта (вхід {cfg.u_enter:g}, вихід {cfg.u_exit:g})", cfg),
        Variant("without", f"без петлі (вхід = вихід = {cfg.u_enter:g})", hysteresis_off(cfg), tie_exit=True),
    ]


def cell_config(variant: Variant, cell: Mapping[str, Any]) -> BacktestConfig:
    """Клітинка сітки поверх варіанта; для «без гістерезису» u_exit прив'язується до u_enter клітинки."""
    cfg = variant.config.with_params(**dict(cell))
    if variant.tie_exit:
        cfg = cfg.with_params(u_exit=cfg.u_enter)
    return cfg


FRONT_KEYS: tuple[str, str, str] = ("sharpe", "max_drawdown", "turnover")    # (SR↑, MaxDD↓, Turnover↓), §5.16


def _finite_or(x: Any, default: float) -> float:
    v = default if x is None else x + 0.0
    return default if math.isnan(v) else v


def select_on_front(metrics: Sequence[Mapping[str, Any]], dd_cap: float) -> dict[str, Any]:
    """Вибір на IS за фронтом Парето (§5.16) — метод ε-обмеження: серед недомінованих клітинок з
    MaxDD ≤ dd_cap — найбільший Шарп (рівність → менший оборот → менший індекс); жодна клітинка фронту не
    вкладається в dd_cap → клітинка фронту з найменшою MaxDD (рівність → більший Шарп → менший оборот →
    менший індекс). NaN — найгірше значення критерію. Те саме правило, що в walk-forward exp_search (його
    код не імпортується — він пишеться паралельно; спільний лише backtest.pareto)."""
    if not metrics:
        raise ValueError("no cells to choose from")
    if not dd_cap > 0:
        raise ValueError("dd_cap must be > 0")
    front = pareto_front([tuple(m[k] + 0.0 for k in FRONT_KEYS) for m in metrics])

    def sr(i: int) -> float:
        return _finite_or(metrics[i]["sharpe"], -math.inf)

    def dd(i: int) -> float:
        return _finite_or(metrics[i]["max_drawdown"], math.inf)

    def to(i: int) -> float:
        return _finite_or(metrics[i]["turnover"], math.inf)

    feasible = [i for i in front if dd(i) <= dd_cap]
    if feasible:
        best, rule = min(feasible, key=lambda i: (-sr(i), to(i), i)), "eps_constraint"
    else:
        best, rule = min(front, key=lambda i: (dd(i), -sr(i), to(i), i)), "fallback_min_dd"
    return {"index": best, "rule": rule, "front": front, "feasible": feasible, "dd_cap": dd_cap}


def select_is_cell(metrics: Sequence[Mapping[str, Any]], rule: str, dd_cap: float) -> dict[str, Any]:
    """Правило вибору клітинки на IS: "front" (типово, select_on_front) або "sharpe" (runner.select_cell)."""
    if rule == "front":
        return select_on_front(metrics, dd_cap)
    if rule == "sharpe":
        return {"index": select_cell(metrics), "rule": "argmax_sharpe", "front": None, "feasible": None,
                "dd_cap": None}
    raise ValueError(f"IS selection rule must be front|sharpe, got {rule!r}")


def default_dd_cap(cfg: BacktestConfig) -> float:
    """dd_cap за замовчуванням = state_machine.cool_enter (просадка входу в COOLDOWN) конфігурації."""
    return to_float(cfg.risk_config().state_machine.cool_enter)


def select_cells(n: int | None, cells: Sequence[Mapping[str, Any]] | None = None) -> list[dict[str, Any]]:
    """n рівномірно розставлених клітинок сітки (None / ≥ розміру — уся сітка, порядок збережено)."""
    grid = [dict(c) for c in (cells if cells is not None else make_grid())]
    if n is None or n >= len(grid):
        return grid
    if n <= 0:
        raise ValueError("number of cells must be > 0")
    if n == 1:
        return [grid[0]]
    idx = sorted({round(i * (len(grid) - 1) / (n - 1)) for i in range(n)})
    return [grid[i] for i in idx]


# ====================================================================== сегменти


@dataclass(frozen=True, slots=True)
class Segment:
    label: str          # "oos_0", "is_0", "full"
    kind: str           # "oos" | "is" | "full"
    fold: int | None
    start: int          # [start, stop) у барах набору
    stop: int
    eval_start: int     # індекс (відносно start), з якого дозволено торгувати і рахуються метрики


def oos_segments(folds: Sequence[Fold], warmup: int) -> list[Segment]:
    """OOS-сегменти як у runner.run_walkforward: прогрів W барів — усередині embargo (інакше ValueError)."""
    out: list[Segment] = []
    for f in folds:
        start = f.oos_start - warmup
        if start < f.is_end:
            raise ValueError(f"fold {f.index}: embargo {f.embargo_bars} bars is shorter than the warm-up "
                             f"{warmup} bars — OOS warm-up would read IS bars")
        out.append(Segment(f"oos_{f.index}", "oos", f.index, start, f.oos_end, warmup))
    return out


def is_segments(folds: Sequence[Fold], warmup: int) -> list[Segment]:
    out: list[Segment] = []
    for f in folds:
        if f.is_end - f.is_start <= warmup:
            raise ValueError(f"fold {f.index}: IS window {f.is_end - f.is_start} bars <= warm-up {warmup}")
        out.append(Segment(f"is_{f.index}", "is", f.index, f.is_start, f.is_end, 0))
    return out


def full_segment(n_bars: int) -> Segment:
    return Segment("full", "full", None, 0, n_bars, 0)


def smoke_folds(n_bars: int, max_lookback: int, warmup: int, k: int = 2) -> list[Fold]:
    """Зменшене розбиття для димових прогонів (ті самі правила: embargo = 2·max_lookback ≥ прогріву)."""
    embargo = 2 * max_lookback
    is_bars = embargo + warmup + max(100, n_bars // 10)
    oos = (n_bars - is_bars) // k
    if oos < 50:
        raise ValueError(f"{n_bars} bars are too few for a {k}-fold smoke layout (need > {is_bars + 50 * k})")
    return make_folds(n_bars, is_bars, oos, oos, embargo, k)


def eval_folds(n_bars: int, cfg: BacktestConfig, *, smoke: bool) -> tuple[list[Fold], str]:
    """Фолди експерименту: профіль backtest (45 днів: 15/5/5 × 6), для --smoke — зменшене розбиття."""
    ml = cfg.feature_params().max_lookback
    if smoke:
        return smoke_folds(n_bars, ml, cfg.resolved_warmup()), "smoke"
    try:
        return folds_from_profile(n_bars, max_lookback=ml), "profile"
    except ValueError as e:
        raise ValueError(f"{e}; the walk-forward profile needs the 45-day window (--db or --smoke)") from e


def fold_dicts(folds: Sequence[Fold]) -> list[dict[str, int]]:
    return [{"index": f.index, "is_start": f.is_start, "is_end": f.is_end, "embargo_bars": f.embargo_bars,
             "oos_start": f.oos_start, "oos_end": f.oos_end} for f in folds]


# ====================================================================== воркер сегмента (чиста функція)


def _dsum(values: Iterable[Decimal]) -> float:
    return to_float(sum(values, D0))


def segment_task(task: Mapping[str, Any]) -> dict[str, Any]:
    """Точка входу ProcessPoolExecutor: один прогін TradingLoop над масивами сегмента → метрики + витрати.

    task = {config: BacktestConfig.to_dict(), arrays: Dataset.to_payload(), seed, eval_start, series: bool,
            meta: {...}}. Без БД і файлів (конфіг герметичний). record_traces = "none" (як клітинка сітки);
    стан автомата і u_final кожного бару збираються через on_step (дешево), ряди — лише з series=True.
    """
    cfg = BacktestConfig.from_dict(task["config"]).with_params(record_traces="none", check_invariants=False)
    ds = Dataset.from_payload(task["arrays"])
    seed = int(task["seed"])
    states: list[int] = []
    us: list[float] = []

    def on_step(sr: StepResult) -> None:
        states.append(STATE_CODE[sr.risk_state])
        us.append(math.nan if sr.decision is None else sr.decision.u_final)

    res = run_backtest(ds, cfg, seed, eval_start=int(task.get("eval_start", 0)), git=False, hash_equity=True,
                       on_step=on_step)
    start = res.eval_start
    t0 = res.equity_ts[start]
    fills = [f for f in res.fills if f.ts_fill_ns > t0]
    funding = [c for c in res.funding if c.ts_ns > t0]
    trades = [t for t in res.trades if t.closed_at_ns > t0]
    notional = [f.qty * f.price for f in fills]
    slip = [f.qty * f.price * f.slippage_bps / 10_000 for f in fills]
    n_eval = len(res.equity) - start - 1
    st = np.asarray(states[start:], dtype=np.int8)
    share = {s.value: (np.count_nonzero(st == STATE_CODE[s]) / st.size if st.size else math.nan)
             for s in STATE_ORDER}
    eq0 = res.equity[start]
    out: dict[str, Any] = {
        "meta": dict(task.get("meta") or {}),
        "metrics": dict(res.metrics),
        "extras": dict(res.extras),
        "costs": {
            "fees": _dsum(f.fee for f in fills),
            "funding": _dsum(c.amount for c in funding),
            "slippage_cost": _dsum(slip),
            "traded_notional": _dsum(notional),
            "n_fills": len(fills),
            "n_taker_fills": sum(1 for f in fills if f.liquidity is Liquidity.TAKER),
            "gross_pnl": _dsum(t.gross_pnl for t in trades),
            "equity_start": to_float(eq0),
            "equity_end": to_float(res.equity[-1]),
        },
        "n_eval_bars": n_eval,
        "days": n_eval / (BARS_PER_YEAR[ds.tf] / 365.0),
        "state_share": share,
        "final_state": res.final_state.value,
        "halted": res.halted_at is not None,
        "equity_hash": res.equity_hash,
        "config_hash": cfg.config_hash,
        "dataset_hash": ds.dataset_hash,
        "series": None,
    }
    if task.get("series"):
        eq = np.array([to_float(e) for e in res.equity[start:]], dtype=np.float64)
        pos = np.array([(q > 0) - (q < 0) for q in res.position_series[start:]], dtype=np.int8)
        out["series"] = {
            "ts_ns": np.asarray(res.equity_ts[start:], dtype=np.int64),
            "equity": eq,
            "returns": eq[1:] / eq[:-1] - 1.0,
            "position": pos,
            "state": st,
            "u_final": np.asarray(us[start:], dtype=np.float64),
        }
    return out


@dataclass
class TaskPlan:
    """Задачі одного етапу + ключі, за якими результати повертаються назад (порядок = порядок задач)."""

    tasks: list[dict[str, Any]] = dataclasses.field(default_factory=list)

    def add(self, dataset: Dataset, seg: Segment, cfg: BacktestConfig, seed: int, *, series: bool,
            meta: Mapping[str, Any], payloads: dict[str, dict[str, Any]]) -> None:
        payload = payloads.get(seg.label)
        if payload is None:
            payload = dataset.slice(seg.start, seg.stop).to_payload()
            payloads[seg.label] = payload
        self.tasks.append({"config": cfg.to_dict(), "arrays": payload, "seed": seed,
                           "eval_start": seg.eval_start, "series": series,
                           "meta": {**meta, "segment": seg.label, "kind": seg.kind, "fold": seg.fold,
                                    "start": seg.start, "stop": seg.stop}})

    def run(self, workers: int, seed: int) -> list[dict[str, Any]]:
        return run_parallel(segment_task, self.tasks, workers, seed=seed)


def evaluate_variants(dataset: Dataset, variants: Sequence[Variant], *, folds: Sequence[Fold], seed: int,
                      workers: int = 0, scopes: Sequence[str] = ("oos",), selection: str = "fixed",
                      cells: Sequence[Mapping[str, Any]] | None = None, series: bool = True,
                      rule: str = "front", dd_cap: float | None = None,
                      log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Прогони варіантів на OOS-фолдах (і/або повному вікні) → {variant: {scope: [результати сегментів]}}.

    selection="is_grid": на IS кожного фолду — сітка `cells`, вибір select_is_cell(rule, dd_cap) лише за
    IS-метриками (IS-сегмент = [is_start, is_end) — жодного бару embargo/OOS), на OOS — вибрана клітинка
    (для повного вікна «is_grid» не визначений — там лише параметри варіанта). Сегменти одного фолду
    спільні для всіх варіантів (прогрів однаковий: warmup від повного набору детекторів).
    """
    if selection not in ("fixed", "is_grid"):
        raise ValueError(f"selection must be fixed|is_grid, got {selection!r}")
    warmup = variants[0].config.resolved_warmup()
    if any(v.config.resolved_warmup() != warmup for v in variants):
        raise ValueError("variants must share the warm-up length")
    payloads: dict[str, dict[str, Any]] = {}
    chosen: dict[str, list[dict[str, Any]]] = {}
    choice_info: dict[str, list[dict[str, Any]]] = {}
    is_metrics: dict[str, list[list[dict[str, Any]]]] = {}
    if selection == "is_grid" and "oos" in scopes:
        grid = [dict(c) for c in (cells if cells is not None else make_grid())]
        cap = dd_cap if dd_cap is not None else default_dd_cap(variants[0].config)
        plan = TaskPlan()
        segs = is_segments(folds, warmup)
        for v in variants:
            for seg in segs:
                for ci, cell in enumerate(grid):
                    ccfg = cell_config(v, cell)
                    if ccfg.resolved_warmup() != warmup:     # OOS-сегменти спільні: прогрів мусить збігатись
                        raise ValueError(f"cell {cell} changes the warm-up: "
                                         f"{ccfg.resolved_warmup()} != {warmup}")
                    plan.add(dataset, seg, ccfg, seed, series=False,
                             meta={"variant": v.key, "cell": ci}, payloads=payloads)
        if log:
            log(f"IS stage: {len(plan.tasks)} runs ({len(variants)} variants x {len(segs)} folds x "
                f"{len(grid)} cells)")
        res = plan.run(workers, seed)
        n = len(grid)
        for vi, v in enumerate(variants):
            per_fold = [res[(vi * len(segs) + k) * n:(vi * len(segs) + k + 1) * n] for k in range(len(segs))]
            is_metrics[v.key] = [[{**r["metrics"], **r["extras"]} for r in fold_res] for fold_res in per_fold]
            choice_info[v.key] = [select_is_cell(m, rule, cap) for m in is_metrics[v.key]]
            chosen[v.key] = [grid[c["index"]] for c in choice_info[v.key]]
    plan = TaskPlan()
    for v in variants:
        if "oos" in scopes:
            for seg in oos_segments(folds, warmup):
                assert seg.fold is not None
                cfg = cell_config(v, chosen[v.key][seg.fold]) if selection == "is_grid" else v.config
                plan.add(dataset, seg, cfg, seed, series=series, meta={"variant": v.key}, payloads=payloads)
        if "full" in scopes:
            plan.add(dataset, full_segment(len(dataset)), v.config, seed, series=series,
                     meta={"variant": v.key}, payloads=payloads)
    if log:
        log(f"evaluation stage: {len(plan.tasks)} runs ({len(variants)} variants, scopes {list(scopes)})")
    results = plan.run(workers, seed)
    out: dict[str, Any] = {v.key: {s: [] for s in scopes} for v in variants}
    for r in results:
        m = r["meta"]
        out[m["variant"]][m["kind"]].append(r)
    return {"results": out, "chosen": chosen, "choice_info": choice_info, "is_metrics": is_metrics}


# ====================================================================== агрегування


SUMMARY_KEYS: tuple[str, ...] = (
    "sharpe", "sharpe_fold_mean", "total_return", "max_drawdown", "turnover", "fees", "funding",
    "slippage_cost", "gross_pnl", "n_trades", "n_fills", "fees_per_day_pct", "psr", "exposure", "days",
    "n_segments", "halted_segments", "share_halted", "share_cooldown",
)


def summarize(results: Sequence[Mapping[str, Any]], periods_per_year: int = BARS_PER_YEAR["1m"],
              equity0: float | None = None) -> dict[str, Any]:
    """Зведення сегментів одного варіанта (зчеплена крива з конкатенованих дохідностей, докстрінг модуля).

    Для одного сегмента (повне вікно) Шарп/дохідність/MaxDD збігаються з метриками рушія.
    """
    if not results:
        return {k: math.nan for k in SUMMARY_KEYS}
    if any(r.get("series") is None for r in results):
        raise ValueError("summarize needs segment series (series=True)")
    r = np.concatenate([np.asarray(x["series"]["returns"], dtype=np.float64) for x in results])
    curve = np.concatenate(([1.0], np.cumprod(1.0 + r)))
    n_bars = sum(int(x["n_eval_bars"]) for x in results)
    days = sum(x["days"] for x in results)
    fold_sr = [x["metrics"]["sharpe"] for x in results if math.isfinite(x["metrics"]["sharpe"])]
    e0 = equity0 if equity0 is not None else results[0]["costs"]["equity_start"]
    fees = sum(x["costs"]["fees"] for x in results)
    try:
        p = psr_from_returns(r) if r.size >= 2 else math.nan
    except ValueError:
        p = math.nan
    states = np.concatenate([np.asarray(x["series"]["state"]) for x in results])
    pos = np.concatenate([np.asarray(x["series"]["position"]) for x in results])
    # оборот ланцюга — формула compute_metrics на зчепленій кривій: Σ_k N_k·C_k/E_k(0) / mean(E) · P / n,
    # номінал сегмента k перемасштабовано з його стартового капіталу E_k(0) на капітал ланцюга C_k
    # (для одного сегмента = оборот рушія; так само зводить OOS exp_search)
    # (у частках: C_k/E_chain = curve[s_k]/mean(curve), тож E₀ ланцюга скорочується)
    starts = np.cumsum([0] + [int(x["n_eval_bars"]) for x in results[:-1]])
    rel_notional = math.fsum(x["costs"]["traded_notional"] * curve[s].item() / x["costs"]["equity_start"]
                             for x, s in zip(results, starts, strict=True))
    turnover = rel_notional / curve.mean().item() * periods_per_year / n_bars if n_bars else math.nan
    return {
        "sharpe": sharpe_ratio(r, periods_per_year) if r.size >= 2 else math.nan,
        "sharpe_fold_mean": sum(fold_sr) / len(fold_sr) if fold_sr else math.nan,
        "total_return": curve[-1].item() - 1.0,
        "max_drawdown": max_drawdown(curve),
        "turnover": turnover,
        "fees": fees,
        "funding": sum(x["costs"]["funding"] for x in results),
        "slippage_cost": sum(x["costs"]["slippage_cost"] for x in results),
        "gross_pnl": sum(x["costs"]["gross_pnl"] for x in results),
        "n_trades": sum(int(x["metrics"]["n_trades"]) for x in results),
        "n_fills": sum(int(x["costs"]["n_fills"]) for x in results),
        # комісії за добу у відсотках початкового капіталу сегмента (кожен фолд стартує з E₀)
        "fees_per_day_pct": (100.0 * fees / (e0 * days)) if days > 0 and e0 > 0 else math.nan,
        "psr": p,
        "exposure": np.count_nonzero(pos).item() / pos.size if pos.size else math.nan,
        "days": days,
        "n_segments": len(results),
        "halted_segments": sum(1 for x in results if x["halted"]),
        "share_halted": (np.count_nonzero(states == STATE_CODE[RiskState.HALTED]).item() / states.size
                         if states.size else math.nan),
        "share_cooldown": (np.count_nonzero(states == STATE_CODE[RiskState.COOLDOWN]).item() / states.size
                           if states.size else math.nan),
    }


def compare(ref: Mapping[str, Any], other: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Наскільки `other` відрізняється від `ref` за метрикою: різниця, відношення (лише коли обидва > 0),
    зміна знака. «Завищення в k разів» має сенс лише для додатних значень — інакше звітуємо різницю."""
    a, b = ref.get(key, math.nan), other.get(key, math.nan)
    finite = math.isfinite(a) and math.isfinite(b)
    return {
        "metric": key, "reference": a, "other": b,
        "diff": b - a if finite else math.nan,
        "ratio": b / a if finite and a > 0 and b > 0 else math.nan,
        "sign_flip": bool(finite and (a > 0) != (b > 0) and a != 0 and b != 0),
    }


def neutral_v_memberships(cfg: BacktestConfig, v: float = V_NEUTRAL) -> dict[str, float]:
    """μ термів V у нейтральній точці (що «бачить» база правил, коли VolRegime вилучено)."""
    from fuzzhelm.fuzzy.membership import load_membership  # noqa: PLC0415

    return dict(load_membership(dict(cfg.trees["membership"]))["V"].fuzzify(v))


# ------------------------------------------------------------------ спільний хід експерименту з варіантами


def add_variant_args(ap: argparse.ArgumentParser, *, default_scope: str = "both") -> None:
    ap.add_argument("--scope", choices=("oos", "full", "both"), default=default_scope,
                    help="walk-forward OOS folds (profile backtest), the full window, or both")
    ap.add_argument("--selection", choices=("fixed", "is_grid"), default="fixed",
                    help="fixed params on every fold, or per-fold IS grid selection (runner.select_cell)")
    ap.add_argument("--cells", type=int, default=None,
                    help="is_grid: evenly spaced subset of the 108-cell grid (default: all; --smoke: 2)")
    ap.add_argument("--rule", choices=("front", "sharpe"), default="front",
                    help="is_grid IS selection: Pareto front + eps-constraint MaxDD <= --dd-cap (default, "
                         "the exp_search walk-forward rule) or plain argmax Sharpe (runner.select_cell)")
    ap.add_argument("--dd-cap", type=float, default=None,
                    help="is_grid front rule: MaxDD cap (default: state_machine.cool_enter, system config)")
    ap.add_argument("--risk-loop", choices=("default", "relaxed"), default="default",
                    help="relaxed = state machine / daily-loss / DD-halt thresholds moved to 0.93-0.97 "
                         "(experiment isolation only)")


@dataclass
class VariantRun:
    dataset: Dataset
    seed: int
    folds: list[Fold]
    layout: str
    variants: list[Variant]
    scopes: tuple[str, ...]
    selection: str
    cells: list[dict[str, Any]] | None
    results: dict[str, dict[str, list[dict[str, Any]]]]
    chosen: dict[str, list[dict[str, Any]]]
    choice_info: dict[str, list[dict[str, Any]]]         # правило/фронт/допустимі клітинки вибору на IS
    summaries: dict[str, dict[str, dict[str, Any]]]      # scope → variant → зведення
    passport: dict[str, Any]
    wall_s: float
    n_runs: int

    def label(self, key: str) -> str:
        return next(v.label for v in self.variants if v.key == key)


def run_variant_experiment(args: argparse.Namespace, experiment: str, argv: Sequence[str],
                           make_variants: Callable[[BacktestConfig], list[Variant]],
                           log: Callable[[str], None] = print) -> VariantRun:
    """Завантажити джерело → варіанти → фолди → прогони → зведення + паспорт (спільне для cost_models,
    ablation, hysteresis_cost)."""
    import time  # noqa: PLC0415 — заміри часу лише в скриптах

    t0 = time.perf_counter()
    seed = args.seed if args.seed is not None else profile_seed()
    ds = load_source(args)
    system_cfg = base_config(parse_params(args.params))
    cfg = apply_risk_loop(system_cfg, args.risk_loop)
    variants = make_variants(cfg)
    folds, layout = eval_folds(len(ds), cfg, smoke=args.smoke)
    scopes = ("oos", "full") if args.scope == "both" else (args.scope,)
    cells = None
    rule = getattr(args, "rule", "front")
    # dd_cap — від СИСТЕМНОЇ конфігурації (послаблений контур --risk-loop relaxed — лише ізоляція ефекту)
    dd_cap = getattr(args, "dd_cap", None)
    dd_cap = default_dd_cap(system_cfg) if dd_cap is None else dd_cap
    if args.selection == "is_grid":
        cells = select_cells(args.cells if args.cells is not None else (2 if args.smoke else None))
    pp = passport(experiment, argv, seed=seed, dataset=ds,
                  config_hashes={v.key: v.config.config_hash for v in variants},
                  extra={"fold_layout": layout, "folds": fold_dicts(folds), "scopes": list(scopes),
                         "selection": args.selection, "cells": cells, "risk_loop": args.risk_loop,
                         "is_rule": rule if args.selection == "is_grid" else None,
                         "dd_cap": dd_cap if args.selection == "is_grid" and rule == "front" else None,
                         "params": parse_params(args.params), "params_source": params_source(args.params),
                         "smoke": bool(args.smoke), "workers": args.workers, "engine": cfg.engine,
                         "warmup_bars": cfg.resolved_warmup()})
    log(f"command: {pp['command']}")
    log(f"dataset {ds.source}: {len(ds)} bars, dataset_hash {ds.dataset_hash}; git_sha {pp['git_sha']} "
        f"(code dirty={pp['git_dirty']}, any={pp.get('git_dirty_any')}); seed {seed}; folds {layout} "
        f"x{len(folds)}")
    for v in variants:
        log(f"  variant {v.key}: config_hash {v.config.config_hash}")
    if pp["params_source"].get("selected_on_oos") and args.selection == "fixed" and "oos" in scopes:
        log("  NOTE: --params were chosen on OOS metrics of the same folds (front_space=oos): the OOS "
            "summary of this config is selection-biased; --selection is_grid gives the clean estimate")
    ev = evaluate_variants(ds, variants, folds=folds, seed=seed, workers=args.workers, scopes=scopes,
                           selection=args.selection, cells=cells, rule=rule, dd_cap=dd_cap, log=log)
    res = ev["results"]
    summaries = {s: {v.key: summarize(res[v.key][s], BARS_PER_YEAR[ds.tf]) for v in variants} for s in scopes}
    n_runs = sum(len(x) for sc in res.values() for x in sc.values())
    if args.selection == "is_grid" and cells is not None:
        n_runs += len(variants) * len(folds) * len(cells)
    return VariantRun(dataset=ds, seed=seed, folds=folds, layout=layout, variants=variants, scopes=scopes,
                      selection=args.selection, cells=cells, results=res, chosen=ev["chosen"],
                      choice_info=ev["choice_info"], summaries=summaries, passport=pp,
                      wall_s=time.perf_counter() - t0, n_runs=n_runs)


def summary_rows(run: VariantRun) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for scope in run.scopes:
        for v in run.variants:
            rows.append({"variant": v.key, "label": v.label, "scope": scope, **run.summaries[scope][v.key],
                         "config_hash": v.config.config_hash, "note": v.note})
    return rows


SCOPE_TITLES = {"oos": "OOS-фолди walk-forward (зчеплено)",
                "full": "Повне вікно (для BTCUSDT містить дні 1–15 калібрування МФ — не поза-вибіркове)"}


def summary_table(run: VariantRun, scope: str, *, columns: Sequence[tuple[str, str]] | None = None) -> str:
    cols = columns or (
        ("sharpe", "Шарп (річн.)"), ("sharpe_fold_mean", "Шарп, сер. фолдів"), ("total_return", "дохідність"),
        ("max_drawdown", "MaxDD"), ("turnover", "оборот, кап./рік"), ("fees", "комісії, USDT"),
        ("funding", "фандинг, USDT"), ("slippage_cost", "ковзання ≈, USDT"), ("n_trades", "угод"),
        ("psr", "PSR"), ("halted_segments", "сегментів з HALTED"),
    )
    rows = [[v.label, *[run.summaries[scope][v.key].get(k) for k, _ in cols]] for v in run.variants]
    return md_table(["варіант", *[h for _, h in cols]], rows)


def folds_table(run: VariantRun, scope: str = "oos") -> str:
    rows = []
    for v in run.variants:
        for r in run.results[v.key].get(scope, []):
            m, c = r["metrics"], r["costs"]
            rows.append([v.label, r["meta"]["segment"], m["sharpe"], m["total_return"], m["max_drawdown"],
                         m["n_trades"], c["fees"], r["final_state"], r["equity_hash"][:12]])
    return md_table(["варіант", "сегмент", "Шарп", "дохідність", "MaxDD", "угод", "комісії, USDT",
                     "стан наприкінці", "equity_hash"], rows)


def chosen_table(run: VariantRun) -> str:
    if not run.chosen:
        return ""
    keys = list(GRID_KEYS)
    rows = []
    for v in run.variants:
        infos = run.choice_info.get(v.key, [])
        for k in range(len(run.chosen.get(v.key, []))):
            info = infos[k] if k < len(infos) else {}
            front = info.get("front")
            rows.append([v.label, k, *[run.chosen[v.key][k].get(p) for p in keys], info.get("rule"),
                         None if front is None else len(front),
                         None if info.get("feasible") is None else len(info["feasible"])])
    return md_table(["варіант", "фолд", *keys, "правило", "фронт, клітинок", "з них MaxDD ≤ dd_cap"], rows)


def fold_rows(per_variant: Mapping[str, Mapping[str, Sequence[Mapping[str, Any]]]],
              labels: Mapping[str, str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for key, scopes in per_variant.items():
        for scope, results in scopes.items():
            for r in results:
                m, c = r["metrics"], r["costs"]
                rows.append({
                    "variant": key, "label": labels.get(key, key), "scope": scope,
                    "segment": r["meta"]["segment"],
                    "fold": r["meta"]["fold"], "sharpe": m["sharpe"], "total_return": m["total_return"],
                    "max_drawdown": m["max_drawdown"], "turnover": m["turnover"], "n_trades": m["n_trades"],
                    "fees": c["fees"], "funding": c["funding"], "slippage_cost": c["slippage_cost"],
                    "psr": r["extras"].get("psr", math.nan), "final_state": r["final_state"],
                    "equity_hash": r["equity_hash"], "config_hash": r["config_hash"],
                    "dataset_hash": r["dataset_hash"],
                })
    return rows


# ====================================================================== VaR / CVaR / Купець


def active_mask(returns: FloatArray, position: npt.NDArray[Any]) -> BoolArray:
    """Бари «в позиції» для доходності r_t = E_t/E_{t−1} − 1: позиція на закритті t−1 або t ненульова,
    або капітал змінився всередині бару (вхід і стоп в одному барі, комісія). Пласка книга без виконань дає
    r_t = 0 точно, тож поза маскою лишаються лише нульові бари пласкої книги."""
    r = np.asarray(returns, dtype=np.float64)
    pos = np.asarray(position)
    if pos.size != r.size + 1:
        raise ValueError(f"position must have len(returns) + 1 points, got {pos.size} vs {r.size}")
    out: BoolArray = (pos[:-1] != 0) | (pos[1:] != 0) | (r != 0.0)
    return out


def rolling_parametric_var(returns: FloatArray, window: int = VAR_WINDOW, alpha: float = VAR_ALPHA,
                           chunk: int = 4096) -> FloatArray:
    """Прогноз на крок уперед VaR_t = z_{1−α}·σ̂(r[t−W:t])·√h, h = 1 бар, середнє = 0 (як у §5.13).

    σ̂ — вибіркове (ddof = 1) стандартне відхилення вікна. Довжина результату — max(0, n − W):
    елемент j відповідає доходності r[W + j] (та сама вирівнюваність, що й risk.var.rolling_var_breaches).
    """
    if not 0.0 < alpha < 1.0:
        raise ValueError(f"alpha must be in (0, 1), got {alpha}")
    if window < 2:
        raise ValueError("window must be >= 2 for a sample standard deviation")
    r = np.asarray(returns, dtype=np.float64)
    n = r.size - window
    if n <= 0:
        return np.empty(0, dtype=np.float64)
    z = NormalDist().inv_cdf(1.0 - alpha)
    wins = np.lib.stride_tricks.sliding_window_view(r[:-1], window)      # рядок j: r[j .. j+W−1]
    out = np.empty(n, dtype=np.float64)
    for lo in range(0, n, chunk):
        out[lo:lo + chunk] = z * wins[lo:lo + chunk].std(axis=1, ddof=1)
    return out


def count_breaches(realized: FloatArray, var: FloatArray) -> int:
    """Пробій — фактична доходність нижча за −VaR (строго): r_t < −VaR_t."""
    a, b = np.asarray(realized, dtype=np.float64), np.asarray(var, dtype=np.float64)
    if a.shape != b.shape:
        raise ValueError(f"shape mismatch {a.shape} vs {b.shape}")
    return int(np.count_nonzero(a < -b))


def _kupiec_dict(breaches: int, n: int, alpha: float) -> dict[str, Any]:
    k = kupiec_pof(breaches, n, p=alpha)
    return {"breaches": breaches, "n": n, "rate": k.p_hat, "expected": alpha * n, "lr": k.lr,
            "critical": k.critical, "reject": k.reject}


def var_block(returns: FloatArray, *, window: int = VAR_WINDOW, alpha: float = VAR_ALPHA,
              equity: float = 10_000.0) -> dict[str, Any]:
    """Оцінка VaR/CVaR на ряді доходностей (усі бари або підмножина).

    static — оцінки на всій вибірці (опис розподілу, не прогноз): історичні VaR/CVaR (risk.var), параметричний
    z·σ, частка перевищень; rolling — прогноз на крок уперед на вікні W (історичний —
    risk.var.rolling_var_breaches; параметричний — rolling_parametric_var), пробої і тест Купця (LR проти
    χ²₁(0.95)). Якщо n ≤ W — rolling = None.
    """
    r = np.asarray(returns, dtype=np.float64)
    n = r.size
    out: dict[str, Any] = {"n": n, "window": window, "alpha": alpha, "equity_ref": equity}
    if n < 2:
        out.update({"static": None, "rolling": None, "reason": f"only {n} returns"})
        return out
    g3, g4 = moments(r)
    z = NormalDist().inv_cdf(1.0 - alpha)
    sd = r.std(ddof=1).item()
    hist = historical_var_cvar(r, alpha, window=None)
    pvar = z * sd
    out["zero_share"] = np.count_nonzero(r == 0.0).item() / n
    out["moments"] = {"mean": r.mean().item(), "std": sd, "skew": g3, "kurt": g4, "excess_kurt": g4 - 3.0,
                      "min": r.min().item(), "max": r.max().item()}
    out["static"] = {
        "hist_var": hist.var, "hist_cvar": hist.cvar, "param_var": pvar, "m": hist.m,
        "hist_var_money": hist.var * equity, "hist_cvar_money": hist.cvar * equity,
        "param_var_money": pvar * equity,
        "hist_exceed_rate": np.count_nonzero(r < -hist.var).item() / n,
        "param_exceed_rate": np.count_nonzero(r < -pvar).item() / n,
        "cvar_over_param_var": hist.cvar / pvar if pvar > 0 else math.nan,
        "hist_var_over_param_var": hist.var / pvar if pvar > 0 else math.nan,
    }
    if n <= window:
        out["rolling"] = None
        out["reason"] = f"{n} returns <= window {window}: no one-step-ahead forecast can be backtested"
        return out
    hb = rolling_var_breaches(r, window, alpha)
    pv = rolling_parametric_var(r, window, alpha)
    realized = r[window:]
    pb = count_breaches(realized, pv)
    out["rolling"] = {
        "hist": {**_kupiec_dict(hb.breaches, hb.n, alpha), "var_median": np.median(hb.var_series).item(),
                 "var_zero_share": np.count_nonzero(hb.var_series <= 0.0).item() / hb.n},
        "param": {**_kupiec_dict(pb, int(realized.size), alpha), "var_median": np.median(pv).item(),
                  "var_zero_share": np.count_nonzero(pv <= 0.0).item() / pv.size},
    }
    return out


def fat_tail_conclusion(block: Mapping[str, Any], name: str) -> list[str]:
    """Висновок українською, побудований лише з виміряних чисел блоку (жодних заготовлених тверджень)."""
    if not block.get("static"):
        return [f"{name}: вибірка замала для оцінки ({block.get('reason', '')})."]
    mom, st = block["moments"], block["static"]
    lines = [f"{name}: n = {block['n']}, частка нульових доходностей {block['zero_share']:.1%}, асиметрія "
             f"{mom['skew']:.3g}, куртозис {mom['kurt']:.4g} (нормальний = 3)."]
    if mom["kurt"] > 3.0:
        lines.append(f"Хвости товщі за нормальні (надлишковий куртозис {mom['excess_kurt']:.4g}); "
                     f"CVaR₉₅/VaR₉₅(парам.) = {fmt(st['cvar_over_param_var'], 3)}, "
                     f"VaR₉₅(іст.)/VaR₉₅(парам.) = {fmt(st['hist_var_over_param_var'], 3)}.")
    else:
        lines.append(f"Надлишкового куртозису немає ({mom['kurt']:.4g} ≤ 3): товсті хвости на цій вибірці "
                     "не виявлено.")
    if st["cvar_over_param_var"] > 1.0 and math.isfinite(st["cvar_over_param_var"]):
        lines.append("Середній збиток у 5 % найгірших барів більший за параметричний VaR₉₅ — нормальна "
                     "модель занижує хвостовий ризик.")
    roll = block.get("rolling")
    if roll:
        h, p = roll["hist"], roll["param"]

        def side(k: Mapping[str, Any]) -> str:
            if k["rate"] < block["alpha"]:
                return "пробоїв менше за номінал — оцінка надто консервативна"
            if k["rate"] > block["alpha"]:
                return "пробоїв більше за номінал — ризик занижено"
            return "частка дорівнює номіналу"

        lines.append(
            f"Прогноз на крок уперед (W = {block['window']}): історичний — {h['breaches']} пробоїв "
            f"із {h['n']} (частка {h['rate']:.4f}, LR = {h['lr']:.3f}, "
            f"{'ВІДКИНУТО' if h['reject'] else 'не відкинуто'}); "
            f"параметричний — {p['breaches']} із {p['n']} (частка {p['rate']:.4f}, LR = {p['lr']:.3f}, "
            f"{'ВІДКИНУТО' if p['reject'] else 'не відкинуто'}) при χ²₁(0.95) = {CHI2_1_95:.3f}; "
            f"історичний: {side(h)}; параметричний: {side(p)}.")
        if p["rate"] > block["alpha"] and p["rate"] > h["rate"]:
            lines.append("Параметричний VaR пробивається частіше за номінальні 5 % і частіше за історичний — "
                         "занижує ризик.")
        if h["var_zero_share"] > 0.5:
            lines.append(f"У {h['var_zero_share']:.1%} вікон історичний VaR₉₅ ≤ 0: вікно переважно з "
                         "нульових доходностей пласкої книги, тож будь-який збитковий бар — «пробій»; "
                         "результат тесту Купця на всіх барах описує частку часу в ринку, а не якість "
                         "моделі.")
    else:
        lines.append(f"Бектест прогнозу неможливий: {block.get('reason', '')}.")
    return lines


# ====================================================================== гістерезис


def band_stats(u_final: FloatArray, enter: float, exit_: float) -> dict[str, float]:
    """Як часто сигнал живе біля порогу: частка рішень у смузі exit ≤ |u| < enter (тут петля тримає стан,
    а компаратор без петлі — ні) і число перетинів порогу |u| = enter (кожен перетин без петлі — вхід або
    вихід)."""
    u = np.asarray(u_final, dtype=np.float64)
    u = u[np.isfinite(u)]
    if u.size == 0:
        return {"n_decisions": 0, "share_in_band": math.nan, "share_above_enter": math.nan,
                "enter_crossings": 0, "crossings_per_day": math.nan}
    a = np.abs(u)
    above = a >= enter
    crossings = int(np.count_nonzero(above[1:] != above[:-1]))
    return {
        "n_decisions": int(u.size),
        "share_in_band": np.count_nonzero((a >= exit_) & (a < enter)).item() / u.size,
        "share_above_enter": np.count_nonzero(above).item() / u.size,
        "enter_crossings": crossings,
        "crossings_per_day": crossings / (u.size / 1440.0),
    }


def churn_cost_pct_per_day(fee_rate: float, fills_per_bar: float, notional_over_equity: float,
                           bars_per_day: int = 1440) -> float:
    """Аналітика §5.9 у відсотках капіталу за добу: bars_per_day · fills_per_bar · τ · N/E · 100.
    Брифінг: 1440 × 2 × 0.0004 при N/E = 1 = 115.2 %/добу (а не 1.15 %, docs/deviations.d/risk.md R-03)."""
    return 100.0 * bars_per_day * fills_per_bar * fee_rate * notional_over_equity


# ====================================================================== таблиці й вивід


def fmt(x: Any, digits: int = 4) -> str:
    """Число → рядок для markdown: None/NaN → «—», ±inf → «∞», тисячі — з вузьким пробілом."""
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "так" if x else "ні"
    if isinstance(x, str):
        return x
    if isinstance(x, Decimal):
        x = to_float(x)
    if isinstance(x, np.generic):
        x = x.item()
    if isinstance(x, int):
        return f"{x:,}".replace(",", " ") if abs(x) >= 10_000 else str(x)
    if isinstance(x, float):
        x += 0.0                 # −0.0 (VaR = −r_(m) при r_(m) = 0) → 0.0
        if math.isnan(x):
            return "—"
        if math.isinf(x):
            return "∞" if x > 0 else "−∞"
        if x != 0 and (abs(x) >= 1e5 or abs(x) < 1e-4):
            return f"{x:.{digits - 1}e}"
        if abs(x) >= 1000:
            return f"{x:,.1f}".replace(",", " ")
        return f"{x:.{digits}g}" if abs(x) < 1 else f"{x:.{max(1, digits - 1)}f}".rstrip("0").rstrip(".")
    return str(x)


def pct(x: Any, digits: int = 2) -> str:
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return fmt(x)
    return f"{100.0 * x:.{digits}f} %"


def md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    def cell(v: Any) -> str:
        return fmt(v).replace("|", "\\|").replace("\n", " ")
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    lines += ["| " + " | ".join(cell(v) for v in r) + " |" for r in rows]
    return "\n".join(lines)


def sanitize(obj: Any) -> Any:
    """Строгий JSON: NaN → None, ±inf → "inf"/"-inf", numpy → Python, Decimal → рядок."""
    if isinstance(obj, Mapping):
        return {str(k): sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [sanitize(v) for v in obj]
    if isinstance(obj, np.ndarray):
        return sanitize(obj.tolist())
    if isinstance(obj, np.generic):
        return sanitize(obj.item())
    if isinstance(obj, bool) or obj is None or isinstance(obj, int | str):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj):
            return None
        if math.isinf(obj):
            return "inf" if obj > 0 else "-inf"
        return obj
    if isinstance(obj, Decimal | Path | UUID):
        return str(obj)
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return sanitize(dataclasses.asdict(obj))
    if hasattr(obj, "value"):          # Enum
        return sanitize(obj.value)
    return str(obj)


def command_line(argv: Sequence[str]) -> str:
    return shlex.join(["uv", "run", "python", *argv])


# Виводи експериментів (artifacts/, docs/report_tables, docs/figures) — не код: їх незакомічена поява не
# робить прогін «з незакоміченого коду». Та сама домовленість, що в паспортах exp_search (GIT_DIRTY_IGNORED).
GIT_DIRTY_IGNORED: tuple[str, ...] = ("artifacts", "docs")


def git_state(repo: Path = ROOT) -> dict[str, Any]:
    """sha HEAD; dirty — зміни коду (поза GIT_DIRTY_IGNORED); dirty_any — сирий `git status --porcelain`
    (як manifest.read_git_sha); dirty_paths — перші 20 рядків змін коду."""
    sha, dirty_any = read_git_sha(repo)
    out: dict[str, Any] = {"sha": sha, "dirty": dirty_any, "dirty_any": dirty_any, "dirty_paths": []}
    if sha is None:
        return out
    try:
        st = subprocess.run(["git", "status", "--porcelain", "--", ".",
                             *(f":(exclude){p}" for p in GIT_DIRTY_IGNORED)],
                            cwd=repo, capture_output=True, text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return out                      # не вдалось звузити — лишається сирий (суворіший) прапорець
    lines = [ln for ln in st.splitlines() if ln.strip()]
    out.update(dirty=bool(lines), dirty_paths=lines[:20])
    return out


def passport(experiment: str, argv: Sequence[str], *, seed: int, dataset: Dataset | None = None,
             config_hashes: Mapping[str, str] | None = None, extra: Mapping[str, Any] | None = None,
             repo: Path = ROOT) -> dict[str, Any]:
    """Паспорт виводу: точна команда, git_sha + dirty, seed, dataset_hash, config_hash кожного варіанта.

    git_dirty — незакомічені зміни КОДУ (поза artifacts/ і docs/: виводи попередніх кроків хвилі туди
    пишуться і не мають робити наступні прогони «брудними»); git_dirty_any — сирий прапорець,
    git_dirty_paths — що саме.
    """
    gs = git_state(repo)
    out: dict[str, Any] = {"experiment": experiment, "command": command_line(argv), "git_sha": gs["sha"],
                           "git_dirty": gs["dirty"], "git_dirty_any": gs["dirty_any"],
                           "git_dirty_paths": gs["dirty_paths"], "seed": seed}
    if dataset is not None:
        out.update({"dataset_hash": dataset.dataset_hash, "source": dataset.source,
                    "symbol": dataset.instrument.symbol_venue, "tf": dataset.tf, "n_bars": len(dataset),
                    "t_first_ns": dataset.t_ns[0].item(), "t_last_ns": dataset.t_ns[-1].item()})
    if config_hashes:
        out["config_hashes"] = dict(config_hashes)
    if extra:
        out.update(extra)
    return out


def passport_md(p: Mapping[str, Any]) -> str:
    dirty = p.get("git_dirty")
    paths = p.get("git_dirty_paths") or []
    lines = [
        f"Команда: `{p.get('command')}`",
        "",
        f"* git_sha: `{p.get('git_sha')}`"
        + (" — **код мав незакомічені зміни**" + (f" ({'; '.join(paths[:3])}{' …' if len(paths) > 3 else ''})"
                                                   if paths else "") if dirty else ""),
        f"* seed: {p.get('seed')}",
    ]
    if p.get("dataset_hash"):
        where = (f" ({p.get('source')}, {p.get('n_bars')} барів)" if p.get("n_bars") is not None else "")
        lines.append(f"* dataset_hash: `{p['dataset_hash']}`{where}")
    for k, v in (p.get("config_hashes") or {}).items():
        lines.append(f"* config_hash `{k}`: `{v}`")
    return "\n".join(lines)


def write_outputs(out_dir: Path, stem: str, *, result: Mapping[str, Any], md: str,
                  rows: Sequence[Mapping[str, Any]] | None = None) -> list[Path]:
    """<stem>.json (строгий JSON) + <stem>.csv (рядки, якщо є) + <stem>.md; повертає шляхи."""
    out_dir.mkdir(parents=True, exist_ok=True)
    paths: list[Path] = []
    pj = out_dir / f"{stem}.json"
    pj.write_text(json.dumps(sanitize(result), ensure_ascii=False, indent=1, allow_nan=False) + "\n",
                  encoding="utf-8")
    paths.append(pj)
    if rows:
        pc = out_dir / f"{stem}.csv"
        keys: list[str] = []
        for r in rows:
            keys += [k for k in r if k not in keys]
        with pc.open("w", encoding="utf-8", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for r in rows:
                w.writerow({k: _csv_cell(r.get(k)) for k in keys})
        paths.append(pc)
    pm = out_dir / f"{stem}.md"
    pm.write_text(md.rstrip() + "\n", encoding="utf-8")
    paths.append(pm)
    return paths


def _csv_cell(v: Any) -> Any:
    s = sanitize(v)
    if isinstance(s, dict | list):
        return json.dumps(s, ensure_ascii=False)
    return "" if s is None else s


# ====================================================================== брифінг, тести, трасування


@dataclass(frozen=True)
class BriefGroup:
    letter: str
    title: str
    declared: int
    names: tuple[str, ...]


_GROUP_RE = re.compile(r"^### ([A-N])\. (.+?) \((\d+)", re.MULTILINE)
_TEST_RE = re.compile(r"`(test_[A-Za-z0-9_]+)`")


def brief_section(text: str, number: int) -> str:
    """Текст розділу `## {number}.` брифінгу до наступного заголовка другого рівня."""
    m = re.search(rf"^## {number}\. .*$", text, re.MULTILINE)
    if m is None:
        raise ValueError(f"brief section {number} not found")
    nxt = re.search(r"^## \d+\. ", text[m.end():], re.MULTILINE)
    return text[m.start(): m.end() + (nxt.start() if nxt else len(text) - m.end())]


def parse_brief_test_groups(brief_text: str) -> list[BriefGroup]:
    """Групи A–N §10 брифінгу: заявлена кількість із заголовка і дослівні назви тест-функцій."""
    sec = brief_section(brief_text, 10)
    heads = list(_GROUP_RE.finditer(sec))
    groups: list[BriefGroup] = []
    for i, h in enumerate(heads):
        body = sec[h.end(): heads[i + 1].start() if i + 1 < len(heads) else len(sec)]
        # тіло групи закінчується перед «Приймальне тестування» (після N) — там тестів немає
        names = tuple(dict.fromkeys(_TEST_RE.findall(body)))
        groups.append(BriefGroup(h.group(1), h.group(2).strip(), int(h.group(3)), names))
    return groups


def test_function_name(nodeid: str) -> str:
    return nodeid.rsplit("::", 1)[-1].split("[", 1)[0]


def parse_collected(output: str) -> list[str]:
    """Вузли `pytest --collect-only -q` (рядки з «::»)."""
    return [ln.strip() for ln in output.splitlines() if "::" in ln and not ln.startswith(" ")]


def test_group_rows(groups: Sequence[BriefGroup], nodeids: Sequence[str]) -> list[dict[str, Any]]:
    """Зведення по групах: дослівні назви брифінгу, які є серед зібраних (з кількістю вузлів — параметризація
    розгортає одну функцію в кілька кейсів), відсутні, і всі вузли файлів, «прив'язаних» до групи (файл
    належить групі, чиїх дослівних тестів у ньому найбільше; евристика, так і позначено у звіті)."""
    by_name: dict[str, list[str]] = {}
    for nid in nodeids:
        by_name.setdefault(test_function_name(nid), []).append(nid)
    group_of_name = {n: g.letter for g in groups for n in g.names}
    votes: dict[str, dict[str, int]] = {}
    for nid in nodeids:
        letter = group_of_name.get(test_function_name(nid))
        if letter is not None:
            f = nid.split("::", 1)[0]
            votes.setdefault(f, {}).setdefault(letter, 0)
            votes[f][letter] += 1
    order = {g.letter: i for i, g in enumerate(groups)}
    file_group = {f: min(v, key=lambda g: (-v[g], order[g])) for f, v in votes.items()}
    rows: list[dict[str, Any]] = []
    for g in groups:
        present = [n for n in g.names if n in by_name]
        missing = [n for n in g.names if n not in by_name]
        items = sum(len(by_name[n]) for n in present)
        files = sorted(f for f, grp in file_group.items() if grp == g.letter)
        file_items = sum(1 for nid in nodeids if nid.split("::", 1)[0] in files)
        rows.append({"group": g.letter, "title": g.title, "declared": g.declared, "named": len(g.names),
                     "named_present": len(present), "named_items": items, "missing": missing,
                     "files": files, "file_items": file_items})
    return rows


_TABLE_LINE = re.compile(r"^\|.*\|\s*$")


def extract_md_tables(text: str, heading: str | None = None) -> list[str]:
    """Усі markdown-таблиці тексту (або лише розділу, заголовок якого містить `heading`)."""
    if heading is not None:
        lines = text.splitlines()
        start = next((i for i, ln in enumerate(lines) if ln.startswith("#") and heading in ln), None)
        if start is None:
            return []
        level = len(lines[start]) - len(lines[start].lstrip("#"))
        end = next((j for j in range(start + 1, len(lines))
                    if lines[j].startswith("#") and len(lines[j]) - len(lines[j].lstrip("#")) <= level),
                   len(lines))
        text = "\n".join(lines[start:end])
    tables: list[str] = []
    cur: list[str] = []
    for ln in [*text.splitlines(), ""]:
        if _TABLE_LINE.match(ln):
            cur.append(ln.rstrip())
        elif cur:
            if len(cur) >= 2:
                tables.append("\n".join(cur))
            cur = []
    return tables


METRIC_LABELS_UK: dict[str, str] = {
    "total_return": "Загальна дохідність", "cagr": "CAGR", "ann_vol": "Волатильність (річна)",
    "sharpe": "Шарп (річний)", "sortino": "Сортіно", "max_drawdown": "Макс. просадка", "calmar": "Калмар",
    "ulcer_index": "Ulcer index", "profit_factor": "Profit factor", "expectancy": "Очікування угоди, USDT",
    "win_rate": "Частка прибуткових", "avg_win": "Сер. виграш, USDT", "avg_loss": "Сер. програш, USDT",
    "n_trades": "Угод", "turnover": "Оборот (капіталів/рік)", "exposure": "Частка часу в ринку",
    "tail_ratio": "Tail ratio", "psr": "PSR (SR* = 0)", "dsr": "DSR (SR* = SR₀ за N прогонами сітки)",
}


def sr0_expected_max(trial_srs: Sequence[float | None]) -> float:
    """SR₀ = √Var(SR_i)·[(1−γ_E)·Φ⁻¹(1−1/N) + γ_E·Φ⁻¹(1−1/(N·e))] (§5.15): N — УСІ прогони сітки (фактичне
    число випробувань, зокрема з невизначеним Шарпом), Var — вибіркова (ddof = 1) лише по скінченних Шарпах.
    Та сама домовленість, що в exp_search; для скінченних Шарпів = metrics.expected_max_sr."""
    n = len(trial_srs)
    fin = np.asarray([x for x in trial_srs if x is not None and math.isfinite(x)], dtype=np.float64)
    if n < 2 or fin.size < 2:
        return 0.0
    nd = NormalDist()
    sd = math.sqrt(np.var(fin, ddof=1).item())
    return sd * ((1.0 - EULER_GAMMA) * nd.inv_cdf(1.0 - 1.0 / n)
                 + EULER_GAMMA * nd.inv_cdf(1.0 - 1.0 / (n * math.e)))


def dsr_for(metrics: Mapping[str, Any], trial_srs: Sequence[float | None]) -> dict[str, Any]:
    """DSR прогону (Шарп за період sr_period, n_obs, skew, kurt — доповнення рушія) за фактичними N прогонами
    сітки (Шарпи за період). Бракує чогось → значення None і причина (жодних підставлених чисел)."""
    need = ("sr_period", "n_obs", "skew", "kurt")
    missing = [k for k in need if metrics.get(k) is None]
    n_trials = len(trial_srs)
    n_fin = sum(1 for x in trial_srs if x is not None and math.isfinite(x))
    base = {"n_trials": n_trials, "n_finite": n_fin}
    if missing:
        return {**base, "dsr": None, "sr0": None, "reason": f"run metrics lack {missing}"}
    if n_fin < 2:
        return {**base, "dsr": None, "sr0": None, "reason": "fewer than 2 grid runs with a finite Sharpe"}
    sr0 = sr0_expected_max(trial_srs)
    try:
        val = psr(metrics["sr_period"], int(metrics["n_obs"]), metrics["skew"], metrics["kurt"], sr_star=sr0)
    except ValueError as e:
        return {**base, "dsr": None, "sr0": sr0, "reason": str(e)}
    return {**base, "dsr": val, "sr0": sr0, "reason": ""}


def pick_trial_group(rows: Sequence[Mapping[str, Any]], dataset_hash: str, *, git_sha: str | None = None,
                     engine: str | None = None) -> tuple[list[float | None], dict[str, Any] | None]:
    """Прогони сітки для DSR: рядки `grid_cell` (dataset_hash, git_sha, seed, engine, sr_period, started_at)
    ТОГО САМОГО набору, згруповані в «одну сітку» за (git_sha, seed, engine) — інакше повторний запуск сітки
    (інший коміт, інший рушій) подвоїв би N і змішав би випробування. Група — з тим самим git_sha і рушієм,
    що й оцінюваний прогін, якщо така є; інакше найновіша (за started_at) з тим самим рушієм; інакше
    найновіша. Повертає (Шарпи за період, опис групи) або ([], None)."""
    groups: dict[tuple[Any, Any, Any], list[Mapping[str, Any]]] = {}
    for r in rows:
        if r.get("dataset_hash") == dataset_hash:
            groups.setdefault((r.get("git_sha"), r.get("seed"), r.get("engine")), []).append(r)
    if not groups:
        return [], None

    def latest(key: tuple[Any, Any, Any]) -> str:
        return max(str(r.get("started_at") or "") for r in groups[key])

    keys = sorted(groups, key=lambda k: (latest(k), str(k)), reverse=True)
    pick = (next((k for k in keys if k[0] == git_sha and k[2] == engine), None)
            or next((k for k in keys if engine is not None and k[2] == engine), None) or keys[0])
    rows_g = groups[pick]
    return ([r.get("sr_period") for r in rows_g],
            {"git_sha": pick[0], "seed": pick[1], "engine": pick[2], "n": len(rows_g), "groups": len(groups)})


_DEV_RE = re.compile(r"^## ([A-Z]+-\d+[a-z]?)\.\s+(.+?)\s*$", re.MULTILINE)


def parse_deviation_titles(text: str, source: str) -> list[dict[str, str]]:
    return [{"id": m.group(1), "title": m.group(2), "source": source} for m in _DEV_RE.finditer(text)]


# Таблиця трасування брифінгу §2: (№, фрагмент, модулі, кандидати-артефакти) — шляхи від кореня репозиторію.
# Артефакт підтверджується лише, якщо файл існує (або рядок БД / результат експерименту передано в
# export_report_tables); відсутнє → маркер <<TBD:…>>.
TRACE_ROWS: tuple[tuple[int, str, tuple[str, ...], tuple[str, ...]], ...] = (
    (1, "агрегації біржових даних",
     ("src/fuzzhelm/ingest/rest_client.py", "src/fuzzhelm/ingest/kraken_client.py",
      "src/fuzzhelm/ingest/crosscheck.py"),
     ("docs/figures/crosscheck_report.md", "docs/figures/crosscheck_divergence.png",
      "data/crosscheck_input.json.gz")),
    (2, "автоматизації торговельних операцій",
     ("src/fuzzhelm/execution/router.py", "src/fuzzhelm/execution/testnet_venue.py"),
     ("scripts/testnet_one_order.py",)),
    (3, "із модулем ризик-менеджменту",
     ("src/fuzzhelm/risk/guard.py", "src/fuzzhelm/risk/state.py", "src/fuzzhelm/risk/journal.py",
      "src/fuzzhelm/risk/rules"),
     ("docs/figures/riskfix_cooldown_policy.md", "docs/figures/first_run_metrics.md")),
    (4, "REST … для збору",
     ("src/fuzzhelm/ingest/rest_client.py", "src/fuzzhelm/ingest/backfill.py",
      "src/fuzzhelm/ingest/ratelimit.py"),
     ("docs/figures/backfill_report.md", "data/dataset_window.json")),
    (5, "WebSocket … у реальному часі",
     ("src/fuzzhelm/ingest/ws_client.py", "src/fuzzhelm/ingest/reconnect.py"),
     ("fixtures/ws/btcusdt_2026-09-18.jsonl.gz", "fixtures/ws/pathological",
      "docs/figures/ingest_pathological.md")),
    (6, "нормалізації",
     ("src/fuzzhelm/ingest/normalize.py", "src/fuzzhelm/ingest/quantize.py", "src/fuzzhelm/ingest/dedup.py"),
     ("docs/field_mapping.md",)),
    (7, "збереження",
     ("src/fuzzhelm/storage/models.py", "src/fuzzhelm/storage/repositories", "alembic/versions"),
     ("docs/db_schema.md", "alembic/versions")),
    (8, "backtesting на історичних даних",
     ("src/fuzzhelm/backtest/engine.py", "src/fuzzhelm/backtest/walkforward.py",
      "src/fuzzhelm/backtest/metrics.py"),
     ("docs/figures/first_run_metrics.md",)),
    (9, "рішень за заданими алгоритмічними правилами",
     ("src/fuzzhelm/fuzzy/mamdani.py", "src/fuzzhelm/decision/core.py", "src/fuzzhelm/detectors/registry.py"),
     ("config/rules_mamdani.yaml", "docs/figures/fuzzy_rules_table.md", "src/fuzzhelm/api/explain.py",
      "docs/figures/fuzzy_control_surface.png")),
    (10, "автоматичного контролю ризиків і лімітів",
     ("src/fuzzhelm/risk/verdict.py", "src/fuzzhelm/risk/state.py", "src/fuzzhelm/sizing/sizer.py"),
     ("docs/figures/riskfix_cooldown_policy.md",)),
    (11, "покрити ключову логіку модульним тестуванням",
     ("tests",),
     ()),
)
TRACE_BRIEF_CLAIMS: dict[int, str] = {
    1: "два джерела, крос-звірка, таблиця розбіжностей",
    2: "один реальний ордер на testnet зі скріншотом",
    3: "6 лімітів, автомат, журнал risk_event",
    4: "45 днів свічок у БД, token-bucket",
    5: "записана сесія, 6 патологічних сценаріїв",
    6: "таблиця мапінгу полів двох бірж",
    7: "12 таблиць, 3 рівні моделі БД",
    8: "walk-forward 6 фолдів, 17 метрик",
    9: "45 правил у YAML, CRUD через UI, /explain",
    10: "statechart, інваріант монотонності",
    11: "92 кейси, вивід pytest --cov",
}


# ====================================================================== рисунки (matplotlib Agg, лише 2D)


STATE_COLORS = {"NORMAL": "#0ca30c", "WARNING": "#fab219", "COOLDOWN": "#ec835a", "HALTED": "#d03b3b"}
_INK, _INK2, _GRID, _SURFACE, _SERIES = "#0b0b0b", "#52514e", "#e4e3dd", "#fcfcfb", "#2a78d6"


def _plt() -> Any:
    import matplotlib  # noqa: PLC0415 — рисунки лише в скриптах, воркери matplotlib не імпортують

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": _SURFACE,
                         "axes.facecolor": _SURFACE, "axes.edgecolor": _GRID, "axes.labelcolor": _INK2,
                         "xtick.color": _INK2, "ytick.color": _INK2, "font.size": 9})
    return plt


def state_spans(states: Sequence[str]) -> list[tuple[int, int, str]]:
    """Суцільні відрізки однакового стану: (перший, останній, стан)."""
    spans: list[tuple[int, int, str]] = []
    start = 0
    for i in range(1, len(states) + 1):
        if i == len(states) or states[i] != states[start]:
            spans.append((start, i - 1, states[start]))
            start = i
    return spans


def plot_equity_figure(path: Path, ts_ns: Sequence[int], equity: Sequence[float], *,
                       states: Sequence[str] | None = None, title: str = "",
                       boundaries: Sequence[int] = ()) -> Path:
    """Капітал + просадка + смуги режимів ризику (NORMAL/WARNING/COOLDOWN/HALTED); межі фолдів — вертикалі."""
    from datetime import UTC, datetime  # noqa: PLC0415

    plt = _plt()
    import matplotlib.dates as mdates  # noqa: PLC0415
    from matplotlib.patches import Patch  # noqa: PLC0415

    eq = np.asarray(equity, dtype=np.float64)
    if eq.size == 0:
        raise ValueError("empty equity curve")
    t = [datetime.fromtimestamp(x / 1e9, tz=UTC) for x in ts_ns]
    dd = -100.0 * (1.0 - eq / np.maximum.accumulate(eq))
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 6.2), sharex=True, layout="constrained",
                                   gridspec_kw={"height_ratios": [3, 1.3]})
    spans = state_spans(list(states)) if states is not None else []
    for ax in (ax1, ax2):
        for a, b, st in spans:
            ax.axvspan(t[a], t[b], color=STATE_COLORS.get(st, "#cccccc"), alpha=0.13, lw=0)
        for i in boundaries:
            if 0 <= i < len(t):
                ax.axvline(t[i], color=_INK2, lw=0.6, ls="--")
        ax.grid(axis="y", color=_GRID, lw=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    ax1.plot(t, eq, color=_SERIES, lw=1.3)
    ax1.set_ylabel("Капітал, USDT")
    ax1.set_title(title, loc="left", color=_INK, fontsize=11)
    ax2.fill_between(t, dd, 0, color=_SERIES, alpha=0.18, lw=0)
    ax2.plot(t, dd, color=_SERIES, lw=1.0)
    ax2.set_ylabel("Просадка, %")
    ax2.set_xlabel("Дата (UTC)")
    short = len(ts_ns) > 1 and (ts_ns[-1] - ts_ns[0]) < 3 * 86_400 * 10**9
    fmt_date = mdates.DateFormatter("%d.%m %H:%M" if short else "%d.%m")  # type: ignore[no-untyped-call]
    ax2.xaxis.set_major_formatter(fmt_date)
    present = [s for s in STATE_COLORS if any(sp[2] == s for sp in spans)]
    if present:
        ax1.legend(handles=[Patch(color=STATE_COLORS[s], alpha=0.35, label=s) for s in present],
                   title="Режим ризику", loc="upper right", frameon=False, fontsize=8, title_fontsize=8)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def plot_var_histogram(path: Path, panels: Sequence[Mapping[str, Any]], *, title: str = "") -> Path:
    """Гістограми доходностей (у %) з вертикалями −VaR/−CVaR; вісь частот — логарифмічна (пік нулів
    пласкої книги інакше сховав би хвости). panel = {title, returns, lines: {підпис: частка капіталу}}."""
    plt = _plt()
    fig, axes = plt.subplots(1, len(panels), figsize=(6.2 * len(panels), 4.2), layout="constrained",
                             squeeze=False)
    styles = [("#d03b3b", "-"), ("#8c1c13", "--"), ("#2a78d6", "-."), ("#52514e", ":")]
    for ax, panel in zip(axes[0], panels, strict=True):
        r = 100.0 * np.asarray(panel["returns"], dtype=np.float64)
        if r.size:
            ax.hist(r, bins=max(20, min(200, int(2.0 * math.sqrt(r.size)))), color=_SERIES, alpha=0.75,
                    log=True)
        for (label, value), (color, ls) in zip((panel.get("lines") or {}).items(), styles, strict=False):
            if value is not None and math.isfinite(value):
                ax.axvline(-100.0 * value, color=color, ls=ls, lw=1.2,
                           label=f"{label} = {100.0 * value + 0.0:.4f} %")
        ax.set_title(str(panel.get("title", "")), loc="left", color=_INK, fontsize=10)
        ax.set_xlabel("Доходність за бар, % (вертикалі — на −VaR і −CVaR)")
        ax.set_ylabel("Кількість барів (лог. шкала)")
        ax.grid(axis="y", color=_GRID, lw=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
        ax.legend(loc="upper left", frameon=True, framealpha=0.9, edgecolor=_GRID, facecolor=_SURFACE,
                  fontsize=7)
    if title:
        fig.suptitle(title, x=0.01, ha="left", color=_INK, fontsize=11)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


# ====================================================================== джерела даних і спільні аргументи


DEFAULT_FIXTURE = ROOT / "fixtures" / "rest" / "binance_klines.json.gz"


def add_common_args(ap: argparse.ArgumentParser, experiment: str) -> None:
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--db", action="store_true",
                     help="45-day window from PostgreSQL (data/dataset_window.json, hash-checked) + funding")
    src.add_argument("--fixture", nargs="?", const=str(DEFAULT_FIXTURE), default=None, metavar="PATH",
                     help="offline klines fixture (default fixtures/rest/binance_klines.json.gz; BTCUSDT)")
    ap.add_argument("--database-url", default=None, help="default: FUZZHELM_DATABASE_URL / Settings")
    ap.add_argument("--symbol", default="BTCUSDT", choices=("BTCUSDT", "ETHUSDT"))
    ap.add_argument("--seed", type=int, default=None, help="default: seed of config/profiles/backtest.yaml")
    ap.add_argument("--workers", type=int, default=0, help="ProcessPoolExecutor size (0 = in-process)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT / experiment, help="output directory")
    ap.add_argument("--smoke", action="store_true",
                    help="small inputs: first --smoke-days of the DB window, 2-fold layout, few grid cells")
    ap.add_argument("--smoke-days", type=float, default=3.0)
    ap.add_argument("--params", default=None,
                    help="config overrides: JSON object or @file.json (keys: " + ", ".join(PARAM_KEYS) + ")")


def load_source(args: argparse.Namespace) -> Dataset:
    """Dataset з аргументів: --db (вікно БД) або фікстура (типово); --smoke обрізає до перших N днів."""
    if args.db:
        ds = asyncio.run(load_db_dataset(args.symbol, args.database_url))
    else:
        if args.symbol != "BTCUSDT":
            raise SystemExit("the klines fixture holds BTCUSDT only; use --db for ETHUSDT")
        ds = load_klines_json(args.fixture or DEFAULT_FIXTURE, load_exchange_instrument(args.symbol))
    if args.smoke and args.db:
        n = min(len(ds), int(args.smoke_days * 1440))
        ds = ds.slice(0, n)
    return ds


async def load_db_dataset(symbol: str, database_url: str | None = None) -> Dataset:  # pragma: no cover — БД
    """Вікно data/dataset_window.json з БД (лише читання, роль застосунку) + фандинг; хеш свічок звірено."""
    from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415
    from fuzzhelm.ingest.funding import load_funding_json  # noqa: PLC0415
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.repositories import CandleRepo, InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415

    win = json.loads((ROOT / "data" / "dataset_window.json").read_text(encoding="utf-8"))
    meta = win["symbols"][symbol]
    lo, hi = win["window"]["start_ms"] * 1_000_000, win["window"]["end_ms"] * 1_000_000
    engine = make_engine(database_url, role=APP_ROLE, null_pool=True)
    try:
        async with session_scope(session_factory(engine)) as s:
            row = await InstrumentRepo(s).get_by_canon(meta["symbol_canon"])
            if row is None:
                raise LookupError(f"instrument {meta['symbol_canon']!r} is not in the database")
            arr = await CandleRepo(s).load_arrays(row.id, "1m", lo, hi)
    finally:
        await engine.dispose()
    if dataset_hash(arr.columns()) != meta["dataset_hash"]:
        raise ValueError(f"{symbol}: candles in the DB differ from data/dataset_window.json")
    inst = row.to_dto()
    fpath = ROOT / "data" / f"funding_{symbol}.json"
    rates = load_funding_json(fpath, inst).rates if fpath.exists() else None
    return Dataset.from_candle_arrays(arr, inst, funding=rates, source=f"db:{symbol}")


async def read_run(session: Any, run_id: UUID) -> dict[str, Any]:  # pragma: no cover — БД
    """Паспорт, метрики і крива капіталу прогону в межах уже відкритої сесії."""
    from fuzzhelm.storage.repositories import EquityRepo, RunRepo  # noqa: PLC0415

    run = await RunRepo(session).get(run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    metrics = await RunRepo(session).get_metrics(run_id)
    curve = await EquityRepo(session).curve(run_id)
    return {"run": run, "metrics": metrics, "curve": curve}


async def load_db_run(run_id: UUID, database_url: str | None = None) -> dict[str, Any]:  # pragma: no cover
    """Паспорт, метрики і крива капіталу збереженого прогону (лише читання)."""
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415

    engine = make_engine(database_url, role=APP_ROLE, null_pool=True)
    try:
        async with session_scope(session_factory(engine)) as s:
            return await read_run(s, run_id)
    finally:
        await engine.dispose()


def first_decision_index(n_points: int, metrics: Mapping[str, Any], default: int) -> tuple[int, str]:
    """Індекс точки кривої, з якої рушій рахував метрики (decide_from) для збереженого прогону.

    Рушій рахує n_obs = len(E) − decide_from − 1 доходностей (engine._metrics), тож decide_from =
    n_points − 1 − n_obs точно, якщо run_metric має n_obs; інакше — `default` (прогрів типової конфігурації
    − 1) з позначкою. Повертає (індекс, звідки)."""
    n_obs = metrics.get("n_obs")
    if n_obs is not None and math.isfinite(n_obs):
        idx = n_points - 1 - int(n_obs)
        if 0 <= idx < n_points:
            return idx, "run_metric.n_obs"
    return max(0, min(default, n_points - 1)), "default warm-up (run_metric.n_obs missing)"


def series_from_curve(curve: Sequence[Any]) -> dict[str, Any]:
    """Рядки equity_point → ряди (ts, капітал, позиція за gross_exposure > 0, стан)."""
    eq = np.array([to_float(p.equity) if p.equity is not None else math.nan for p in curve], dtype=np.float64)
    pos = np.array([1 if (p.gross_exposure is not None and p.gross_exposure > 0) else 0 for p in curve],
                   dtype=np.int8)
    states = [str(p.risk_state) if p.risk_state is not None else "NORMAL" for p in curve]
    return {"ts_ns": np.array([p.ts_ns for p in curve], dtype=np.int64), "equity": eq,
            "returns": eq[1:] / eq[:-1] - 1.0, "position": pos, "state_names": states}


def state_names(codes: Sequence[int] | npt.NDArray[Any]) -> list[str]:
    return [STATE_ORDER[int(c)].value for c in codes]


def state_codes(names: Sequence[str]) -> npt.NDArray[np.int8]:
    """Назви станів (рядки equity_point.risk_state) → коди STATE_CODE; невідома назва → ValueError."""
    idx = {s.value: i for i, s in enumerate(STATE_ORDER)}
    return np.asarray([idx[n] for n in names], dtype=np.int8)


__all__: Sequence[str] = (
    "BRIEF_HYSTERESIS_CLAIM_PCT", "COST_LABELS", "DETECTOR_LABELS", "FRONT_KEYS", "GIT_DIRTY_IGNORED",
    "PARAM_KEYS", "STATE_CODE", "STATE_ORDER", "SUMMARY_KEYS", "TRACE_BRIEF_CLAIMS", "TRACE_ROWS",
    "VAR_ALPHA", "VAR_WINDOW", "V_NEUTRAL", "BriefGroup", "Segment", "TaskPlan", "Variant",
    "ablation_variants", "active_mask", "add_common_args", "apply_risk_loop", "band_stats", "base_config",
    "brief_section", "cell_config", "churn_cost_pct_per_day", "command_line", "compare", "cost_variants",
    "count_breaches", "default_dd_cap", "dsr_for", "eval_folds", "evaluate_variants", "extract_md_tables",
    "fat_tail_conclusion", "first_decision_index", "fmt", "fold_dicts", "fold_rows", "full_segment",
    "git_state", "hysteresis_off", "hysteresis_variants", "is_rule_text", "is_segments", "kappa_off",
    "load_db_dataset", "load_db_run", "load_source", "md_table", "oos_segments", "params_source",
    "parse_brief_test_groups", "parse_collected", "parse_deviation_titles", "parse_params", "passport",
    "passport_md", "pct", "pick_trial_group", "plot_equity_figure", "plot_var_histogram", "profile_seed",
    "read_run", "relaxed_risk", "rolling_parametric_var", "sanitize", "segment_task", "select_cells",
    "select_is_cell", "select_on_front", "selection_caveat", "series_from_curve", "smoke_folds",
    "sr0_expected_max", "state_codes", "state_names", "state_spans", "summarize", "tbd", "test_function_name",
    "test_group_rows", "var_block", "write_outputs",
)
