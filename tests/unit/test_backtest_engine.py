"""K (решта). Подієвий рушій бектесту: детермінізм, негативний контроль seed, нульовий сигнал, відсутність
зазирання в майбутнє, незалежність сітки від кількості воркерів, тотожність обліку на кожному барі,
flash-crash → HALTED без участі людини.

Автор: Андрій Жук, 2026. Дані — 3000 реальних 1m-барів BTCUSDT (fixtures/rest/binance_klines.json.gz).
"""

from __future__ import annotations

import gzip
import json
from collections import defaultdict
from decimal import Decimal
from functools import cache
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from tests.helpers.engine_scripted import base_config, crash_dataset, fixture, fixture_run

from fuzzhelm.backtest.dataset import (
    DEFAULT_KLINES,
    INSTRUMENT_SPEC_COLUMN,
    Dataset,
    instrument_spec_column,
    load_exchange_instrument,
    load_klines_json,
)
from fuzzhelm.backtest.engine import BacktestResult, run_backtest
from fuzzhelm.backtest.manifest import dataset_hash, equity_hash
from fuzzhelm.core.enums import ExitReason, RiskState, Side
from fuzzhelm.core.errors import LookaheadError
from fuzzhelm.detectors.registry import DETECTOR_NAMES
from fuzzhelm.ingest.funding import funding_columns
from fuzzhelm.ingest.normalize import FundingRate, normalize_rest_klines

SEED = 20260918


@cache
def baseline() -> BacktestResult:
    """Прогін за замовчуванням (Мамдані, full-витрати), трасування угод, інваріанти на кожному барі."""
    return run_backtest(fixture(), base_config().with_params(record_traces="trades", check_invariants=True),
                        seed=SEED)


def _canon(rows: list[dict[str, Any]]) -> list[str]:
    """Порівнюване представлення метрик (NaN == NaN, порядок ключів неважливий)."""
    return [json.dumps(r, sort_keys=True) for r in rows]


# ---------------------------------------------------------------- детермінізм і seed


def test_backtest_deterministic_same_seed_same_equity_sha256() -> None:
    a = baseline()                                         # з check_invariants — окремий, незалежний прогін
    b = fixture_run(None, "trades", SEED)                  # ≡ run_backtest(fixture(), trades, SEED)
    assert len(a.trades) > 0, "fixture run must trade, otherwise the check is vacuous"
    assert a.equity_hash == b.equity_hash
    assert a.equity == b.equity
    assert [f.model_dump() for f in a.fills] == [f.model_dump() for f in b.fills]
    assert a.decisions == b.decisions                      # повні DecisionTrace з sizing і risk
    assert a.manifest.identity() == b.manifest.identity()
    assert a.manifest.journal_head_hash is not None
    assert a.manifest.journal_head_hash == b.manifest.journal_head_hash


def test_different_seed_changes_equity() -> None:
    ds = fixture().slice(0, 1500)
    cfg = base_config().with_params(record_traces="none")
    a = fixture_run(1500, "none", SEED)                    # ≡ run_backtest(ds, cfg, seed=SEED)
    b = run_backtest(ds, cfg, seed=SEED + 1)
    assert len(a.trades) > 0
    # негативний контроль: інший seed — інша крива (seed входить через seeded-шум ковзання, EXE-02)
    assert a.equity_hash != b.equity_hash
    # ...і лише через нього: без шуму (noise_bps = 0) seed на криву не впливає
    trees = dict(base_config().trees)
    trees["cost_model"] = {**trees["cost_model"], "noise_bps": 0}
    quiet = cfg.with_params(trees=trees)
    q1, q2 = run_backtest(ds, quiet, seed=SEED), run_backtest(ds, quiet, seed=SEED + 1)
    assert q1.equity_hash == q2.equity_hash


@pytest.mark.parametrize("engine", ["mamdani", "linear"])
def test_zero_signal_yields_flat_equity(engine: str) -> None:
    # усі ω_k = 0 ⇒ T = R = 0 ⇒ |u| < порогу входу: жодної заявки, капітал рівно початковий на кожному барі
    ds = fixture().slice(0, 900)
    cfg = base_config().with_params(engine=engine, record_traces="all", check_invariants=True,
                                    detector_weights=tuple((n, 0.0) for n in DETECTOR_NAMES))
    res = run_backtest(ds, cfg, seed=SEED)
    init = cfg.initial_equity
    assert len(res.decisions) == len(ds) - res.warmup_bars + 1
    assert all(abs(d.u_final) < 1e-12 and d.action == "none" for d in res.decisions)
    assert res.fills == [] and res.trades == [] and res.funding == [] and res.orders == []
    assert all(e == init for e in res.equity)
    assert res.equity_hash == equity_hash([init] * len(ds), res.equity_ts)
    for k in ("total_return", "max_drawdown", "sharpe", "n_trades", "turnover", "exposure"):
        assert res.metrics[k] == 0.0, k


