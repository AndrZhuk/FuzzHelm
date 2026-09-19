"""Інвентар тестів брифінгу: кожна тест-функція, названа в §10 (групи A–N), визначена рівно один раз.

Найменування: tests/arch/test_brief_test_inventory.py
Призначення: правило contracts §0 п. 7 («назви з брифінгу — дослівно, грепаються при підрахунку») як
    перевірка, а не домовленість: перейменування чи дубль назви одразу ламає прогін, а не таблицю звіту
    docs/report_tables/test_groups.md (генератор — tests/helpers/brief_test_groups.py).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import importlib
import math
import sys
from pathlib import Path
from typing import Any

from tests.helpers.brief_test_groups import ROOT, brief_groups, defined_tests


def test_every_brief_named_test_is_defined_exactly_once() -> None:
    groups = brief_groups()
    assert [g.letter for g in groups] == list("ABCDEFGHIJKLMN")
    assert all(g.names for g in groups), [g.letter for g in groups if not g.names]
    defs = defined_tests()
    missing = [(g.letter, n) for g in groups for n in g.names if n not in defs]
    duplicated = {n: defs[n] for g in groups for n in g.names if len(defs.get(n, [])) > 1}
    assert missing == [], missing
    assert duplicated == {}, duplicated
    # одна назва — одна група (інакше підрахунок по групах подвоює тест)
    names = [n for g in groups for n in g.names]
    assert len(names) == len(set(names))


# Кількості, які брифінг (§10) задає явно. Прискорення набору (QP-03) не сміє їх тихо зменшити.
MANDATED_EXAMPLES = {                        # [property, N прикладів]
    "test_u_nondecreasing_in_trend_input": 500,
    "test_all_detectors_bounded_and_no_nan_on_any_ohlcv": 200,     # «6×200»: 6 детекторів на кожному прикладі
}
MANDATED_CASES = {                           # [parametrize …] — добуток довжин parametrize
    "test_risk_fsm_transition_table_is_total": 24,                   # 4 стани × 6 подій
    "test_walkforward_embargo_no_overlap": 4,                        # k = 6 × E ∈ {0, 60, 120, 240}
    "test_all_pathological_sessions_recover_with_zero_lost_events": 6,
}
MANDATED_LIST_SIZES = {                      # [property, до N …] — (аргумент @given, min_size, max_size)
    "test_risk_chain_never_increases_exposure": ("chain", 0, 20),
}


def _function(name: str) -> Any:
    """Функція тесту з уже зібраного pytest модуля (pytest імпортує їх за базовим ім'ям — повторний імпорт
    під іншим ім'ям виконав би модуль удруге); поза повним прогоном — імпорт за шляхом."""
    [where] = defined_tests()[name]
    rel = where.split(":", 1)[0]
    path = str(ROOT / rel)
    mod = sys.modules.get(Path(rel).stem)
    if mod is not None and getattr(mod, "__file__", None) == path:
        return getattr(mod, name)
    return getattr(importlib.import_module(rel.removesuffix(".py").replace("/", ".")), name)


def test_brief_mandated_example_counts_and_parametrizations_are_kept() -> None:
    for name, n in MANDATED_EXAMPLES.items():
        fn = _function(name)
        s = getattr(fn, "_hypothesis_internal_use_settings", None)       # явний @settings(max_examples=…)
        assert s is not None, f"{name} must pin its example count with @settings"
        assert s.max_examples == n, (name, s.max_examples)
    for name, n in MANDATED_CASES.items():
        marks = [m for m in getattr(_function(name), "pytestmark", []) if m.name == "parametrize"]
        assert marks, name
        assert math.prod(len(m.args[1]) for m in marks) == n, name
    # «до 20 вердиктів» (§10 J): стратегія ланцюга — списки довжини 0…20 (QR-03)
    for name, (arg, lo, hi) in MANDATED_LIST_SIZES.items():
        strategy = _function(name).hypothesis._given_kwargs[arg]
        lists = getattr(strategy, "wrapped_strategy", strategy)
        assert (lists.min_size, lists.max_size) == (lo, hi), (name, arg, lists.min_size, lists.max_size)
