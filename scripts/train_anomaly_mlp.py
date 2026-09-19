"""Навчання MLP-автокодувальника аномалій на СПРАВЖНЬОМУ IS-вікні і замір ROC-AUC на розмічених ін'єкціях.

Найменування: train_anomaly_mlp.py
Призначення: брифінг §5.17 / фаза 7 — «навчається на векторі x нормальних барів IS-вікна … ROC-AUC на
розмічених ін'єкованих аномаліях — таблиця у звіті». Джерело за замовчуванням — робоча БД: BTCUSDT, дні 1–15
зафіксованого вікна `data/dataset_window.json` (те саме вікно, що й калібрування МФ,
docs/figures/calibration_report.md) — навчання лише на нормальних барах; відкладений відрізок — наступні дні
16–20 (OOS-вікно першого фолду walk-forward), куди по одній вносяться аномалії чотирьох типів. Скрипт лише
ЧИТАЄ БД (роль fuzzhelm_app, SELECT). `--from-fixture` — офлайн-режим на 3000 барах фікстури (як до хвилі 3).
Автор: Андрій Жук, 2026.

Сама процедура — `fuzzhelm.quality.anomaly_eval.evaluate_injections` (покрита швидким unit-тестом).

Запуск:  uv run python scripts/train_anomaly_mlp.py [--seed 20260918] [--rate 0.02] [--extra-seeds 1,2,3,4,5]
         uv run python scripts/train_anomaly_mlp.py --from-fixture --out /tmp/mlp_fixture.md
Результат: docs/figures/quality_mlp_rocauc.md (усі числа в ньому — з цього прогону).
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from fuzzhelm.backtest.manifest import dataset_hash
from fuzzhelm.cli import DAY_MS, NS_PER_MS, DatasetWindow, load_window, utc_iso
from fuzzhelm.config import get_settings
from fuzzhelm.core.enums import Venue
from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.normalize import normalize_rest_klines
from fuzzhelm.ingest.symbols import BTC_USDT_PERP
from fuzzhelm.quality.anomaly_eval import (
    ARCHITECTURES,
    DEFAULT_MAGNITUDES,
    EvalReport,
    evaluate_injections,
    structurally_valid,
)
from fuzzhelm.quality.anomaly_mlp import ANOMALY_KINDS, FEATURE_NAMES

ROOT = Path(__file__).resolve().parents[1]
KLINES = ROOT / "fixtures" / "rest" / "binance_klines.json.gz"
WINDOW_JSON = ROOT / "data" / "dataset_window.json"
CAL_MANIFEST = ROOT / "data" / "calibration_manifest.json"
OUT = ROOT / "docs" / "figures" / "quality_mlp_rocauc.md"
FIXTURE_IS_FRACTION = 0.6
MIN_MS = 60_000


def _hash(bars: list[Bar]) -> str:
    cols = {"t_ns": np.array([b.t_ns for b in bars], dtype=np.int64),
            **{k: np.array([getattr(b, k) for b in bars]) for k in ("o", "h", "l", "c", "v")}}
    return dataset_hash(cols)


def load_fixture() -> tuple[list[Bar], dict[str, Any]]:
    rows: list[list[Any]] = orjson.loads(gzip.decompress(KLINES.read_bytes()))
    now_ms = rows[-1][6] + 1_000
    bars = [bar_from_candle(c) for c in normalize_rest_klines(rows, BTC_USDT_PERP, now_ms * 1_000_000,
                                                              server_time_ms=now_ms)]
    n_is = int(len(bars) * FIXTURE_IS_FRACTION)
    meta = {"source": f"fixture `{KLINES.relative_to(ROOT)}`", "symbol": "BTCUSDT", "n_train": n_is,
            "train_label": f"перші {n_is} барів фікстури",
            "holdout_label": f"останні {len(bars) - n_is} барів",
            "train_hash": _hash(bars[:n_is]), "holdout_hash": _hash(bars[n_is:]), "synthetic": 0,
            "train_from": utc_iso(bars[0].t_ns // NS_PER_MS),
            "holdout_to": utc_iso(bars[-1].t_ns // NS_PER_MS + MIN_MS)}
    return bars, meta


async def load_db(url: str, symbol: str, window: DatasetWindow, is_days: int, holdout_days: int
                  ) -> tuple[list[Bar], dict[str, Any], set[int]]:
    from sqlalchemy import text  # noqa: PLC0415

    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.repositories import CandleRepo, InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415

    lo, mid = window.sub_window(1, is_days)
    _, hi = window.sub_window(is_days + 1, holdout_days)
    engine = make_engine(url, role=APP_ROLE, null_pool=True)
    try:
        async with session_scope(session_factory(engine)) as s:
            inst = await InstrumentRepo(s).get_by_venue_symbol(Venue.BINANCE_USDM, symbol)
            if inst is None:
                raise SystemExit(f"instrument {symbol} is not in the DB: run `fuzzhelm backfill` first")
            repo = CandleRepo(s)
            a_is = await repo.load_arrays(inst.id, window.tf, lo * NS_PER_MS, mid * NS_PER_MS)
            a_ho = await repo.load_arrays(inst.id, window.tf, mid * NS_PER_MS, hi * NS_PER_MS)
            res = await s.execute(text(
                "SELECT (extract(epoch FROM open_time) * 1000000)::bigint * 1000 FROM candle "
                "WHERE instrument_id = :iid AND tf = :tf AND is_closed AND is_synthetic "
                "AND open_time >= to_timestamp(:lo / 1000.0) AND open_time < to_timestamp(:hi / 1000.0)"),
                {"iid": inst.id, "tf": window.tf, "lo": lo, "hi": hi})
            synthetic = {int(r[0]) for r in res.all()}
    finally:
        await engine.dispose()
    for name, arr, a, b in (("IS", a_is, lo, mid), ("held-out", a_ho, mid, hi)):
        if len(arr) != (b - a) // MIN_MS:
            raise SystemExit(f"{symbol} {name}: {len(arr)} closed bars in [{utc_iso(a)}, {utc_iso(b)}), "
                             f"expected {(b - a) // MIN_MS} — backfill the window first")
    meta = {"source": "робоча БД (PostgreSQL, порт 5442, таблиця `candle`, лише SELECT роллю fuzzhelm_app)",
            "symbol": symbol, "symbol_canon": inst.symbol_canon, "n_train": len(a_is),
            "train_label": f"дні 1–{is_days} `[{utc_iso(lo)}, {utc_iso(mid)})`",
            "holdout_label": f"дні {is_days + 1}–{is_days + holdout_days} `[{utc_iso(mid)}, {utc_iso(hi)})`",
            "train_hash": dataset_hash(a_is.columns()), "holdout_hash": dataset_hash(a_ho.columns()),
            "synthetic": len(synthetic), "train_from": utc_iso(lo), "holdout_to": utc_iso(hi)}
    return a_is.bars() + a_ho.bars(), meta, synthetic


def _calibration_hash() -> str | None:
    if not CAL_MANIFEST.is_file():
        return None
    doc = orjson.loads(CAL_MANIFEST.read_bytes())
    return str((doc.get("dataset") or {}).get("dataset_hash")) or None


def _amp(kind: str) -> str:
    lo_m, hi_m = DEFAULT_MAGNITUDES[kind]  # type: ignore[index]
    if hi_m == 0:
        return "—"
    return f"×{lo_m:g}…×{hi_m:g}" if kind == "volume_burst" else f"{lo_m * 100:g}…{hi_m * 100:g} %"


KIND_UK = {
    "price_spike": "стрибок закриття: c = c₋₁·(1 ± m), тінь розширюється до c",
    "wick": "«товстий палець»: h = max(o, c)·(1 + m), тіло без змін",
    "volume_burst": "сплеск активності: v, qv і кількість угод × m, ціни без змін",
    "frozen": "застиглий бар: o = h = l = c = c₋₁, v = 0, угод 0",
}


def write_md(main: EvalReport, extra: list[EvalReport], meta: dict[str, Any], *, command: str, rate: float,
             seconds: float, out: Path) -> str:
    n_ho = main.n_holdout_bars
    cal = _calibration_hash()
    same = "збігається" if cal == meta["train_hash"] else "НЕ збігається"
    lines = [
        "# MLP-автокодувальник аномалій: ROC-AUC на розмічених ін'єкціях (справжнє IS-вікно)",
        "",
        f"Згенеровано `{command}`. Автор: Андрій Жук, 2026. Процедура — "
        "`fuzzhelm.quality.anomaly_eval.evaluate_injections`; усі числа нижче — з цього прогону.",
        "",
        "## Дані",
        "",
        f"* Джерело: {meta['source']}; символ {meta['symbol']}, tf 1m, закриті свічки.",
        f"* Навчання (IS): {meta['train_label']} — {meta['n_train']} барів; `dataset_hash` (колонки c, h, l, "
        f"o, t_ns, v) `{meta['train_hash']}`.",
    ]
    if cal is not None and meta["source"].startswith("робоча"):
        lines.append(f"  Хеш {same} з хешем вікна калібрування МФ у `data/calibration_manifest.json` "
                     f"(`{cal[:16]}…`): це ті самі бари.")
    lines += [
        f"* Відкладений відрізок (held-out): {meta['holdout_label']} — {n_ho} барів; `dataset_hash` "
        f"`{meta['holdout_hash']}`. На ньому нічого не підбирається: архітектура 8-3-8 задана наперед "
        "(брифінг §3), 5-3-5 — порівняння, seed — `FUZZHELM_SEED`.",
        f"* «Нормальні» бари навчання: справжні бари IS без ін'єкцій; вилучаються лише бари зі зламаними "
        f"структурними інваріантами або синтетичні (добір прогалин) — вилучено {main.excluded_train_vectors} "
        f"векторів (синтетичних свічок у вікні: {meta['synthetic']}). Справжні екстремальні бари не "
        "вилучаються: розмітки для них немає, а відбір власним скором моделі був би коловим.",
        f"* Векторів ознак: навчання {main.train_vectors} (після прогріву екстрактора, 218 барів), негативів "
        f"held-out {main.neg_vectors}; σ(Δlog p) на навчанні = {main.sigma_dlogp_train:.6f}.",
        f"* Ознаки 8-3-8: {', '.join(FEATURE_NAMES)}; 5-3-5 — перші п'ять (вектор брифінгу §5.17).",
        "* Модель: StandardScaler → MLPRegressor(hidden_layer_sizes=(3,), activation=tanh, max_iter=500, "
        f"random_state={main.seed}); скор — ‖x − x̂‖² у стандартизованому просторі; поріг — q₉₉ навчальних "
        "скорів.",
        "",
        "## Ін'єкції (типи й частоти)",
        "",
        f"Кожен тип вноситься в {main.per_kind} випадкових позицій held-out (частота {rate:g} на тип = "
        f"{main.per_kind}/{n_ho} барів; разом {main.per_kind * len(ANOMALY_KINDS)} позитивних на "
        f"{main.neg_vectors} негативних), по одній: стан екстрактора до бару t — з чистого ряду, змінюється "
        "лише бар t.",
        "",
        "| тип | що спотворюється | амплітуда m | позицій |",
        "|---|---|---|---|",
    ]
    lines += [f"| {k} | {KIND_UK[k]} | {_amp(k)} | {main.per_kind} |" for k in ANOMALY_KINDS]
    lines += [
        "",
        "## Результат (seed основного прогону)",
        "",
        "| тип аномалії | ROC-AUC 8-3-8 | recall@q99 8-3-8 | ROC-AUC 5-3-5 | recall@q99 5-3-5 |",
        "|---|---|---|---|---|",
    ]
    a, b = main.archs["8-3-8"], main.archs["5-3-5"]
    for k in ANOMALY_KINDS:
        pa, pb = a.per_kind[k], b.per_kind[k]
        lines.append(f"| {k} | {pa['auc']:.4f} | {pa['recall_at_q99']:.3f} | {pb['auc']:.4f} | "
                     f"{pb['recall_at_q99']:.3f} |")
    lines += [
        f"| **усі типи** | **{a.auc_all:.4f}** | | **{b.auc_all:.4f}** | |",
        "",
        f"Частка хибних тривог на чистих held-out барах при порозі q₉₉: 8-3-8 — {a.fpr_at_q99:.4f}, "
        f"5-3-5 — {b.fpr_at_q99:.4f} (номінал на навчальній вибірці — 0.01). Навчання: 8-3-8 — {a.n_iter} "
        f"ітерацій (збіжність: {a.converged}), 5-3-5 — {b.n_iter} ({b.converged}).",
        "",
    ]
    drift = main.feature_drift
    lines += [
        "Дрейф розподілу чистих held-out барів відносно навчання по ознаках — пояснює, чому частка хибних "
        "тривог на held-out відрізняється від номінальних 1 %. σ(Δlog p): навчання "
        f"{main.sigma_dlogp_train:.6f}, held-out {main.sigma_dlogp_holdout:.6f}.",
        "",
        "| міра | " + " | ".join(drift) + " |",
        "|---|" + "---|" * len(drift),
        "| σ_held-out / σ_IS | " + " | ".join(f"{d['sd_ratio']:.3f}" for d in drift.values()) + " |",
        "| частка поза центральними 99 % IS (номінал 0.01) | "
        + " | ".join(f"{d['tail_share']:.4f}" for d in drift.values()) + " |",
        "",
    ]
    if extra:
        reps = [main, *extra]
        lines += [
            "## Стійкість до seed",
            "",
            "Той самий набір даних; seed змінює позиції й амплітуди ін'єкцій та ініціалізацію мережі.",
            "",
            "| seed | ROC-AUC 8-3-8 | FPR@q99 8-3-8 | ROC-AUC 5-3-5 | FPR@q99 5-3-5 |",
            "|---|---|---|---|---|",
        ]
        lines += [f"| {r.seed} | {r.archs['8-3-8'].auc_all:.4f} | {r.archs['8-3-8'].fpr_at_q99:.4f} | "
                  f"{r.archs['5-3-5'].auc_all:.4f} | {r.archs['5-3-5'].fpr_at_q99:.4f} |" for r in reps]
        for label in ARCHITECTURES:
            aucs = [r.archs[label].auc_all for r in reps]
            lines.append(f"| {label}: середнє ± σ (n={len(aucs)}) | {statistics.mean(aucs):.4f} ± "
                         f"{statistics.stdev(aucs):.4f} | min {min(aucs):.4f} | max {max(aucs):.4f} | |")
        wins = sum(r.archs["8-3-8"].auc_all > r.archs["5-3-5"].auc_all for r in reps)
        lines += ["", f"8-3-8 має вищий ROC-AUC по всіх типах у {wins} з {len(reps)} прогонів.", ""]
    lines += [f"Час прогону скрипта: {seconds:.1f} с.", ""]
    text = "\n".join(lines)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return text


def _seeds(text: str) -> list[int]:
    return [int(x) for x in text.split(",") if x.strip()]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--seed", type=int, default=None, help="default: FUZZHELM_SEED (20260918)")
    ap.add_argument("--rate", type=float, default=0.02,
                    help="injections per kind as a fraction of held-out bars")
    ap.add_argument("--extra-seeds", type=_seeds, default=[1, 2, 3, 4, 5],
                    help="robustness seeds (comma-separated; empty string = none)")
    ap.add_argument("--symbol", default="BTCUSDT")
    ap.add_argument("--is-days", type=int, default=15)
    ap.add_argument("--holdout-days", type=int, default=5)
    ap.add_argument("--window-json", type=Path, default=WINDOW_JSON)
    ap.add_argument("--database-url", default=None, help="default: FUZZHELM_DATABASE_URL / .env")
    ap.add_argument("--from-fixture", action="store_true", help="offline: 3000 bars of the REST fixture")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args(argv)
    if not 0 < a.rate <= 0.25:
        ap.error("--rate must be in (0, 0.25]")
    settings = get_settings()
    seed = settings.seed if a.seed is None else a.seed
    t0 = time.perf_counter()
    if a.from_fixture:
        bars, meta = load_fixture()
        normal = [structurally_valid(b) for b in bars]
    else:
        window, _ = load_window(a.window_json)
        if (a.is_days + a.holdout_days) * DAY_MS > window.end_ms - window.start_ms:
            ap.error("IS + held-out days exceed the dataset window")
        url = str(a.database_url or settings.database_url)
        bars, meta, synthetic = asyncio.run(load_db(url, a.symbol, window, a.is_days, a.holdout_days))
        normal = [structurally_valid(b) and b.t_ns not in synthetic for b in bars]
    n_train = int(meta["n_train"])
    train, holdout = (0, n_train), (n_train, len(bars))
    per_kind = max(1, round(a.rate * (len(bars) - n_train)))
    seeds = [seed, *[x for x in a.extra_seeds if x != seed]]
    reports = [evaluate_injections(bars, train=train, holdout=holdout, seed=s, per_kind=per_kind,
                                   normal=normal) for s in seeds]
    cmd = "uv run python scripts/train_anomaly_mlp.py" + (" --from-fixture" if a.from_fixture else "")
    cmd += f" --seed {seed} --rate {a.rate:g} --extra-seeds {','.join(map(str, a.extra_seeds))}"
    if not a.from_fixture:
        cmd += f" --symbol {a.symbol} --is-days {a.is_days} --holdout-days {a.holdout_days}"
    if a.out != OUT:
        cmd += f" --out {a.out}"
    print(write_md(reports[0], reports[1:], meta, command=cmd, rate=a.rate, seconds=time.perf_counter() - t0,
                   out=a.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
