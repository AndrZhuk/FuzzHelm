"""Виконавець POST /backtests: конфігурація рушія з запиту і відображення BacktestResult → рядки БД.

Найменування: tests/unit/test_api_backtest_runner.py
Автор: Андрій Жук, 2026.

Рушій справжній (fuzzhelm.backtest.engine) на 1500 реальних 1m-барах фікстури (≈0,2 с); запис того самого
плану в PostgreSQL — tests/integration/test_api_db.py
(test_backtest_runner_persists_run_and_explain_is_consistent).
"""

from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest
from tests.helpers.api_fakes import MemoryDb, jsonb

from fuzzhelm.api import backtests as backtests_mod
from fuzzhelm.api.backtest_runner import (
    PARAM_KEYS,
    build_config,
    funding_rates,
    plan_persistence,
    run_config_json,
    strategy_trees,
)
from fuzzhelm.api.explain import explain_decision
from fuzzhelm.api.schemas import BacktestParams
from fuzzhelm.backtest.dataset import load_exchange_instrument, load_fixture_dataset
from fuzzhelm.backtest.engine import BacktestConfig
from fuzzhelm.backtest.metrics import METRIC_NAMES
from fuzzhelm.config import load_yaml
from fuzzhelm.core.dto import Instrument
from fuzzhelm.core.enums import ContractType, RunKind, Venue
from fuzzhelm.storage.repositories import RunRow

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
CYRILLIC = re.compile(r"[А-ЩЬЮЯҐЄІЇа-щьюяґєії]")
RUN_ID = UUID(int=42)
SEED = 20260918


@pytest.fixture(scope="module")
def engine_result():  # type: ignore[no-untyped-def]
    mod = backtests_mod.load_engine_module()
    ds = load_fixture_dataset().slice(0, 1500)
    cfg = build_config(mod, {"engine": "mamdani", "params": {}})
    return ds, cfg, mod.run_backtest(ds, cfg, SEED, run_id=RUN_ID, git=False, kind=RunKind.BACKTEST)


def test_build_config_applies_overrides_and_strategy_trees() -> None:
    mod = backtests_mod.load_engine_module()
    base = build_config(mod, {"engine": "mamdani"})
    over = build_config(mod, {"engine": "mamdani", "params": {"chi": 3.0, "initial_equity": "5000"}})
    assert (over.chi, over.initial_equity) == (3.0, Decimal("5000"))
    assert over.config_hash != base.config_hash  # перекриття входить у паспорт прогону
    # тексти стратегії = файли config/ → ті самі дерева → той самий config_hash
    rules, membership = (CONFIG / "rules_mamdani.yaml").read_text(), (CONFIG / "membership.yaml").read_text()
    same = build_config(mod, {"engine": "mamdani"}, strategy_trees(rules, membership))
    assert same.config_hash == base.config_hash
    with pytest.raises(ValueError, match="unknown backtest parameters"):
        build_config(mod, {"engine": "mamdani", "params": {"leverage": 10}})
    # схема запиту допускає рівно ті ключі, які приймає виконавець
    assert set(BacktestParams.model_fields) == PARAM_KEYS


def test_funding_rates_are_the_whole_file_and_windowing_is_the_engines(tmp_path: Path) -> None:
    """Виконавець не обрізає ставки сам: вікно [t₀ − 1 доба, t_last + 1 хв] застосовує Dataset.with_funding
    (канонічний шлях рушія), тож хеш набору не залежить від того, чи запущено прогін через API чи CLI."""
    inst = load_exchange_instrument("BTCUSDT")
    rates = funding_rates(ROOT / "data" / "funding_BTCUSDT.json", inst)
    assert rates is not None and len(rates) > 10
    times = [r.funding_time_ms for r in rates]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert funding_rates(tmp_path / "missing.json", inst) is None  # файлу немає → fallback рушія (EXE-05)