def test_record_traces_mode_does_not_change_equity() -> None:
    ds = fixture().slice(0, 1500)
    runs = {m: fixture_run(1500, m, SEED) for m in ("none", "trades", "all")}
    assert len({r.equity_hash for r in runs.values()}) == 1
    assert len(runs["all"].decisions) == len(ds) - runs["all"].warmup_bars + 1
    with_orders = [d for d in runs["all"].decisions if d.order_ids]
    # none: лише легкі записи рішень із заявками, без DecisionTrace і без поточкових equity_point
    assert all(d.trace is None for d in runs["none"].decisions) and runs["none"].equity_points == []
    for m in ("none", "trades"):
        assert [d.open_time_ns for d in runs[m].decisions] == [d.open_time_ns for d in with_orders]
        assert len(runs[m].risk_events) == len(runs["all"].risk_events) > 0
    # запис трасувань не входить в ідентичність прогону (config_hash)
    assert runs["all"].manifest.config_hash == runs["none"].manifest.config_hash


# ---------------------------------------------------------------- без зазирання в майбутнє


def _shuffled_future(ds: Dataset, t: int, rng: np.random.Generator) -> Dataset:
    """Ті самі бари 0..t; для t+1.. — переставлені OHLCV (мітки часу зростають, як і належить)."""
    perm = rng.permutation(np.arange(t + 1, len(ds)))
    cols = {k: getattr(ds, k).copy() for k in ("o", "h", "l", "c", "v", "qv", "n")}
    for k, col in cols.items():
        col[t + 1:] = getattr(ds, k)[perm]
    return Dataset.from_arrays(ds.instrument, tf=ds.tf, t_ns=ds.t_ns, **cols, source="shuffled")


def test_shuffling_future_bars_does_not_change_past_decisions() -> None:
    ds = fixture().slice(0, 1000)
    t = 800
    cfg = base_config().with_params(record_traces="all")
    ref = run_backtest(ds, cfg, seed=SEED)
    past_ref = [d for d in ref.decisions if d.open_time_ns <= ds.t_ns[t]]
    assert len(past_ref) == t - ref.warmup_bars + 2 > 250
    assert any(d.order_ids for d in past_ref)
    for s in (1, 2):
        alt = run_backtest(_shuffled_future(ds, t, np.random.default_rng(s)), cfg, seed=SEED)
        past_alt = [d for d in alt.decisions if d.open_time_ns <= ds.t_ns[t]]
        assert past_ref == past_alt                           # повне трасування, сайзинг і ризик
        assert ref.equity[: t + 1] == alt.equity[: t + 1]
        assert alt.decisions[len(past_alt):] != ref.decisions[len(past_ref):]   # майбутнє справді інше


def test_decisions_use_only_data_up_to_t_lookahead_guard() -> None:
    ds = fixture().slice(0, 700)
    feed = ds.feed()
    seen = 0
    for i, (bar, dbar, close_ns) in enumerate(feed):
        assert feed.cursor == i
        assert bar.t_ns == dbar.open_time_ns == ds.t_ns[i]
        assert close_ns == ds.close_time_ns(i)
        assert len(feed.history("c")) == i + 1
        if i + 1 < len(ds):
            assert close_ns < ds.t_ns[i + 1]                  # рішення на close_t раніше за open_{t+1}
            with pytest.raises(LookaheadError):
                feed.peek(i + 1)                            # бар t+1 на кроці t недосяжний
        seen += 1
    assert seen == len(ds)
    # префікс: обрізаний набір дає ті самі рішення до своєї межі (жодної залежності від барів після t)
    cfg = base_config().with_params(record_traces="all")
    full = run_backtest(ds, cfg, seed=SEED)
    for cut in (600, 650):
        pre = run_backtest(ds.slice(0, cut), cfg, seed=SEED)
        assert pre.decisions == full.decisions[: len(pre.decisions)]
        assert pre.equity == full.equity[:cut]


# ---------------------------------------------------------------- облік, трасування, записи


