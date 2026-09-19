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

Робоча модель (WIRE-01): після оцінювання скрипт навчає обрану архітектуру (`--arch`, типово 5-3-5 — п'ять
ознак §5.17; на справжньому IS-вікні вона краща за 8-3-8 у 6/6 seed) на ТИХ САМИХ нормальних барах IS з тим
самим seed і пише JSON-артефакт `data/anomaly_mlp_<SYMBOL>.json` (не pickle): архітектура, mean/scale
скейлера, coefs_/intercepts_, поріг q₉₉, вікно навчання і його dataset_hash, seed, версії sklearn/numpy, числа
оцінювання цього прогону і git-провенанс. Перед записом скрипт перевіряє, що (1) поріг збігається з порогом
тієї самої архітектури в оцінюванні (та сама модель) і (2) мережа, відтворена з артефакту
(`quality.anomaly_mlp.load_model_artifact`), дає ПОБІТОВО ті самі скори на навчальних векторах. У режимі
`--from-fixture` модель пишеться лише за явним `--model-out` (фікстура не має підмінити робочу модель).

Запуск:  uv run python scripts/train_anomaly_mlp.py [--seed 20260918] [--rate 0.02] [--extra-seeds 1,2,3,4,5]
         uv run python scripts/train_anomaly_mlp.py --no-report          # лише оцінювання + артефакт моделі
         uv run python scripts/train_anomaly_mlp.py --from-fixture --out /tmp/mlp_fixture.md
Результат: docs/figures/quality_mlp_rocauc.md (усі числа в ньому — з цього прогону) і
data/anomaly_mlp_<SYMBOL>.json.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import platform
import statistics
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from fuzzhelm.backtest.manifest import dataset_hash, read_git_state
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
from fuzzhelm.quality.anomaly_mlp import (
    ANOMALY_KINDS,
    FEATURE_NAMES,
    AnomalyAutoencoder,
    ExtractorParams,
    default_model_path,
    feature_matrix,
    load_model_artifact,
    model_artifact,
    write_model_artifact,
)

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


DECISION_UK = (
    "Робоча архітектура — 5-3-5: вектор п'яти ознак брифінгу §5.17 (нормативна вимога). Порівняння з "
    "8-3-8 (§3) на справжньому IS-вікні — docs/figures/quality_mlp_rocauc.md: 5-3-5 має вищий ROC-AUC у "
    "6 з 6 seed і нижчу частку хибних тривог при q99; перевага зберігається і на внутрішньому розбитті IS "
    "(навчання дні 1–12, перевірка 13–15), тобто рішення не спирається лише на відкладені дні 16–20 "
    "(docs/deviations.d/wiring.md WIRE-01)."
)


def train_production_model(bars: list[Bar], normal: list[bool], n_train: int, n_feat: int, seed: int
                           ) -> tuple[AnomalyAutoencoder, np.ndarray, int]:
    """Та сама вибірка, що й навчання в evaluate_injections: вектори барів IS [0, n_train), нормальні бари."""
    X, idx = feature_matrix(bars[:n_train])
    keep = np.asarray(normal, dtype=bool)[idx]
    train_X = X[keep][:, :n_feat]
    return AnomalyAutoencoder(seed=seed).fit(train_X), train_X, int((~keep).sum())


