"""Контракт веб-панелі §8.2: три екрани, десять компонентів, п'ять сторів, i18n uk/en без дір.

Найменування: tests/arch/test_ui_contract.py
Призначення: §8.2 перелічує склад панелі поіменно, а §13 дозволяє відрізати i18n лише свідомо.
    Перевіряємо це як інваріант дерева, а не як домовленість: зникне компонент або розійдуться
    ключі uk/en — падає прогін, а не екранограма у звіті. Тести не запускають Node і не збирають
    панель: це перевірка складу репозиторію, тож вони офлайн і швидкі.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

UI = Path(__file__).resolve().parents[2] / "ui"

VIEWS = ("LiveView", "ExplainView", "BacktestView")
COMPONENTS = (
    "CandleChart", "DetectorGauge", "MembershipPlot", "RuleTable", "RiskStatePanel",
    "RejectionLog", "EquityCurve", "MetricsTable", "RulesEditor", "DqPanel",
)
# Понад перелік §8.2: решта вимог того ж підрозділу до BacktestView («6 фолдів walk-forward
# парними стовпчиками IS vs OOS, Парето-фронт, таблиця чутливості») — окремими компонентами.
EXTRA_COMPONENTS = ("WalkForwardChart", "ParetoFront", "SensitivityTable")
STORES = ("market", "decision", "risk", "backtest", "auth")


def _keys(node: object, prefix: str = "") -> set[str]:
    """Пласка множина ключів словника перекладів: «app.nav.explain» тощо."""
    out: set[str] = set()
    if isinstance(node, dict):
        for k, v in node.items():
            out.add(f"{prefix}{k}")
            out |= _keys(v, f"{prefix}{k}.")
    return out


def test_ui_has_three_views_ten_components_and_five_stores() -> None:
    missing: list[str] = []
    for name in VIEWS:
        if not (UI / "src" / "views" / f"{name}.vue").is_file():
            missing.append(f"views/{name}.vue")
    for name in (*COMPONENTS, *EXTRA_COMPONENTS):
        if not (UI / "src" / "components" / f"{name}.vue").is_file():
            missing.append(f"components/{name}.vue")
    for name in STORES:
        if not (UI / "src" / "stores" / f"{name}.ts").is_file():
            missing.append(f"stores/{name}.ts")
    assert missing == [], f"бракує елементів панелі з §8.2: {missing}"


def test_ui_i18n_uk_and_en_have_the_same_keys() -> None:
    uk = json.loads((UI / "src" / "i18n" / "uk.json").read_text(encoding="utf-8"))
    en = json.loads((UI / "src" / "i18n" / "en.json").read_text(encoding="utf-8"))
    only_uk = sorted(_keys(uk) - _keys(en))
    only_en = sorted(_keys(en) - _keys(uk))
    assert only_uk == [] and only_en == [], f"розходження ключів i18n: лише uk {only_uk}, лише en {only_en}"


def test_ui_default_locale_is_ukrainian() -> None:
    """§8.2: «інтерфейс українською за замовчуванням» — екранограми йдуть в український звіт."""
    src = (UI / "src" / "i18n" / "index.ts").read_text(encoding="utf-8")
    assert "return 'uk'" in src, "типова локаль панелі має бути uk"


@pytest.mark.parametrize("host", ["binance.com", "api.binance.com", "fapi.binance.com"])
def test_ui_never_points_at_mainnet(host: str) -> None:
    """§0.2: у панелі не може бути жодного mainnet-хоста навіть у коментарі чи заготовці."""
    hits = [
        str(p.relative_to(UI))
        for p in (UI / "src").rglob("*")
        if p.is_file() and p.suffix in {".ts", ".vue", ".json"} and host in p.read_text(encoding="utf-8")
    ]
    cfg = UI / "vite.config.ts"
    if host in cfg.read_text(encoding="utf-8"):
        hits.append("vite.config.ts")
    assert hits == [], f"панель згадує mainnet-хост {host} у {hits}"


def test_ui_experiment_data_is_generated_and_compact() -> None:
    """§8.2 вимагає walk-forward, Парето і чутливість; дані зводить scripts/export_ui_experiments.py.

    Перевіряємо, що артефакт є, містить усі три розділи і лишається малим: у нього свідомо не
    кладуть криві капіталу (сирий walkforward — 8.8 МБ, зведення — сотня кілобайт).
    """
    data_file = UI / "src" / "data" / "experiments.json"
    assert data_file.is_file(), "немає ui/src/data/experiments.json — виконайте `make ui-data`"
    doc = json.loads(data_file.read_text(encoding="utf-8"))
    assert doc["symbols"], "у зведенні немає жодного інструмента"
    for section in ("walkforward", "pareto", "sensitivity"):
        assert doc.get(section), f"у зведенні немає розділу {section}"
    # Розділ може бути не для кожного інструмента: чутливість рахували лише для BTCUSDT.
    # Вимагаємо, щоб наявні розділи були непорожні, а панель уміла показати відсутність.
    for sym, wf in doc["walkforward"].items():
        assert wf["engines"], f"{sym}: немає рушіїв walk-forward"
    for sym, pf in doc["pareto"].items():
        assert pf["cells"], f"{sym}: немає клітинок сітки"
    for sym, sn in doc["sensitivity"].items():
        assert sn["tornado"], f"{sym}: немає рядків чутливості"
    assert data_file.stat().st_size < 400 * 1024, "зведення розрослося — у ньому зайві масиви"
