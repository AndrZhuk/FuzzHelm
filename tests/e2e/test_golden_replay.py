"""Golden-тест офлайн-реплею: крива капіталу і паспорт прогону не змінилися мовчки.

Найменування: tests/e2e/test_golden_replay.py
Призначення: `tests/e2e/test_replay_e2e.py` перевіряє структурні властивості прогону (45 барів,
    ≥ 1 угода, відсутність зазирання вперед, тотожність капіталу), але не фіксує самі числа.
    Тому зміна ядра, сайзера чи моделі витрат могла б тихо змінити результат стратегії, не
    зламавши жодного тесту. Ці два еталони (§9, `fixtures/golden/`) закривають саме цю дірку.
Якщо тест упав після СВІДОМОЇ зміни ядра — перезняти еталони:
    `uv run python scripts/make_golden_replay.py --write` і закомітити різницю окремо, щоб вона
    була видна в історії.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import json
import math
from decimal import Decimal
from pathlib import Path
from typing import Any

from tests.helpers.workers_replay import replayed_clean

GOLDEN = Path(__file__).resolve().parents[2] / "fixtures" / "golden"
HINT = "перезняти: uv run python scripts/make_golden_replay.py --write"


def _load(name: str) -> dict[str, Any]:
    return json.loads((GOLDEN / name).read_text(encoding="utf-8"))


def test_golden_replay_equity_curve_is_unchanged() -> None:
    """Крива капіталу збігається з еталоном у кожній точці, а не лише хешем."""
    ref = _load("equity_reference.json")
    steps = replayed_clean().sink.steps

    assert len(steps) == ref["bars"], f"кількість барів змінилася ({HINT})"
    actual = [{"open_time_ns": o.step.open_time_ns, "equity": str(o.step.equity)} for o in steps]

    # Порівнюємо як Decimal: "9992.720886400" і "9992.7208864" — те саме число.
    for i, (got, want) in enumerate(zip(actual, ref["points"], strict=True)):
        assert got["open_time_ns"] == want["open_time_ns"], f"бар {i}: зсув часу ({HINT})"
        assert Decimal(got["equity"]) == Decimal(want["equity"]), (
            f"бар {i}: капітал {got['equity']} замість {want['equity']} ({HINT})"
        )


def test_golden_replay_manifest_is_unchanged() -> None:
    """Паспорт прогону (без run_id) збігається з еталоном полем за полем."""
    ref = _load("run_manifest.json")
    summary = replayed_clean().summary.as_dict()

    mismatched: list[str] = []
    for key, want in ref.items():
        if key.startswith("_"):
            continue
        got = summary.get(key)
        got_cmp = str(got) if not isinstance(got, (int, float, list, dict, type(None))) else got
        # float-діагностика (напр. max_abs_u_final) на різних процесорах різниться в останніх знаках
        # (FMA, порядок підсумовування); гроші й хеші порівнюються точно
        if isinstance(got_cmp, float) and isinstance(want, float) and math.isclose(
                got_cmp, want, rel_tol=1e-12, abs_tol=1e-15):
            continue
        if got_cmp != want:
            mismatched.append(f"{key}: {got_cmp!r} замість {want!r}")
    assert mismatched == [], f"паспорт прогону змінився ({HINT}): " + "; ".join(mismatched)


def test_golden_equity_hash_matches_documented_value() -> None:
    """`equity_hash` офлайн-еталона — те саме число, що зафіксовано в тесті."""
    assert _load("run_manifest.json")["equity_hash"] == (
        "d0f45aa3cd5ea81e60c96780ad988e2143f0c77a83eb43a89028af3a00909f1e"
    ), f"офлайн-еталон змінився ({HINT})"
