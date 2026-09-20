"""Згенерувати golden-еталони офлайн-реплею: крива капіталу і паспорт прогону.

Найменування: scripts/make_golden_replay.py
Призначення: §9 передбачає у `fixtures/golden/` файли `equity_reference.json` і `run_manifest.json`.
    Наявний e2e-тест реплею перевіряє структурні властивості (45 барів, ≥ 1 угода, відсутність
    зазирання вперед, тотожність капіталу), але не фіксує самі числа — тихий регрес стратегії
    пройшов би непоміченим. Ці еталони закривають саме цю дірку.
Джерело даних: та сама детермінована офлайн-сесія, що й у тестах (`tests.helpers.workers_replay`),
    без мережі та без БД.
Перезняти після свідомої зміни ядра: `uv run python scripts/make_golden_replay.py --write`.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
# Помічник прогону живе в tests/: корінь репозиторію має бути на шляху імпорту.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.helpers.workers_replay import replayed_clean  # noqa: E402

GOLDEN = ROOT / "fixtures" / "golden"


def build() -> tuple[dict[str, Any], dict[str, Any]]:
    """Зняти з детермінованого прогону криву капіталу і паспорт."""
    r = replayed_clean()
    summary = r.summary.as_dict()

    # Крива капіталу: одна точка на закритий бар.
    points = [
        {"open_time_ns": o.step.open_time_ns, "equity": str(o.step.equity)}
        for o in r.sink.steps
    ]
    equity = {
        "_comment": "Еталон кривої капіталу офлайн-реплею; перезняти: "
                    "uv run python scripts/make_golden_replay.py --write",
        "bars": summary["bars"],
        "equity_first": str(summary["equity_first"]),
        "equity_last": str(summary["equity_last"]),
        "equity_hash": summary["equity_hash"],
        "points": points,
    }

    # Що НЕ входить в еталон і чому:
    #   run_id       — новий у кожному прогоні;
    #   journal_head — хеш ланцюга журналу залежить від контексту запуску (виміряно: значення
    #                  стабільне між процесами `python`, але інше під pytest), тож як еталон він
    #                  дав би хибні падіння. Відтворюваність фіксує `equity_hash`, а цілісність
    #                  самого ланцюга перевіряє `fuzzhelm verify-journal` і тести журналу.
    skip = {"run_id", "journal_head"}
    manifest = {k: (str(v) if not isinstance(v, (int, float, list, dict, type(None))) else v)
                for k, v in summary.items() if k not in skip}
    manifest["_comment"] = ("Еталон паспорта офлайн-реплею; run_id і journal_head свідомо виключені "
                            "(залежать від контексту запуску) — див. scripts/make_golden_replay.py")
    return equity, manifest


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="перезаписати файли еталонів")
    args = ap.parse_args(argv)

    equity, manifest = build()
    GOLDEN.mkdir(parents=True, exist_ok=True)
    for name, doc in (("equity_reference.json", equity), ("run_manifest.json", manifest)):
        path = GOLDEN / name
        text = json.dumps(doc, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if args.write:
            path.write_text(text, encoding="utf-8")
            print(f"записано {path.relative_to(ROOT)} ({len(text)} байт)")
        else:
            same = path.is_file() and path.read_text(encoding="utf-8") == text
            print(f"{path.relative_to(ROOT)}: {'збігається' if same else 'РОЗБІЖНІСТЬ'}")
    print(f"equity_hash {manifest['equity_hash'][:16]}…, барів {manifest['bars']}, "
          f"угод {manifest['closed_trades']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