def build_artifact(model: AnomalyAutoencoder, train_X: np.ndarray, excluded: int, meta: dict[str, Any],
                   main: EvalReport, extra: list[EvalReport], *, arch: str, seed: int, command: str,
                   window_ms: tuple[int, int] | None) -> dict[str, Any]:
    import sklearn  # noqa: PLC0415

    other = next(a for a in ARCHITECTURES if a != arch)
    reps = [main, *extra]
    ev_arch, ev_other = main.archs[arch], main.archs[other]
    gs = read_git_state(ROOT)
    cal = _calibration_hash()
    training: dict[str, Any] = {
        "estimator": "sklearn.preprocessing.StandardScaler -> sklearn.neural_network.MLPRegressor",
        "hidden_layer_sizes": [model.hidden], "activation": model.activation, "max_iter": model.max_iter,
        "random_state": seed, "n_iter": model.n_iter, "converged": model.converged,
        "source": meta["source"], "symbol": meta["symbol"], "window": meta["train_label"],
        "from_utc": meta["train_from"], "n_bars": int(meta["n_train"]), "n_vectors": int(train_X.shape[0]),
        "excluded_vectors": excluded, "synthetic_candles_in_window": meta["synthetic"],
        "dataset_hash": meta["train_hash"], "dataset_hash_columns": ["c", "h", "l", "o", "t_ns", "v"],
        "calibration_dataset_hash": cal, "same_bars_as_mf_calibration": cal == meta["train_hash"],
        "train_score_quantile": model.quantile, "threshold": float(model.threshold),
    }
    if window_ms is not None:
        training["from_ms"], training["to_ms"] = window_ms
    evaluation: dict[str, Any] = {
        "procedure": "fuzzhelm.quality.anomaly_eval.evaluate_injections",
        "report": "docs/figures/quality_mlp_rocauc.md", "holdout": meta["holdout_label"],
        "holdout_dataset_hash": meta["holdout_hash"], "per_kind_injections": main.per_kind, "seed": main.seed,
        "same_threshold_as_evaluated_model": float(model.threshold) == ev_arch.threshold,
        arch: {"roc_auc_all": ev_arch.auc_all, "fpr_at_q99": ev_arch.fpr_at_q99,
               "per_kind": {k: dict(v) for k, v in ev_arch.per_kind.items()}},
        other: {"roc_auc_all": ev_other.auc_all, "fpr_at_q99": ev_other.fpr_at_q99},
    }
    if extra:
        evaluation["seeds"] = [r.seed for r in reps]
        for label in (arch, other):
            aucs = [r.archs[label].auc_all for r in reps]
            evaluation[label]["roc_auc_all_mean"] = statistics.mean(aucs)
            evaluation[label]["roc_auc_all_sd"] = statistics.stdev(aucs)
        evaluation[f"{arch}_wins"] = sum(r.archs[arch].auc_all > r.archs[other].auc_all for r in reps)
    return model_artifact(
        model, symbol=str(meta["symbol"]), extractor=ExtractorParams(),
        symbol_canon=meta.get("symbol_canon"),
        decision=DECISION_UK if arch == "5-3-5" and not str(meta["source"]).startswith("fixture") else None,
        training=training, evaluation=evaluation,
        versions={"python": platform.python_version(), "numpy": np.__version__,
                  "sklearn": sklearn.__version__},
        provenance={"command": command, "created_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "git_sha": gs.sha, "git_dirty": gs.dirty, "git_dirty_paths": list(gs.dirty_paths)},
    )


def verify_roundtrip(doc: dict[str, Any], model: AnomalyAutoencoder, train_X: np.ndarray) -> bool:
    """Мережа з артефакту (JSON → numpy) дає побітово ті самі скори, що й навчена модель."""
    with tempfile.TemporaryDirectory() as d:
        p = write_model_artifact(Path(d) / "m.json", doc)
        loaded = load_model_artifact(p)
    return bool(np.array_equal(loaded.model.score(train_X), model.score(train_X)))


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
    ap.add_argument("--out", type=Path, default=None,
                    help="report path (default docs/figures/quality_mlp_rocauc.md; other symbols: "
                         "quality_mlp_rocauc_<SYMBOL>.md — the BTCUSDT report is never overwritten by them)")
    ap.add_argument("--no-report", action="store_true", help="do not write the markdown report")
    ap.add_argument("--arch", choices=tuple(ARCHITECTURES), default="5-3-5",
                    help="architecture of the persisted model (default 5-3-5, see WIRE-01)")
    ap.add_argument("--model-out", type=Path, default=None,
                    help="model artifact path (default data/anomaly_mlp_<SYMBOL>.json; fixture mode: none)")
    ap.add_argument("--no-model", action="store_true", help="evaluate only, do not write the model artifact")
    a = ap.parse_args(argv)
    if not 0 < a.rate <= 0.25:
        ap.error("--rate must be in (0, 0.25]")
    settings = get_settings()
    seed = settings.seed if a.seed is None else a.seed
    t0 = time.perf_counter()
    window_ms: tuple[int, int] | None = None
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
        window_ms = window.sub_window(1, a.is_days)
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
    default_out = OUT if a.from_fixture or a.symbol.upper() == "BTCUSDT" else \
        OUT.with_name(f"quality_mlp_rocauc_{a.symbol.upper()}.md")
    out = a.out or default_out
    if out != default_out and not a.no_report:
        cmd += f" --out {out}"
    if a.no_report:
        cmd += " --no-report"
    if a.arch != "5-3-5":
        cmd += f" --arch {a.arch}"
    if not a.no_report:
        print(write_md(reports[0], reports[1:], meta, command=cmd, rate=a.rate,
                       seconds=time.perf_counter() - t0, out=out))
    model_out = None if a.no_model else (a.model_out or (None if a.from_fixture
                                                         else default_model_path(a.symbol)))
    if model_out is None:
        return 0
    if a.model_out is not None:
        cmd += f" --model-out {a.model_out}"
    model, train_X, excluded = train_production_model(bars, normal, n_train, ARCHITECTURES[a.arch], seed)
    doc = build_artifact(model, train_X, excluded, meta, reports[0], reports[1:], arch=a.arch, seed=seed,
                         command=cmd, window_ms=window_ms)
    same = doc["evaluation"]["same_threshold_as_evaluated_model"]
    bit_identical = verify_roundtrip(doc, model, train_X)
    if not (same and bit_identical):
        print(f"refusing to write {model_out}: same_threshold={same}, "
              f"bit_identical_roundtrip={bit_identical}", file=sys.stderr)
        return 1
    write_model_artifact(model_out, doc)
    print(f"model {doc['architecture']} → {model_out}: threshold {model.threshold!r}, "
          f"{train_X.shape[0]} training vectors, n_iter {model.n_iter}, "
          f"params_sha256 {doc['params_sha256']}; "
          f"round trip bit-identical: {bit_identical}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