def test_plan_maps_engine_result_to_decision_order_and_equity_rows(engine_result) -> None:  # type: ignore[no-untyped-def]
    ds, _cfg, result = engine_result
    plan = plan_persistence(result, run_id=RUN_ID, instrument_id=7)
    traced = [d for d in result.decisions if d.trace is not None]
    assert traced and len(plan.decisions) == len(traced) and plan.decisions_skipped == 0
    assert plan.decision_keys == [d.open_time_ns for d in traced]
    for rec, d in zip(plan.decisions, traced, strict=True):
        assert rec.run_id == RUN_ID and rec.instrument_id == 7 and rec.open_time_ns == d.open_time_ns
        assert rec.u_final == d.u_final and rec.target_qty == d.target_qty and rec.stop_price == d.stop_price
        # кожне рішення з заявкою має повне трасування: правила, розкладку сайзера, ризик і текст
        assert rec.fired_rules and rec.sizing is not None and rec.risk is not None
        assert rec.binding_constraint == rec.sizing["binding_constraint"] == d.binding_constraint
        assert rec.narrative and CYRILLIC.search(rec.narrative)
    # жоден ордер не губиться: або має рішення-джерело (FK), або врахований як пропущений
    assert len(plan.orders) + plan.orders_skipped == len(result.orders)
    assert all(key in set(plan.decision_keys) for _, key in plan.orders)
    assert sum(len(v) for v in plan.fills.values()) == len(result.fills)
    assert len(plan.equity) == len(ds) and plan.equity[-1].equity == result.equity[-1]
    assert set(METRIC_NAMES) <= set(plan.metrics) and "n_fills" in plan.metrics
    assert len(plan.risk_events) == len(result.risk_events) and len(plan.positions) == len(result.positions)


async def test_explain_of_engine_decision_is_consistent_with_run_config(engine_result) -> None:  # type: ignore[no-untyped-def]
    """/explain рішення рушія, перераховане з run.config (як його пише виконавець), збігається зі збереженим.

    Рядок проходить через MemoryDb (імітація JSONB і округлення NUMERIC(8,5)), як після запису в PostgreSQL.
    """
    _ds, cfg, result = engine_result
    plan = plan_persistence(result, run_id=RUN_ID, instrument_id=7)
    repo = MemoryDb().repos().decisions
    run = RunRow(
        id=RUN_ID, kind="backtest", strategy_id=None, instrument_id=7, tf="1m", ts_from_ns=None,
        ts_to_ns=None, config=jsonb(run_config_json(cfg)), config_hash=b"", dataset_hash=b"", git_sha=None,
        seed=SEED, engine="mamdani", journal_head_hash=None, equity_hash=None, status="DONE", error=None,
        started_at_ns=None, finished_at_ns=None,
    )
    for rec in plan.decisions[:5]:
        row = await repo.get(await repo.insert(rec))
        ex = explain_decision(row, run=run, strategy=None, detectors_cfg=load_yaml("detectors"))
        assert ex["strategy"]["source"] == "run_config" and ex["inputs_source"] == "detector_outputs"
        assert ex["consistency"]["ok"] is True and ex["consistency"]["max_alpha_abs_diff"] <= 1e-12
        assert ex["u_raw"] == pytest.approx(rec.u_raw, abs=1e-12)
        assert ex["narrative_source"] == "stored" and ex["narrative_uk"] == rec.narrative
        assert ex["sizing"]["binding_constraint"] == ex["target"]["binding_constraint"]


def test_run_config_json_stores_instrument_spec_explicitly() -> None:
    # ENG-13: паспорт мусить показувати, з якими tick/step/mmr рахувався прогін, а не лише хешувати їх
    inst = Instrument(venue=Venue.BINANCE_USDM, symbol_venue="BTCUSDT", symbol_canon="BTC-USDT-PERP",
                      base_asset="BTC", quote_asset="USDT", contract_type=ContractType.PERP,
                      tick_size=Decimal("0.10"), step_size=Decimal("0.001"), min_notional=Decimal(50),
                      mmr=Decimal("0.004"))
    cfg = BacktestConfig()
    out = run_config_json(cfg, inst)
    assert out["instrument"]["symbol_canon"] == "BTC-USDT-PERP"
    assert Decimal(out["instrument"]["mmr"]) == Decimal("0.004")
    assert Decimal(out["instrument"]["tick_size"]) == Decimal("0.1")
    assert "instrument" not in run_config_json(cfg)
    assert {k: v for k, v in out.items() if k != "instrument"} == run_config_json(cfg)
