"""Перший повний прогін на фіксованому 45-денному наборі з БД → паспорт `run` DONE + звіт і рисунок (фаза 6).

Найменування: run_backtest.py
Призначення: gate фази 6 брифінгу — «у БД є run зі статусом DONE і повним паспортом»; артефакти:
    docs/figures/first_run_metrics.md (паспорт, 17 метрик + PSR, лічильники, шлях автомата ризику, час)
    і docs/figures/first_run_equity.png (крива капіталу, просадка, смуги режимів ризику).
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/run_backtest.py [--symbol BTCUSDT] [--profile backtest] [--database-url URL]

Порядок: свічки вікна data/dataset_window.json з PostgreSQL (хеш звіряється з файлом) + ставки фандингу
data/funding_<SYMBOL>.json → Dataset (канонічний шлях рушія, той самий dataset_hash, що й у API/сітки) →
перевірка ідентичності (config_hash, dataset_hash, seed, engine, git_sha): такий прогін уже є — нового не
пишемо, звіт перебудовується з уже збереженого → паспорт RUNNING → run_backtest (профіль backtest, Мамдані,
seed профілю) з записом хеш-ланцюга журналу → пакетний запис (workers.persist) → finish DONE → перевірка:
equity_hash і голова журналу, перераховані З БД, дорівнюють паспорту. Звіт і рисунок будуються з рядків БД.
Чесність: стратегія може бути збитковою — тема роботи метод, а не прибутковість (§0.2).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIG_DIR = ROOT / "docs" / "figures"
NS_PER_MS = 1_000_000

METRIC_LABELS: dict[str, str] = {
    "total_return": "Загальна дохідність",
    "cagr": "CAGR",
    "ann_vol": "Волатильність (річна)",
    "sharpe": "Шарп (річний)",
    "sortino": "Сортіно",
    "max_drawdown": "Макс. просадка",
    "calmar": "Калмар",
    "ulcer_index": "Ulcer index",
    "profit_factor": "Profit factor",
    "expectancy": "Очікування угоди, USDT",
    "win_rate": "Частка прибуткових",
    "avg_win": "Сер. виграш, USDT",
    "avg_loss": "Сер. програш, USDT",
    "n_trades": "Угод",
    "turnover": "Оборот (капіталів/рік)",
    "exposure": "Частка часу в ринку",
    "tail_ratio": "Tail ratio",
    "psr": "PSR (SR* = 0)",
}


def _utc(ns: int) -> str:
    return datetime.fromtimestamp(ns / 1e9, tz=UTC).strftime("%Y-%m-%d %H:%M")


def _fmt(x: float | None) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    if abs(x) >= 1000 or x == int(x):
        return f"{x:,.0f}".replace(",", " ") if abs(x) >= 1000 else f"{x:.0f}"
    return f"{x:.6g}"


async def load_window(factory: Any, symbol: str) -> Any:
    """Той самий набір, що й backtest.runner.load_db_window, але через передану фабрику сесій."""
    from fuzzhelm.backtest.dataset import Dataset  # noqa: PLC0415
    from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415
    from fuzzhelm.ingest.funding import load_funding_json  # noqa: PLC0415
    from fuzzhelm.storage.repositories import CandleRepo, InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    win = json.loads((ROOT / "data" / "dataset_window.json").read_text(encoding="utf-8"))
    meta = win["symbols"][symbol]
    lo, hi = win["window"]["start_ms"] * NS_PER_MS, win["window"]["end_ms"] * NS_PER_MS
    async with session_scope(factory) as s:
        row = await InstrumentRepo(s).get_by_canon(meta["symbol_canon"])
        if row is None:
            raise SystemExit(
                f"instrument {meta['symbol_canon']} is not in the DB: run `fuzzhelm backfill` first"
            )
        arr = await CandleRepo(s).load_arrays(row.id, "1m", lo, hi)
    if dataset_hash(arr.columns()) != meta["dataset_hash"]:
        raise SystemExit(f"{symbol}: candles in the DB differ from data/dataset_window.json")
    inst = row.to_dto()
    fpath = ROOT / "data" / f"funding_{symbol}.json"
    rates = load_funding_json(fpath, inst).rates if fpath.exists() else None
    ds = Dataset.from_candle_arrays(arr, inst, funding=rates, source=f"db:{symbol}")
    return ds, row.id, lo, hi


async def run(args: argparse.Namespace) -> dict[str, Any]:
    from fuzzhelm.backtest.engine import BacktestConfig, run_backtest  # noqa: PLC0415
    from fuzzhelm.backtest.manifest import equity_hash, read_git_sha  # noqa: PLC0415
    from fuzzhelm.config import get_settings, load_yaml  # noqa: PLC0415
    from fuzzhelm.core.enums import RunKind, RunStatus  # noqa: PLC0415
    from fuzzhelm.core.journal import JournalEntry  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import SystemClock, new_run_id  # noqa: PLC0415
    from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
    from fuzzhelm.storage.repositories import EquityRepo, JournalRepo, RiskEventRepo, RunRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415
    from fuzzhelm.workers.persist import Passport, create_run, finish_run, persist_backtest  # noqa: PLC0415

    settings = get_settings()
    engine = make_engine(args.database_url or settings.database_url, role=APP_ROLE, null_pool=True)
    factory = session_factory(engine)
    clock = SystemClock()
    timings: dict[str, float] = {}
    try:
        t = time.perf_counter()
        ds, iid, lo, hi = await load_window(factory, args.symbol)
        timings["load_db_s"] = time.perf_counter() - t
        prof = load_yaml(f"profiles/{args.profile}")
        cfg = BacktestConfig.from_profile(args.profile)
        seed = int(prof["seed"])
        sha, dirty = read_git_sha()
        ds_hash = ds.dataset_hash
        async with session_scope(factory) as s:
            existing = await RunRepo(s).find_by_identity(
                config_hash=cfg.config_hash,
                dataset_hash=ds_hash,
                seed=seed,
                engine=str(cfg.engine),
                git_sha=sha,
            )
        counts: dict[str, int] | None = None
        repro: dict[str, str | None] | None = None
        if existing is not None and existing.status == "DONE":
            rid = existing.id
            print(
                f"identical run already stored as {rid} "
                "(same config_hash, dataset_hash, seed, engine, git_sha);"
                " rebuilding the report from it"
            )
            if args.verify:
                # п'ятірка ідентичності не бачить незакомічених змін (git_dirty): перерахувати прогін ПОТОЧНИМ
                # кодом і чесно показати, чи відтворюються хеші паспорта
                t = time.perf_counter()
                again = run_backtest(ds, cfg, seed, run_id=rid, git=False, kind=RunKind.BACKTEST)
                timings["reproduce_s"] = time.perf_counter() - t
                repro = {"equity_hash": again.manifest.equity_hash,
                         "journal_head": again.manifest.journal_head_hash}
        elif existing is not None and sha is not None:     # NULL git_sha унікальний індекс не блокує
            raise SystemExit(
                f"run {existing.id} with the same identity (config_hash, dataset_hash, seed, engine, "
                f"git_sha) is {existing.status}; ux_run_identity blocks a new one until the tree is "
                "committed (new git_sha)"
            )
        else:
            rid = new_run_id()
            async with session_scope(factory) as s:
                await create_run(
                    s,
                    Passport(
                        run_id=rid,
                        kind=RunKind.BACKTEST,
                        config=cfg.identity_dict(),
                        config_hash=cfg.config_hash,
                        dataset_hash=ds_hash,
                        seed=seed,
                        engine=str(cfg.engine),
                        git_sha=sha,
                        git_dirty=dirty,
                        instrument_id=iid,
                        tf="1m",
                        ts_from_ns=lo,
                        ts_to_ns=hi,
                        started_at_ns=clock.now_ns(),
                    ),
                )
            entries: list[JournalEntry] = []
            try:
                t = time.perf_counter()
                res = run_backtest(
                    ds, cfg, seed, run_id=rid, git=False, kind=RunKind.BACKTEST, journal_sink=entries.append
                )
                timings["engine_s"] = time.perf_counter() - t
                m = res.manifest
                if (m.config_hash, m.dataset_hash) != (cfg.config_hash, ds_hash):
                    raise RuntimeError("engine manifest does not match the stored passport")
                t = time.perf_counter()
                async with session_scope(factory) as s:
                    pc = await persist_backtest(
                        s, rid, res, instrument_id=iid, symbol=ds.instrument.symbol_canon, journal=entries
                    )
                    await finish_run(
                        s,
                        rid,
                        RunStatus.DONE,
                        journal_head_hash=m.journal_head_hash,
                        equity_hash=m.equity_hash,
                        finished_at_ns=clock.now_ns(),
                    )
                timings["persist_s"] = time.perf_counter() - t
                counts = pc.as_dict()
                async with session_scope(factory) as s:     # заміри часу — у run_metric (звіт з БД їх бачить)
                    await RunRepo(s).put_metrics(rid, {f"wall_{k}": v for k, v in timings.items()})
            except BaseException as e:
                from fuzzhelm.workers.trading_worker import public_error  # noqa: PLC0415

                async with session_scope(factory) as s:
                    # для помилок СУБД — лише клас і SQLSTATE (без SQL і параметрів), як у API (API-16)
                    await finish_run(
                        s, rid, RunStatus.FAILED, error=public_error(e), finished_at_ns=clock.now_ns()
                    )
                raise
        # усе нижче — з РЯДКІВ БД (звіт описує саме збережений прогін)
        t = time.perf_counter()
        async with session_scope(factory) as s:
            row = await RunRepo(s).get(rid)
            metrics = await RunRepo(s).get_metrics(rid)
            curve = await EquityRepo(s).curve(rid)
            ts, eq = await EquityRepo(s).equity_series(rid)
            bad = await JournalRepo(s).verify(rid)
            head = await JournalRepo(s).head(rid)
            transitions = await RiskEventRepo(s).transitions(rid)
            vetoes = await RiskEventRepo(s).vetoes(rid, limit=100_000)
            n_orders = (
                await s.execute(_sql("SELECT count(*) FROM sim_order WHERE run_id = :r"), {"r": rid})
            ).scalar_one()
            n_dec = (
                await s.execute(_sql("SELECT count(*) FROM decision WHERE run_id = :r"), {"r": rid})
            ).scalar_one()
            n_pos = (
                await s.execute(_sql("SELECT count(*) FROM position WHERE run_id = :r"), {"r": rid})
            ).scalar_one()
            n_risk = (
                await s.execute(_sql("SELECT count(*) FROM risk_event WHERE run_id = :r"), {"r": rid})
            ).scalar_one()
            exits = dict(
                (
                    await s.execute(
                        _sql("SELECT exit_reason, count(*) FROM position WHERE run_id = :r GROUP BY 1"),
                        {"r": rid},
                    )
                ).all()
            )
        timings["read_back_s"] = time.perf_counter() - t
    finally:
        await engine.dispose()
    assert row is not None
    eq_hash_db = equity_hash(eq, ts)
    return {
        "run": row,
        "metrics": metrics,
        "curve": curve,
        "equity_hash_db": eq_hash_db,
        "journal_bad": bad,
        "journal_head_db": head.head.hex(),
        "journal_entries_db": head.next_seq,
        "transitions": transitions,
        "vetoes": vetoes,
        "counts": counts,
        "timings": timings,
        "symbol": args.symbol,
        "profile": args.profile,
        "git_dirty": dirty,
        "repro": repro,
        "head_now": sha,
        "n_orders": n_orders,
        "n_decisions": n_dec,
        "n_positions": n_pos,
        "n_risk": n_risk,
        "exits": exits,
        "bars": len(ds),
    }


def _sql(q: str) -> Any:
    from sqlalchemy import text  # noqa: PLC0415

    return text(q)


def report_md(r: dict[str, Any], figure: Path) -> str:
    run = r["run"]
    m = r["metrics"]
    ok_eq = run.equity_hash is not None and run.equity_hash.hex() == r["equity_hash_db"]
    ok_j = (
        r["journal_bad"] is None
        and run.journal_head_hash is not None
        and run.journal_head_hash.hex() == r["journal_head_db"]
    )
    lines = [
        f"# Перший повний прогін: {r['symbol']}, 45 днів, профіль `{r['profile']}`",
        "",
        "Згенеровано `scripts/run_backtest.py` з рядків PostgreSQL (таблиці `run`, `run_metric`,",
        "`equity_point`, `risk_event`, `event_journal`). Стратегія може бути збитковою: тема роботи — метод",
        "і перевірюваний стенд, а не прибутковість (брифінг §0.2).",
        "",
        "## Паспорт прогону",
        "",
        "| поле | значення |",
        "|---|---|",
        f"| run_id | `{run.id}` |",
        f"| kind / status | {run.kind} / **{run.status}** |",
        f"| вікно | {_utc(run.ts_from_ns)} — {_utc(run.ts_to_ns)} UTC (кінець виключно), "
        f"{r['bars']} барів 1m |",
        f"| config_hash | `{run.config_hash.hex()}` |",
        f"| dataset_hash | `{run.dataset_hash.hex()}` (= `data/dataset_window.json` + ряд фандингу) |",
        f"| git_sha | `{run.git_sha}`"
        + (" — **дерево мало незакомічені зміни** (`run_metric.git_dirty = 1`)" if m.get("git_dirty") else "")
        + " |",
        f"| seed / engine | {run.seed} / {run.engine} |",
        f"| journal_head_hash | `{run.journal_head_hash.hex() if run.journal_head_hash else None}` |",
        f"| equity_hash | `{run.equity_hash.hex() if run.equity_hash else None}` |",
        "",
        f"Перевірка з БД: `equity_hash`, перерахований з {len(r['curve'])} рядків `equity_point`, "
        f"{'**збігається**' if ok_eq else '**НЕ збігається**'} з паспортом; хеш-ланцюг `event_journal` "
        f"({r['journal_entries_db']} записів) {'цілий, голова = паспорт' if ok_j else '**пошкоджений**'}.",
        "",
        *_repro_lines(r),
        "## Метрики (вікно оцінки — з бару першого можливого рішення)",
        "",
        "| метрика | значення |",
        "|---|---|",
    ]
    for key, label in METRIC_LABELS.items():
        if key in m:
            lines.append(f"| {label} (`{key}`) | {_fmt(m[key])} |")
    extra = sorted(k for k in m if k not in METRIC_LABELS and not k.startswith("wall_"))
    lines += [
        "",
        "Інші величини `run_metric`: " + ", ".join(f"`{k}` = {_fmt(m[k])}" for k in extra) + ".",
        "",
    ]
    lines += [
        "## Що записано",
        "",
        f"* `decision`: {r['n_decisions']} (профіль `backtest`: повне трасування лише рішень із заявками, "
        "ENG-10);",
        f"* `sim_order`: {r['n_orders']}, кожен з `decision_id` (NOT NULL + FK); "
        f"`position`: {r['n_positions']} "
        f"({', '.join(f'{k}: {v}' for k, v in sorted(r['exits'].items(), key=lambda kv: str(kv[0])))});",
        f"* `risk_event`: {r['n_risk']} (з них VETO: {len(r['vetoes'])}); `equity_point`: {len(r['curve'])} "
        "(з VaR₉₅/CVaR₉₅ у USDT на вікні 500 бар-дохідностей).",
        "",
        "## Шлях автомата ризику",
        "",
        "| час (UTC) | перехід | подія | просадка | денний PnL |",
        "|---|---|---|---|---|",
    ]
    for t in r["transitions"]:
        p = t.payload or {}
        lines.append(
            f"| {_utc(t.ts_ns)} | {t.state_from} → {t.state_to} | {p.get('event')} | "
            f"{float(t.observed or 0):.4f} | {float(p.get('day_return') or 0):.4f} |"
        )
    if not r["transitions"]:
        lines.append("| — | переходів не було | | | |")
    states: dict[str, int] = {}
    for p in r["curve"]:
        states[p.risk_state] = states.get(p.risk_state, 0) + 1
    lines += [
        "",
        "Барів у кожному режимі: " + ", ".join(f"{k}: {v}" for k, v in sorted(states.items())) + ".",
        "",
    ]
    wall = {k[5:]: v for k, v in m.items() if k.startswith("wall_") and v is not None}
    if wall:
        lines += ["## Час прогону (заміряно під час запису; паралельно працювали інші процеси)", ""]
        lines += [f"* {k}: {v:.2f} с" for k, v in wall.items()]
        lines.append("")
    if r["counts"]:
        lines += ["Лічильники запису: " + ", ".join(f"{k} = {v}" for k, v in r["counts"].items()) + ".", ""]
    lines += [f"Рисунок: `{figure.relative_to(ROOT)}`.", ""]
    return "\n".join(lines)


def _repro_lines(r: dict[str, Any]) -> list[str]:
    """Чи відтворює ПОТОЧНИЙ код збережений прогін (перевірка при повторному запуску скрипта)."""
    rep, run = r.get("repro"), r["run"]
    if not rep:
        return []

    def verdict(now: str | None, stored: bytes | None) -> tuple[bool, str]:
        ok = stored is not None and now == stored.hex()
        return ok, "**відтворюється**" if ok else f"**НЕ відтворюється** (зараз `{str(now)[:16]}…`)"

    ok_eq, eq_txt = verdict(rep["equity_hash"], run.equity_hash)
    ok_j, j_txt = verdict(rep["journal_head"], run.journal_head_hash)
    dirty = " + незакомічені зміни" if r["git_dirty"] else ""
    lines = [
        f"Відтворення поточним кодом (HEAD `{str(r['head_now'])[:7]}`{dirty}; той самий run_id, seed і "
        f"дані): `equity_hash` {eq_txt}; голова журналу {j_txt}.",
    ]
    if ok_eq and not ok_j:
        lines.append("Крива капіталу та сама, а записи журналу інші: код змінився після запису прогону "
                     "(журнал містить `client_order_id`; див. `docs/deviations.d/workers.md` W-10).")
    return [*lines, ""]


STATE_COLORS = {"NORMAL": "#0ca30c", "WARNING": "#fab219", "COOLDOWN": "#ec835a", "HALTED": "#d03b3b"}
STATE_UK = {
    "NORMAL": "NORMAL",
    "WARNING": "WARNING (κ=0.5)",
    "COOLDOWN": "COOLDOWN (reduce-only)",
    "HALTED": "HALTED",
}


def plot(r: dict[str, Any], out: Path) -> None:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415
    from matplotlib.patches import Patch  # noqa: PLC0415

    ink, ink2, grid, surface, series = "#0b0b0b", "#52514e", "#e4e3dd", "#fcfcfb", "#2a78d6"
    curve = r["curve"]
    t = [datetime.fromtimestamp(p.ts_ns / 1e9, tz=UTC) for p in curve]
    eq = [float(p.equity) for p in curve]
    dd = [-100 * float(p.drawdown or 0) for p in curve]
    plt.rcParams.update(
        {
            "font.family": "DejaVu Sans",
            "figure.facecolor": surface,
            "axes.facecolor": surface,
            "axes.edgecolor": grid,
            "axes.labelcolor": ink2,
            "xtick.color": ink2,
            "ytick.color": ink2,
            "font.size": 9,
        }
    )
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(12, 6.2), sharex=True, layout="constrained", gridspec_kw={"height_ratios": [3, 1.3]}
    )
    # смуги режимів ризику: суцільні відрізки однакового risk_state
    spans: list[tuple[int, int, str]] = []
    start = 0
    for i in range(1, len(curve) + 1):
        if i == len(curve) or curve[i].risk_state != curve[start].risk_state:
            spans.append((start, i - 1, curve[start].risk_state))
            start = i
    for ax in (ax1, ax2):
        for a, b, st in spans:
            ax.axvspan(t[a], t[b], color=STATE_COLORS.get(st, "#cccccc"), alpha=0.13, lw=0)
        ax.grid(axis="y", color=grid, lw=0.6)
        for side in ("top", "right"):
            ax.spines[side].set_visible(False)
    ax1.plot(t, eq, color=series, lw=1.4)
    ax1.set_ylabel("Капітал, USDT")
    ax1.set_title(
        f"{r['symbol']}: крива капіталу першого повного прогону (run {str(r['run'].id)[:8]}…)",
        loc="left",
        color=ink,
        fontsize=11,
    )
    ax1.annotate(
        f"{eq[-1]:,.2f}".replace(",", " "),
        (t[-1], eq[-1]),
        xytext=(4, 0),
        textcoords="offset points",
        va="center",
        color=ink2,
        fontsize=8,
    )
    ax2.fill_between(t, dd, 0, color=series, alpha=0.18, lw=0)
    ax2.plot(t, dd, color=series, lw=1.0)
    ax2.set_ylabel("Просадка, %")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
    present = [s for s in STATE_COLORS if any(sp[2] == s for sp in spans)]
    ax1.legend(
        handles=[Patch(color=STATE_COLORS[s], alpha=0.35, label=STATE_UK[s]) for s in present],
        title="Режим ризику",
        loc="upper right",
        frameon=False,
        fontsize=8,
        title_fontsize=8,
    )
    fig.savefig(out, dpi=150)
    plt.close(fig)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="First full persisted backtest run on the 45-day DB window.")
    ap.add_argument("--symbol", default="BTCUSDT", choices=("BTCUSDT", "ETHUSDT"))
    ap.add_argument("--profile", default="backtest")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--report", type=Path, default=FIG_DIR / "first_run_metrics.md")
    ap.add_argument("--figure", type=Path, default=FIG_DIR / "first_run_equity.png")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="when an identical run is already stored, do not re-run the engine to check "
                         "that the current code reproduces its hashes")
    args = ap.parse_args(argv)
    r = asyncio.run(run(args))
    run_row = r["run"]
    plot(r, args.figure)
    args.report.write_text(report_md(r, args.figure), encoding="utf-8")
    print(
        f"run_id {run_row.id}  status {run_row.status}  config_hash {run_row.config_hash.hex()[:16]}…  "
        f"dataset_hash {run_row.dataset_hash.hex()[:16]}…  equity_hash {run_row.equity_hash.hex()[:16]}…"
    )
    print(f"{'metric':<22}{'value':>18}")
    for k in METRIC_LABELS:
        if k in r["metrics"]:
            print(f"{k:<22}{_fmt(r['metrics'][k]):>18}")
    print("timings:", {k: round(v, 2) for k, v in r["timings"].items()})
    if r["repro"]:
        for line in _repro_lines(r)[:-1]:
            print(line.replace("**", ""))
    print(f"wrote {args.report.relative_to(ROOT)} and {args.figure.relative_to(ROOT)}")
    ok = (
        run_row.status == "DONE"
        and r["journal_bad"] is None
        and run_row.equity_hash is not None
        and run_row.equity_hash.hex() == r["equity_hash_db"]
    )
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