def test_engine_accounting_identity_holds_at_every_bar() -> None:
    res = baseline()                                     # check_invariants=True: резидуал ≤ 1e−9 щобару
    ds = fixture()
    fills_at: dict[int, list[Any]] = defaultdict(list)
    for f in res.fills:
        fills_at[f.ts_fill_ns].append(f)
    funding_at: dict[int, Decimal] = defaultdict(Decimal)
    for ch in res.funding:
        funding_at[ch.ts_ns] += ch.amount
    # незалежна реконструкція з сирих потоків (без Portfolio): E = cash + q·c − Σfees
    cash, q, fees = base_config().initial_equity, Decimal(0), Decimal(0)
    worst = Decimal(0)
    assert len(res.equity_points) == len(ds)
    for i, point in enumerate(res.equity_points):
        t_open = int(ds.t_ns[i])
        cash -= funding_at.pop(t_open, Decimal(0))
        for f in fills_at.pop(t_open, []):
            dq = int(f.side) * f.qty
            cash -= dq * f.price
            q += dq
            fees += f.fee
        eq = cash + q * ds.dec_bar(i).c - fees
        worst = max(worst, abs(eq - point.equity))
        assert point.position_qty == q
        assert point.cash == cash
    assert worst <= Decimal("1E-9")
    assert not fills_at and not funding_at                  # кожне виконання відноситься до бару


def test_every_trade_opening_decision_has_fired_rules() -> None:
    res = baseline()
    by_time = {d.open_time_ns: d for d in res.decisions}
    closed = [p for p in res.positions if p.closed_at_ns is not None]
    assert len(closed) == len(res.trades) > 0
    for p in res.positions:
        d = by_time[p.opening_decision_ns]
        assert d.action in ("enter", "flip")
        assert d.trace is not None and d.trace.fired_rules, "entry without a formal Mamdani derivation"
        assert d.trace.sizing is not None and d.trace.sizing["binding_constraint"] == d.binding_constraint
        assert d.trace.risk is not None and d.trace.risk["evaluated"] is True
        assert abs(d.u_final) >= base_config().u_enter
        assert p.side == d.target_side and p.stop_price == d.stop_price and p.tp_price == d.tp_price
    reasons = {t.exit_reason for t in res.trades}
    assert None not in reasons
    assert reasons <= set(ExitReason)


def test_dataset_hash_is_the_same_for_candles_arrays_and_payload() -> None:
    inst = load_exchange_instrument("BTCUSDT")
    rows = json.loads(gzip.decompress(DEFAULT_KLINES.read_bytes()))[:600]
    candles = normalize_rest_klines(rows, inst, ts_ingest_ns=rows[-1][6] * 1_000_000 + 10**9)
    a = Dataset.from_candles(candles, inst)
    b = load_klines_json(DEFAULT_KLINES, inst).slice(0, 600)
    c = Dataset.from_payload(b.to_payload())
    assert a.dataset_hash == b.dataset_hash == c.dataset_hash
    for i in (0, 299, 599):
        assert a.dec_bar(i) == b.dec_bar(i)
        assert (a.dec_bar(i).c, a.dec_bar(i).volume) == (candles[i].c, candles[i].volume)
    with_funding = Dataset.from_arrays(inst, t_ns=b.t_ns, o=b.o, h=b.h, l=b.l, c=b.c, v=b.v,
                                       funding_t_ns=[int(b.t_ns[10])], funding_rate=[0.0001])
    assert with_funding.dataset_hash != b.dataset_hash       # інша ставка фандингу — інший датасет


def test_dataset_from_candle_arrays_and_funding_keep_the_hash_contract() -> None:
    ds = fixture().slice(0, 600)
    arr = SimpleNamespace(**{k: getattr(ds, k) for k in ("t_ns", "o", "h", "l", "c", "v", "qv", "n")})
    cols = {k: getattr(ds, k) for k in ("t_ns", "o", "h", "l", "c", "v")}
    plain = Dataset.from_candle_arrays(arr, ds.instrument)
    # свічки (ті самі колонки, що й CandleRepo.load_arrays().columns(), data/dataset_window.json) +
    # специфікація інструмента (RF-02): хеш набору ≠ хешу лише свічок, але однаковий для DB- і масив-шляху
    spec = {INSTRUMENT_SPEC_COLUMN: instrument_spec_column(ds.instrument)}
    assert plain.dataset_hash == dataset_hash({**cols, **spec}) == ds.dataset_hash != dataset_hash(cols)
    inst, t0, hour = ds.instrument, int(ds.t_ns[0]), 3_600 * 10**9

    def rate(t: int, r: str) -> FundingRate:
        return FundingRate(inst.symbol_canon, inst.venue, t, Decimal(r), None)
    inside = [rate(t0 - 12 * hour, "0.0003"), rate(t0 + 4_000_000, "0.0001"), rate(t0 + 8 * hour, "-0.0002")]
    outside = [rate(t0 - 30 * hour, "0.9"), rate(int(ds.t_ns[-1]) + 2 * hour, "0.9")]
    with_f = Dataset.from_candle_arrays(arr, inst, funding=[*outside, *inside[::-1]])
    assert with_f.funding_t_ns is not None
    assert with_f.funding_t_ns.tolist() == [r.funding_time_ns for r in inside]
    full = dataset_hash({**cols, **spec, **funding_columns(inside)})
    assert with_f.dataset_hash == full != plain.dataset_hash
    assert with_f.slice(0, 600).dataset_hash == with_f.dataset_hash       # те саме вікно, що й у slice


