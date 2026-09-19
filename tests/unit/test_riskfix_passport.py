"""RF-02 (ENG-13): специфікація інструмента — частина паспорта прогону (dataset_hash → ux_run_identity).

Найменування: tests/unit/test_riskfix_passport.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from tests.helpers.engine_scripted import base_config, fixture

from fuzzhelm.backtest.dataset import HASH_COLUMNS, Dataset, instrument_spec
from fuzzhelm.backtest.engine import deterministic_run_id, run_backtest
from fuzzhelm.backtest.manifest import dataset_hash
from fuzzhelm.core.digest import canonical_json
from fuzzhelm.core.dto import Instrument

D = Decimal
COLS = ("t_ns", "o", "h", "l", "c", "v", "qv", "n")


def _with(inst: Instrument, **update: Any) -> Dataset:
    ds = fixture().slice(0, 600)
    return Dataset.from_arrays(inst.model_copy(update=update), **{k: getattr(ds, k) for k in COLS})


def test_changing_mmr_changes_the_run_passport_hash() -> None:
    inst = fixture().instrument
    base, other = _with(inst), _with(inst, mmr=inst.mmr + D("0.001"))
    assert base.dataset_hash != other.dataset_hash
    # хеш лише свічок (data/dataset_window.json) — той самий: відрізняється саме специфікація
    candles = {k: getattr(base, k) for k in HASH_COLUMNS}
    assert dataset_hash(candles) == dataset_hash({k: getattr(other, k) for k in HASH_COLUMNS})
    cfg = base_config().with_params(record_traces="none", warmup_bars=40)
    a, b = run_backtest(base, cfg, seed=1), run_backtest(other, cfg, seed=1)
    # паспорт: dataset_hash рушія = Dataset.dataset_hash (так його звіряє API), config_hash не змінився
    assert a.manifest.dataset_hash == base.dataset_hash and b.manifest.dataset_hash == other.dataset_hash
    assert a.manifest.identity() != b.manifest.identity()               # ux_run_identity різний
    assert a.manifest.config_hash == b.manifest.config_hash == cfg.config_hash
    assert deterministic_run_id(cfg.config_hash, base.dataset_hash, 1) != \
        deterministic_run_id(cfg.config_hash, other.dataset_hash, 1)
    assert a.instrument_spec == instrument_spec(inst) and b.instrument_spec["mmr"] != a.instrument_spec["mmr"]


@pytest.mark.parametrize(("field", "value"), [
    ("tick_size", D("0.2")), ("step_size", D("0.01")), ("min_notional", D("5")),
    ("maint_amount", D("12.5")), ("max_leverage", 5), ("symbol_canon", "ETH-USDT-PERP"),
    ("symbol_venue", "ETHUSDT"),
])
def test_every_spec_field_enters_the_dataset_hash(field: str, value: Any) -> None:
    inst = fixture().instrument
    assert _with(inst, **{field: value}).dataset_hash != _with(inst).dataset_hash


def test_spec_is_canonical_decimal_scale_does_not_matter() -> None:
    """'0.1' (рядок БД) і '0.10' (exchangeInfo) — та сама специфікація і той самий хеш (числа — до 1e−18)."""
    inst = fixture().instrument
    assert str(inst.tick_size) == "0.10"
    short = inst.model_copy(update={"tick_size": D("0.1"), "mmr": D("0.0050"), "min_notional": D("50.000")})
    assert instrument_spec(short) == instrument_spec(inst)
    assert _with(inst, tick_size=D("0.1")).dataset_hash == _with(inst).dataset_hash
    spec = instrument_spec(inst)
    assert spec["tick_size"] == "0.100000000000000000" and spec["mmr"] == "0.005000000000000000"
    assert set(spec) >= {"symbol_canon", "tick_size", "step_size", "min_notional", "mmr", "maint_amount",
                         "max_leverage"}
    assert canonical_json(spec)                                              # канонічно серіалізовний
