"""Компактні дані експерименту фази 7 для панелі: walk-forward, Парето-фронт, чутливість.

Найменування: scripts/export_ui_experiments.py
Призначення: §8.2 вимагає від `BacktestView` «6 фолдів walk-forward парними стовпчиками IS vs OOS,
    Парето-фронт, таблицю чутливості». Ці числа не живуть у БД як готові таблиці: їх дають скрипти
    фази 7 у `docs/report_tables/raw/exp_search/**`. Замість нового маршруту API (панель показувала б
    інші числа, ніж звіт) беремо ті самі артефакти і зводимо їх у один невеликий JSON, який панель
    імпортує на збірці. Джерело одне — отже, панель і звіт не можуть розійтися.
Вхід: `docs/report_tables/raw/exp_search/{walkforward,grid,sensitivity}/*.json`.
Вихід: `ui/src/data/experiments.json` (≈ десятки КБ, без кривих капіталу).
Запуск: `make ui-data` (входить у `make report`).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "docs/report_tables/raw/exp_search"
OUT = ROOT / "ui/src/data/experiments.json"

# Поля клітинки сітки, потрібні фронту Парето. Решту (config_hash, equity_hash, run_id) не беремо:
# вони не малюються, а вага JSON зростає втричі.
CELL_FIELDS = (
    "cell", "n_atr", "chi", "u_enter", "rho_base", "lam",
    "oos_sharpe", "oos_max_drawdown", "oos_turnover", "oos_total_return", "oos_psr",
    "pareto_oos", "feasible_oos", "selected",
)
FOLD_METRICS = ("sharpe", "max_drawdown", "turnover", "total_return", "n_trades", "psr")
TORNADO_FIELDS = (
    "param", "symbol", "label_uk", "base_value", "low_value", "high_value",
    "sharpe_base", "sharpe_low", "sharpe_high", "sharpe_d_low", "sharpe_d_high", "sharpe_swing",
)


def _load(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _pick(src: dict[str, Any] | None, fields: tuple[str, ...]) -> dict[str, Any]:
    return {} if src is None else {k: src[k] for k in fields if k in src}


def _provenance(doc: dict[str, Any]) -> dict[str, Any]:
    p = doc.get("provenance", {})
    return {k: p[k] for k in ("git_sha", "git_dirty", "dataset_hash", "symbol", "command") if k in p}


def walkforward(symbol: str) -> dict[str, Any] | None:
    doc = _load(RAW / "walkforward" / f"walkforward_{symbol}_engines.json")
    if doc is None:
        return None
    engines = []
    for e in doc.get("engines", []):
        engines.append({
            "engine": e.get("engine"),
            "concat_oos": _pick(e.get("concat_oos"), FOLD_METRICS),
            "folds": [
                {
                    "fold": f.get("fold"),
                    "selected_cell": f.get("selected_cell"),
                    "is": _pick(f.get("is"), FOLD_METRICS),
                    "oos": _pick(f.get("oos"), FOLD_METRICS),
                }
                for f in e.get("folds", [])
            ],
        })
    return {"provenance": _provenance(doc), "engines": engines}


def pareto(symbol: str, engine: str = "mamdani") -> dict[str, Any] | None:
    doc = _load(RAW / "grid" / f"grid_{symbol}_{engine}.json")
    if doc is None:
        return None
    return {
        "provenance": _provenance(doc),
        "criteria": doc.get("criteria", []),
        "selection_rule_uk": doc.get("selection_rule_uk") or doc.get("selection_rule"),
        "dd_cap": doc.get("dd_cap"),
        "choice": doc.get("choice"),
        "front": doc.get("front_full", []),
        "dsr": doc.get("dsr"),
        "statement": doc.get("statement"),
        "cells": [{k: c[k] for k in CELL_FIELDS if k in c} for c in doc.get("cells", [])],
    }


def sensitivity(symbol: str, engine: str = "mamdani", target: str = "full") -> dict[str, Any] | None:
    doc = _load(RAW / "sensitivity" / f"sensitivity_{symbol}_{engine}_{target}.json")
    if doc is None:
        return None
    return {
        "provenance": _provenance(doc),
        "target": doc.get("target"),
        "levels": doc.get("levels", []),
        "tornado": [_pick(t, TORNADO_FIELDS) for t in doc.get("tornado", [])],
    }


def build(symbols: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {"symbols": [], "walkforward": {}, "pareto": {}, "sensitivity": {}}
    for sym in symbols:
        wf, pf, sn = walkforward(sym), pareto(sym), sensitivity(sym)
        if wf is None and pf is None and sn is None:
            continue
        out["symbols"].append(sym)
        if wf is not None:
            out["walkforward"][sym] = wf
        if pf is not None:
            out["pareto"][sym] = pf
        if sn is not None:
            out["sensitivity"][sym] = sn
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--symbol", action="append", default=None, help="repeatable; default BTCUSDT, ETHUSDT")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)

    symbols = args.symbol or ["BTCUSDT", "ETHUSDT"]
    data = build(symbols)
    if not data["symbols"]:
        print(f"no experiment outputs under {RAW}; run `make experiments` first")
        return 1

    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n"
    args.out.write_text(payload, encoding="utf-8")
    size_kb = args.out.stat().st_size / 1024
    folds = sum(len(e["folds"]) for s in data["walkforward"].values() for e in s["engines"])
    cells = sum(len(p["cells"]) for p in data["pareto"].values())
    params = sum(len(s["tornado"]) for s in data["sensitivity"].values())
    print(
        f"{args.out.relative_to(ROOT)}: {size_kb:.0f} KB — symbols {', '.join(data['symbols'])}; "
        f"{folds} folds, {cells} grid cells, {params} sensitivity params"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