@pytest.mark.parametrize(("patch", "match"), [
    ({"t_ns": "dup"}, "strictly increasing"),
    ({"h": "below_close"}, "inconsistent OHLC"),
    ({"v": "negative"}, ">= 0"),
    ({"c": "nan"}, "NaN"),
    ({"tf": "7m"}, "unsupported timeframe"),
    ({"funding": "mismatch"}, "go together|lengths differ"),
])
def test_dataset_rejects_inconsistent_columns(patch: dict[str, str], match: str) -> None:
    ds = fixture().slice(0, 20)
    cols: dict[str, Any] = {k: getattr(ds, k).copy() for k in ("t_ns", "o", "h", "l", "c", "v", "qv", "n")}
    kw: dict[str, Any] = {}
    if patch.get("t_ns"):
        cols["t_ns"][5] = cols["t_ns"][4]
    if patch.get("h"):
        cols["h"][3] = cols["c"][3] - 1.0
    if patch.get("v"):
        cols["v"][2] = -1.0
    if patch.get("c"):
        cols["c"][1] = float("nan")
    if patch.get("tf"):
        kw["tf"] = "7m"
    if patch.get("funding"):
        kw["funding_t_ns"] = [int(ds.t_ns[0])]
    with pytest.raises(ValueError, match=match):
        Dataset.from_arrays(ds.instrument, **cols, **kw)


def test_dec_bar_cache_is_keyed_by_instrument_spec_not_only_candles() -> None:
    ds = fixture().slice(0, 50)                               # tick_size з exchangeInfo: '0.10'
    first = ds.dec_bar(7).c                                   # заповнити кеш процесу першим набором
    cols = {k: getattr(ds, k) for k in ("t_ns", "o", "h", "l", "c", "v", "qv", "n")}
    short_tick = Dataset.from_arrays(ds.instrument.model_copy(update={"tick_size": Decimal("0.1")}), **cols)
    assert short_tick.dataset_hash == ds.dataset_hash and short_tick.dec_bar(7).c == first
    # масштаб Decimal-ціни — від запису tick; спільний кеш віддав би '…0' і змінив канонічний журнал
    assert len(str(first).split(".")[1]) == 2 and len(str(short_tick.dec_bar(7).c).split(".")[1]) == 1
    eth = Dataset.from_arrays(ds.instrument.model_copy(update={"symbol_canon": "ETH-USDT-PERP"}), **cols)
    assert eth.dec_bar(0).instrument == "ETH-USDT-PERP"


# ---------------------------------------------------------------- ризик-контур наскрізно


def test_flash_crash_drives_fsm_to_halted_and_killswitch_stays_latched() -> None:
    base = baseline()
    pos = base.position_series
    # перший бар, де лонг тримається і на закритті k, і після виконань k+1 (розрив застане позицію)
    k = next(i for i in range(len(pos) - 200) if pos[i] > 0 and pos[i + 1] > 0)
    crash = crash_dataset(fixture(), k, "0.75")
    res = run_backtest(crash, base_config().with_params(record_traces="trades", check_invariants=True),
                       seed=SEED)
    assert res.equity[: k + 1] == base.equity[: k + 1]             # до розриву — той самий шлях
    assert res.halted_at == k + 1                                   # HALTED на барі розриву, без людини
    assert res.final_state is RiskState.HALTED and res.killswitch_tripped
    halt_tr = [t for t in res.transitions if t.state_to is RiskState.HALTED]
    assert len(halt_tr) == 1 and halt_tr[0].actor is None
    assert halt_tr[0].drawdown >= Decimal("0.12") or halt_tr[0].day_return <= Decimal("-0.03")
    assert all(p.risk_state is RiskState.HALTED and p.kappa == 0 for p in res.equity_points[k + 1:])
    # засувка: після HALTED жодного приросту експозиції і жодної нової заявки на вхід
    assert [f for f in res.fills if f.ts_fill_ns > int(fixture().t_ns[k + 1])] == []
    assert all(q == 0 for q in res.position_series[k + 1:])
    assert not [o for o in res.orders if o.ts_created_ns > res.equity_ts[k] and o.role in ("enter", "flip")]
    loss = res.trades[-1]
    assert loss.side is Side.LONG and loss.exit_reason in (ExitReason.STOP, ExitReason.LIQUIDATION)
    assert loss.pnl < 0


# ---------------------------------------------------------------- сітка і walk-forward


