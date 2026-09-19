"""Командний рядок FuzzHelm: реальні дані (добір, крос-звірка, фінансування), калібрування МФ, журнал, БД.

Найменування: cli.py
Призначення: точка входу `fuzzhelm` (pyproject [project.scripts] → fuzzhelm.cli:main). Підкоманди:
  backfill        — N повних UTC-днів 1m-свічок у PostgreSQL; прогалини → ingest_gap → REST-добір;
                    звіт docs/figures/backfill_report.md і зафіксоване вікно data/dataset_window.json;
  crosscheck      — Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT на останніх ~720 хв Kraken (+ розклад
                    розбіжності на премію перпетуала, курс USDT/USD і залишок) → docs/figures/crosscheck_*;
  fetch-funding   — історія ставок фінансування за вікном датасету → data/funding_<SYMBOL>.json;
  calibrate       — T-перцентилі {8,25,50,75,92} і KMeans(k=3) для V на ПЕРШОМУ IS-вікні (без заглядання
                    в OOS) → config/membership.yaml, data/calibration_manifest.json,
                    docs/figures/calibration_report.md, docs/figures/regimes_clusters.png;
  replay-gap      — реплей fixtures/ws/pathological/gap.jsonl.gz через IngestPipeline з БД-стоками і
                    справжнім REST-добором → реальні рядки ingest_gap зі статусом FILLED;
  verify-journal  — перерахунок ланцюга хешів event_journal (і звірка з run.journal_head_hash, якщо є);
  db-stats        — кількості рядків, покриття свічок, статуси прогалин, журнали.
Автор: Андрій Жук, 2026.

Мережа — лише allow-listed read-only хости: базові URL валідує Settings, а клієнти перевіряють їх
вдруге (`assert_readonly_url`); ордерних ендпоінтів тут немає. Асинхронний код — через asyncio.run.
`--dry-run` мережевих підкоманд друкує план без мережі й БД (так їх перевіряє tests/unit/test_cli.py).
Модуль поза межею детермінізму (настінний годинник для міток запуску і заміру часу), але все, що
потрапляє в дані й хеші, від нього не залежить.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import hashlib
import json
import math
import sys
import time
from collections.abc import Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final
from uuid import NAMESPACE_URL, UUID, uuid5

import numpy as np
import numpy.typing as npt
import orjson

from fuzzhelm.config import CONFIG_DIR, FIXTURES_DIR, ROOT, Settings, get_settings, load_yaml
from fuzzhelm.core.enums import GapStatus, Venue
from fuzzhelm.core.money import dec, dec_str
from fuzzhelm.features.convert import Bar
from fuzzhelm.ingest.ratelimit import USED_WEIGHT_HEADER, klines_weight, request_weight

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from fuzzhelm.core.dto import Candle, Instrument
    from fuzzhelm.ingest.crosscheck import CrosscheckReport

DAY_MS: Final = 86_400_000
MIN_MS: Final = 60_000
NS_PER_MS: Final = 1_000_000
DATA_DIR: Final = ROOT / "data"
FIG_DIR: Final = ROOT / "docs" / "figures"
WINDOW_JSON: Final = DATA_DIR / "dataset_window.json"
CAL_MANIFEST_JSON: Final = DATA_DIR / "calibration_manifest.json"
CROSSCHECK_INPUT: Final = DATA_DIR / "crosscheck_input.json.gz"
GAP_SESSION: Final = FIXTURES_DIR / "ws" / "pathological" / "gap.jsonl.gz"
GAP_REFERENCE: Final = FIXTURES_DIR / "ws" / "sample_btcusdt_4m.jsonl.gz"
DEFAULT_SYMBOLS: Final = ("BTCUSDT", "ETHUSDT")
MAX_KLINES_LIMIT: Final = 1500
TIME_SAMPLES: Final = 3                  # замірів /time для оцінки зсуву годинника (мін. RTT)

# палітра рисунків (docs/figures — ті самі ролі, що в scripts/plot_membership.py; слоти 1–3 валідні all-pairs)
SERIES: Final = ("#2a78d6", "#eb6834", "#1baf7a")
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, SURFACE = "#e1e0d9", "#c3c2b7", "#fcfcfb"


class CliError(Exception):
    """Очікувана помилка користувача/даних: друкується одним рядком, код виходу 2."""


# ================================================================== час і вікно датасету


def utc_iso(ms: int) -> str:
    """Мілісекунди епохи → 'YYYY-MM-DDTHH:MM:SSZ' (ціла арифметика, без float)."""
    dt = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=ms)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ") if ms % 1000 == 0 else dt.isoformat().replace("+00:00", "Z")


def utc_date_ms(d: date) -> int:
    return (d - date(1970, 1, 1)).days * DAY_MS


def day_floor_ms(ms: int) -> int:
    return ms // DAY_MS * DAY_MS


@dataclass(frozen=True, slots=True)
class DatasetWindow:
    """Фіксоване вікно датасету: [start_ms, end_ms) — обидві межі на UTC-півночі (кінець виключно)."""

    start_ms: int
    end_ms: int
    tf: str = "1m"
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS

    def __post_init__(self) -> None:
        if self.start_ms % DAY_MS or self.end_ms % DAY_MS:
            raise ValueError("window bounds must be UTC midnights")
        if self.end_ms <= self.start_ms:
            raise ValueError("window end must be after start")
        if self.tf != "1m":
            raise ValueError("only the 1m timeframe is supported")

    @classmethod
    def ending_at(cls, end_ms: int, days: int, symbols: Sequence[str] = DEFAULT_SYMBOLS) -> DatasetWindow:
        return cls(end_ms - days * DAY_MS, end_ms, symbols=tuple(symbols))

    @property
    def days(self) -> int:
        return (self.end_ms - self.start_ms) // DAY_MS

    @property
    def bars_per_symbol(self) -> int:
        return (self.end_ms - self.start_ms) // MIN_MS

    @property
    def last_open_ms(self) -> int:
        return self.end_ms - MIN_MS

    def sub_window(self, first_day: int, n_days: int) -> tuple[int, int]:
        """[from, to) для днів first_day … first_day + n_days − 1 (нумерація днів з 1)."""
        if first_day < 1 or n_days < 1 or first_day - 1 + n_days > self.days:
            raise ValueError(f"days {first_day}..{first_day + n_days - 1} are outside the "
                             f"{self.days}-day window")
        lo = self.start_ms + (first_day - 1) * DAY_MS
        return lo, lo + n_days * DAY_MS

    def to_dict(self) -> dict[str, Any]:
        return {"start_ms": self.start_ms, "end_ms": self.end_ms, "start_utc": utc_iso(self.start_ms),
                "end_utc_exclusive": utc_iso(self.end_ms), "last_open_utc": utc_iso(self.last_open_ms),
                "days": self.days, "tf": self.tf, "bars_per_symbol": self.bars_per_symbol,
                "symbols": list(self.symbols)}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> DatasetWindow:
        syms = d.get("symbols") or list(DEFAULT_SYMBOLS)
        if isinstance(syms, Mapping):
            syms = list(syms)
        return cls(int(d["start_ms"]), int(d["end_ms"]), str(d.get("tf", "1m")), tuple(syms))


def load_window(path: Path) -> tuple[DatasetWindow, dict[str, Any]]:
    """data/dataset_window.json → (вікно, увесь документ)."""
    if not path.exists():
        raise CliError(f"{_rel(path)} not found: run `fuzzhelm backfill` first (it fixes the dataset window)")
    doc = orjson.loads(path.read_bytes())
    return DatasetWindow.from_dict(doc["window"]), doc


@dataclass(frozen=True, slots=True)
class BackfillPlan:
    window: DatasetWindow
    limit: int
    pages_per_symbol: int
    weight_per_symbol: int
    overhead_weight: int

    @property
    def total_requests(self) -> int:
        return self.pages_per_symbol * len(self.window.symbols) + TIME_SAMPLES + 1

    @property
    def total_weight(self) -> int:
        return self.weight_per_symbol * len(self.window.symbols) + self.overhead_weight

    def lines(self) -> list[str]:
        w = self.window
        return [
            f"window   : [{utc_iso(w.start_ms)}, {utc_iso(w.end_ms)})  ({w.days} full UTC days, tf={w.tf})",
            f"symbols  : {', '.join(w.symbols)}",
            f"bars     : {w.bars_per_symbol} per symbol, {w.bars_per_symbol * len(w.symbols)} total",
            f"requests : {self.pages_per_symbol} klines pages/symbol (limit={self.limit}, 1-bar overlap) "
            f"+ {TIME_SAMPLES} /time + 1 /exchangeInfo = {self.total_requests}",
            f"weight   : {self.weight_per_symbol}/symbol (klines_weight({self.limit}) = "
            f"{klines_weight(self.limit)}) + {self.overhead_weight} = {self.total_weight} (limit 2400/min)",
        ]


def pages_needed(n_bars: int, limit: int) -> int:
    """Сторінок klines з перекриттям в 1 бар: перша дає `limit` барів, кожна наступна — `limit − 1` нових."""
    if not 2 <= limit <= MAX_KLINES_LIMIT:
        raise ValueError(f"limit must be in [2, {MAX_KLINES_LIMIT}]")
    if n_bars <= limit:
        return 1
    return 1 + math.ceil((n_bars - limit) / (limit - 1))


def plan_backfill(window: DatasetWindow, limit: int = MAX_KLINES_LIMIT) -> BackfillPlan:
    pages = pages_needed(window.bars_per_symbol, limit)
    overhead = TIME_SAMPLES * request_weight("/fapi/v1/time") + request_weight("/fapi/v1/exchangeInfo")
    return BackfillPlan(window, limit, pages, pages * klines_weight(limit), overhead)


# ================================================================== дрібні утиліти


def _rel(p: Path) -> str:
    try:
        return str(p.resolve().relative_to(ROOT))
    except ValueError:
        return str(p)


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_text(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return path


def _write_json(path: Path, doc: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2 | orjson.OPT_NON_STR_KEYS) + b"\n")
    tmp.replace(path)
    return path


def _fmt(x: float | Decimal | None, nd: int = 4) -> str:
    if x is None:
        return "—"
    return f"{float(x):.{nd}f}"


def _md_table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> list[str]:
    out = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return out


@dataclass
class HttpLog:
    """httpx event hook: кожна відповідь (шлях, статус, X-MBX-USED-WEIGHT-1M) — для звіту про вагу."""

    calls: list[tuple[str, int, int | None]] = field(default_factory=list)

    async def on_response(self, resp: httpx.Response) -> None:
        raw = resp.headers.get(USED_WEIGHT_HEADER)
        used = int(raw) if raw is not None and raw.isascii() and raw.isdigit() else None
        self.calls.append((resp.request.url.path, resp.status_code, used))

    def by_path(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for path, _, _ in self.calls:
            out[path] = out.get(path, 0) + 1
        return dict(sorted(out.items()))

    def statuses(self) -> dict[int, int]:
        out: dict[int, int] = {}
        for _, st, _ in self.calls:
            out[st] = out.get(st, 0) + 1
        return dict(sorted(out.items()))

    def max_used_weight(self) -> int | None:
        used = [u for _, _, u in self.calls if u is not None]
        return max(used) if used else None


def _http(timeout: float, log: HttpLog) -> httpx.AsyncClient:
    import httpx  # noqa: PLC0415 — мережа лише для мережевих команд (dry-run її не імпортує)

    return httpx.AsyncClient(timeout=timeout, event_hooks={"response": [log.on_response]})


def _db_url(args: argparse.Namespace, settings: Settings) -> str:
    return str(args.database_url or settings.database_url)


def _engine(url: str) -> AsyncEngine:
    from fuzzhelm.storage.session import (  # noqa: PLC0415 — sqlalchemy лише для БД-команд
        APP_ROLE,
        make_engine,
    )

    # роль застосунку: найменші достатні права (UPDATE/DELETE журналу СУБД відхилить)
    return make_engine(url, role=APP_ROLE, null_pool=True)


def _factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    from fuzzhelm.storage.session import session_factory  # noqa: PLC0415

    return session_factory(engine)


# ================================================================== backfill


async def cmd_backfill(args: argparse.Namespace) -> int:
    settings = get_settings()
    symbols = tuple(args.symbols)
    if args.dry_run:
        end_ms = utc_date_ms(args.end_date) if args.end_date else day_floor_ms(time.time_ns() // NS_PER_MS)
        plan = plan_backfill(DatasetWindow.ending_at(end_ms, args.days, symbols), args.limit)
        print("DRY RUN (no network, no DB)" + ("" if args.end_date else "; end = today's UTC midnight "
                                                "by the LOCAL clock (the real run uses exchange time)"))
        print("\n".join(plan.lines()))
        return 0

    from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
    from fuzzhelm.ingest.backfill import backfill_klines, fill_gaps  # noqa: PLC0415
    from fuzzhelm.ingest.normalize import normalize_exchange_info  # noqa: PLC0415
    from fuzzhelm.ingest.ratelimit import binance_request_bucket  # noqa: PLC0415
    from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415
    from fuzzhelm.storage.repositories import CandleRepo, GapRepo, InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    clock = SystemClock()
    log = HttpLog()
    engine = _engine(_db_url(args, settings))
    factory = _factory(engine)
    t_all = time.perf_counter()
    started_utc = _now_utc_iso()
    per_symbol: dict[str, dict[str, Any]] = {}
    try:
        async with _http(args.timeout, log) as http:
            bn = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                                   RetryPolicy(rng_seed=settings.seed), clock=clock)
            offset_ms = await bn.server_time_offset_ms(TIME_SAMPLES)
            now_ms = clock.now_ns() // NS_PER_MS + offset_ms          # «зараз» біржі
            end_ms = utc_date_ms(args.end_date) if args.end_date else day_floor_ms(now_ms)
            window = DatasetWindow.ending_at(end_ms, args.days, symbols)
            if window.end_ms > now_ms:
                raise CliError(f"window end {utc_iso(window.end_ms)} is in the future (exchange time "
                               f"{utc_iso(now_ms)})")
            plan = plan_backfill(window, args.limit)
            print("\n".join(plan.lines()))
            instruments = normalize_exchange_info(await bn.exchange_info(), symbols)
            async with session_scope(factory) as s:
                repo_i = InstrumentRepo(s)
                ids = {sym: await repo_i.upsert(instruments[sym], spec_fetched_at_ns=clock.now_ns())
                       for sym in symbols}

            for sym in symbols:
                inst, iid = instruments[sym], ids[sym]
                acc = {"inserted": 0, "updated": 0, "skipped": 0, "pages_written": 0}

                async def on_page(candles: list[Candle], iid: int = iid, acc: dict[str, int] = acc) -> None:
                    async with session_scope(factory) as s:
                        r = await CandleRepo(s).upsert(candles, iid)
                    acc["inserted"] += r.inserted
                    acc["updated"] += r.updated
                    acc["skipped"] += r.skipped
                    acc["pages_written"] += 1

                req0, w0 = bn.requests_sent, bn.bucket.acquired_weight
                t0 = time.perf_counter()
                res = await backfill_klines(bn, sym, window.start_ms, window.last_open_ms, limit=args.limit,
                                            instrument=inst, now_ms=now_ms, on_page=on_page)
                elapsed = time.perf_counter() - t0
                gap_rows = await _record_backfill_gaps(
                    factory=factory, fill_gaps=fill_gaps, client=bn, symbol=sym, instrument=inst,
                    instrument_id=iid, result=res, now_ms=now_ms, now_ns=clock.now_ns())
                async with session_scope(factory) as s:
                    repo = CandleRepo(s)
                    arrays = await repo.load_arrays(iid, window.tf, window.start_ms * NS_PER_MS,
                                                    window.end_ms * NS_PER_MS)
                    db_gaps = await repo.find_gaps(iid, window.tf, MIN_MS * NS_PER_MS,
                                                   window.start_ms * NS_PER_MS, window.end_ms * NS_PER_MS)
                seams: dict[str, int] = {}
                for seg in res.segments:
                    key = "first" if seg.seam is None else seg.seam.value
                    seams[key] = seams.get(key, 0) + 1
                cols = arrays.columns()
                per_symbol[sym] = {
                    "symbol_canon": inst.symbol_canon, "instrument_id": iid,
                    "rows_fetched": len(res.candles), "requests": bn.requests_sent - req0,
                    "weight_charged": bn.bucket.acquired_weight - w0, "elapsed_s": round(elapsed, 3),
                    "seams": seams, "overlap_mismatches": len(res.overlap_mismatches),
                    "gaps_detected": [
                        {"ts_lo_utc": utc_iso(g.ts_lo_ns // NS_PER_MS),
                         "ts_hi_utc": utc_iso(g.ts_hi_ns // NS_PER_MS),
                         "expected_count": g.expected_count} for g in res.gaps],
                    "gap_rows": gap_rows, **acc,
                    "rows_in_db_window": len(arrays), "db_gaps_in_window": len(db_gaps),
                    "first_open_utc": utc_iso(int(arrays.t_ns[0]) // NS_PER_MS) if len(arrays) else None,
                    "last_open_utc": utc_iso(int(arrays.t_ns[-1]) // NS_PER_MS) if len(arrays) else None,
                    "dataset_hash": dataset_hash(cols), "dataset_hash_columns": sorted(cols),
                    "tick_size": dec_str(inst.tick_size), "step_size": dec_str(inst.step_size),
                    "min_notional": dec_str(inst.min_notional),
                }
                print(f"{sym}: {len(res.candles)} bars fetched in {elapsed:.1f} s, "
                      f"{per_symbol[sym]['requests']} requests, gaps={len(res.gaps)}, "
                      f"rows in DB window={len(arrays)}")
            async with session_scope(factory) as s:
                total_candles = await CandleRepo(s).count()
                gap_stats = await GapRepo(s).stats()
    finally:
        await engine.dispose()

    stats = {
        "window": window.to_dict(), "clock_offset_ms": offset_ms, "exchange_now_utc": utc_iso(now_ms),
        "run_started_utc": started_utc, "run_finished_utc": _now_utc_iso(),
        "end_date_explicit": args.end_date is not None,
        "elapsed_total_s": round(time.perf_counter() - t_all, 3),
        "plan": {"requests": plan.total_requests, "weight": plan.total_weight,
                 "pages_per_symbol": plan.pages_per_symbol, "limit": plan.limit},
        "http": {"by_path": log.by_path(), "statuses": {str(k): v for k, v in log.statuses().items()},
                 "max_used_weight_1m_header": log.max_used_weight(),
                 "weight_charged_total": bn.bucket.acquired_weight,
                 "bucket_wait_s": round(bn.bucket.total_wait_s, 3),
                 "retry_sleeps": len(bn.retry.sleeps)},
        "symbols": per_symbol, "db": {"candle_total": total_candles, "ingest_gap_by_status": gap_stats},
        "command": "uv run fuzzhelm " + " ".join(args.argv),
    }
    _write_json(args.window_json, window_document(window, stats))
    _write_text(args.report, backfill_report_md(stats))
    print(f"candle rows in DB: {total_candles} (phase-1 gate: >= 60000 → "
          f"{'PASS' if total_candles >= 60_000 else 'FAIL'})")
    print(f"wrote {_rel(args.window_json)}, {_rel(args.report)}")
    return 0 if total_candles >= 60_000 else 1


async def _record_backfill_gaps(*, factory: async_sessionmaker[AsyncSession],
                                fill_gaps: Callable[..., Awaitable[Any]], client: Any, symbol: str,
                                instrument: Instrument, instrument_id: int, result: Any, now_ms: int,
                                now_ns: int) -> list[dict[str, Any]]:
    """Прогалини добору → рядки ingest_gap (OPEN → FILLING) → fill_gaps → кінцевий статус; додане — в БД."""
    from fuzzhelm.storage.repositories import CandleRepo, GapRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    res, iid = result, instrument_id
    if not res.gaps:
        return []
    db_ids: list[int] = []
    async with session_scope(factory) as s:
        repo = GapRepo(s)
        for g in res.gaps:
            gid = await repo.open(iid, g.stream, g.ts_lo_ns, g.ts_hi_ns, expected_count=g.expected_count,
                                  detector=g.detector, detected_at_ns=now_ns)
            await repo.update_status(gid, GapStatus.FILLING, count_attempt=True)
            db_ids.append(gid)
    rep = await fill_gaps(client, symbol, res.candles, res.gaps, instrument=instrument, now_ms=now_ms)
    out: list[dict[str, Any]] = []
    async with session_scope(factory) as s:
        if rep.added:
            await CandleRepo(s).upsert(list(rep.added), iid)
        repo = GapRepo(s)
        for gid, g in zip(db_ids, rep.gaps, strict=True):
            row = await repo.update_status(gid, g.status, filled_rows=g.filled_rows, count_attempt=False)
            out.append({"id": gid, "status": row.status, "expected_count": g.expected_count,
                        "filled_rows": g.filled_rows, "ts_lo_utc": utc_iso(g.ts_lo_ns // NS_PER_MS),
                        "ts_hi_utc": utc_iso(g.ts_hi_ns // NS_PER_MS)})
    return out


def window_document(window: DatasetWindow, stats: Mapping[str, Any]) -> dict[str, Any]:
    """Машиночитне вікно датасету (data/dataset_window.json): джерело calibrate, fetch-funding, бектесту."""
    wf = (load_yaml("profiles/backtest").get("walkforward") or {})
    is_days = int(wf.get("is_days", 15))
    lo, hi = window.sub_window(1, is_days)
    ending = ("at the explicit --end-date" if stats.get("end_date_explicit")
              else "at the last full UTC day before the backfill run")
    return {
        "v": 1,
        "definition": (f"{window.days} full UTC days ending {ending} (end exclusive); "
                       "bars are Binance USD-M 1m klines, closed only"),
        "window": window.to_dict(),
        "first_is_window": {"days": [1, is_days], "start_ms": lo, "end_ms": hi, "start_utc": utc_iso(lo),
                            "end_utc_exclusive": utc_iso(hi),
                            "note": "calibration uses ONLY this window: every walk-forward OOS window starts "
                                    "after it (config/profiles/backtest.yaml, docs/deviations.md D-03)"},
        "symbols": {sym: {k: st[k] for k in ("symbol_canon", "rows_in_db_window", "first_open_utc",
                                              "last_open_utc", "dataset_hash", "dataset_hash_columns")}
                    for sym, st in stats["symbols"].items()},
        "dataset_hash_method": "backtest.manifest.dataset_hash(CandleRepo.load_arrays(...).columns())",
        "created_utc": stats.get("run_finished_utc", stats["run_started_utc"]),
        "command": stats["command"],
    }


def backfill_report_md(st: Mapping[str, Any]) -> str:
    w = st["window"]
    lines = [
        "# Добір історії: 45 днів 1m-свічок Binance USDⓈ-M у PostgreSQL",
        "",
        f"Згенеровано `{st['command'].strip()}` (запуск {st['run_started_utc']}, завершення "
        f"{st.get('run_finished_utc', '—')}, час біржі на початку {st['exchange_now_utc']}, зсув локального "
        f"годинника {st['clock_offset_ms']} мс). Автор: Андрій Жук, 2026.",
        "",
        "## Зафіксоване вікно датасету",
        "",
        f"* `[{w['start_utc']}, {w['end_utc_exclusive']})` — {w['days']} повних UTC-днів, що закінчуються "
        + ("заданою `--end-date`" if st.get("end_date_explicit") else
           "останнім повним UTC-днем перед запуском")
        + f"; tf = {w['tf']}; {w['bars_per_symbol']} барів на символ.",
        f"* Останній бар: open_time = {w['last_open_utc']}. Машиночитна копія — `data/dataset_window.json`.",
        "",
        "## Результат за символами",
        "",
    ]
    rows = []
    for sym, s in st["symbols"].items():
        seams = ", ".join(f"{k}×{v}" for k, v in s["seams"].items())
        rows.append([f"{sym} ({s['symbol_canon']})", s["rows_fetched"], s["inserted"], s["updated"],
                     s["skipped"], s["requests"], s["weight_charged"], f"{s['elapsed_s']:.1f}", seams,
                     s["overlap_mismatches"], len(s["gaps_detected"]), s["rows_in_db_window"],
                     s["db_gaps_in_window"]])
    lines += _md_table(["символ", "отримано барів", "inserted", "updated", "skipped", "запитів",
                        "вага (облік)",
                        "час, с", "стики сторінок", "MISMATCH", "прогалин", "рядків у БД (вікно)",
                        "дірок у БД (вікно)"], rows)
    lines += ["", "Хеш датасету (BLAKE2b-256, `backtest.manifest.dataset_hash` над колонками "
              "`c, h, l, o, t_ns, v` з `CandleRepo.load_arrays`):", ""]
    lines += [f"* {sym}: `{s['dataset_hash']}` (перший бар {s['first_open_utc']}, останній "
              f"{s['last_open_utc']}; "
              f"tick {s['tick_size']}, step {s['step_size']}, minNotional {s['min_notional']})"
              for sym, s in st["symbols"].items()]
    gaps = [(sym, g) for sym, s in st["symbols"].items() for g in s["gap_rows"]]
    lines += ["", "## Прогалини", ""]
    if gaps:
        lines += _md_table(["символ", "ingest_gap.id", "перший відсутній", "останній відсутній", "очікувано",
                            "добрано", "статус"],
                           [[sym, g["id"], g["ts_lo_utc"], g["ts_hi_utc"], g["expected_count"],
                             g["filled_rows"], g["status"]] for sym, g in gaps])
    else:
        mism = sum(s["overlap_mismatches"] for s in st["symbols"].values())
        seams = ("стики всіх сторінок збіглися дослівно (MISMATCH = 0)" if mism == 0 else
                 f"на стиках сторінок {mism} розбіжностей вмісту (MISMATCH; збережено новішу версію бару)")
        lines.append("Біржа віддала всі бари вікна: прогалин за неперервністю open_time не виявлено, "
                     f"{seams}, тому рядків `ingest_gap` цей прогін не створив. Реальні рядки FILLED — з "
                     "реплею `gap.jsonl.gz` (`docs/figures/backfill_gap_replay.md`).")
    h = st["http"]
    lines += [
        "",
        "## Вага запитів і час",
        "",
        f"* Запитів за шляхами: {', '.join(f'`{p}` × {n}' for p, n in h['by_path'].items())}; "
        f"статуси HTTP: {', '.join(f'{k} × {v}' for k, v in h['statuses'].items())}; пауз повтору: "
        f"{h['retry_sleeps']}.",
        f"* Вага за власним обліком token bucket: {h['weight_charged_total']} (план {st['plan']['weight']}); "
        f"максимум заголовка `X-MBX-USED-WEIGHT-1M` за прогін: {h['max_used_weight_1m_header']} із 2400; "
        f"очікування у відрі: {h['bucket_wait_s']} с.",
        f"* Загальний час прогону (разом із записом у БД через COPY-upsert): {st['elapsed_total_s']:.1f} с.",
        "",
        "## Стан БД після прогону",
        "",
        f"* `candle`: {st['db']['candle_total']} рядків — gate фази 1 (≥ 60 000): "
        f"**{'виконано' if st['db']['candle_total'] >= 60_000 else 'НЕ виконано'}**.",
        f"* `ingest_gap` за статусами: {st['db']['ingest_gap_by_status'] or '{}'}.",
        "",
    ]
    return "\n".join(lines)


# ================================================================== crosscheck


async def capture_crosscheck_input(settings: Settings, timeout: float, log: HttpLog) -> dict[str, Any]:
    """Сирі відповіді обох бірж для одного вікна (зберігаються, щоб звіт можна було перерахувати офлайн)."""
    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
    from fuzzhelm.ingest.kraken_client import KrakenRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.ratelimit import binance_request_bucket, kraken_public_bucket  # noqa: PLC0415
    from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415
    from fuzzhelm.ingest.symbols import kraken_result_key  # noqa: PLC0415

    clock = SystemClock()
    async with _http(timeout, log) as http:
        kr = KrakenRestClient(settings.kraken_rest_base, http, RetryPolicy(rng_seed=settings.seed),
                              bucket=kraken_public_bucket(clock), clock=clock)
        bn = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                               RetryPolicy(rng_seed=settings.seed), clock=clock)
        offset = await bn.server_time_offset_ms(TIME_SAMPLES)
        k_ingest = clock.now_ns()
        kres = await kr.ohlc("XBTUSD", 1)
        ures = await kr.ohlc("USDTUSD", 1)
        opens = [int(r[0]) for r in kres[kraken_result_key("XBTUSD")]]
        lo_ms, hi_ms = min(opens) * 1000, max(opens) * 1000
        b_ingest = clock.now_ns()
        server_ms = b_ingest // NS_PER_MS + offset
        rows = await bn.klines("BTCUSDT", "1m", start_ms=lo_ms, end_ms=hi_ms, limit=1000)
        idx = await bn.index_price_klines("BTCUSDT", "1m", start_ms=lo_ms, end_ms=hi_ms, limit=1000)
        prem = await bn.premium_index("BTCUSDT")
    return {
        "v": 1, "captured_utc": _now_utc_iso(), "clock_offset_ms": offset,
        "kraken": {"pair": "XBTUSD", "interval": 1, "ts_ingest_ns": k_ingest, "result": kres},
        "kraken_usdtusd": {"pair": "USDTUSD", "interval": 1, "result": ures},
        "binance": {"symbol": "BTCUSDT", "interval": "1m", "ts_ingest_ns": b_ingest,
                    "server_time_ms": server_ms, "rows": rows},
        "binance_index": {"pair": "BTCUSDT", "interval": "1m", "rows": idx},
        "premium_index": prem,
    }


def _order_stat(sorted_vals: Sequence[Decimal], q: Decimal) -> Decimal:
    """Порядкова статистика за найближчим рангом: ⌈q·n⌉-й елемент (q ∈ (0, 1])."""
    n = len(sorted_vals)
    rank = max(1, math.ceil(q * n))
    return sorted_vals[rank - 1]


def _median(sorted_vals: Sequence[Decimal]) -> Decimal:
    n = len(sorted_vals)
    mid = n // 2
    return sorted_vals[mid] if n % 2 else (sorted_vals[mid - 1] + sorted_vals[mid]) / 2


def _kraken_closes(result: Mapping[str, Any]) -> dict[int, Decimal]:
    """{open_ms: close} для ЗАКРИТИХ барів сирого OHLC Kraken (без нормалізатора: пари немає в реєстрі)."""
    last = int(result["last"])
    keys = [k for k in result if k != "last"]
    if len(keys) != 1:
        raise ValueError(f"expected exactly one pair key in Kraken result, got {keys}")
    return {int(r[0]) * 1000: dec(str(r[4])) for r in result[keys[0]] if int(r[0]) <= last}


@dataclass(frozen=True)
class CrosscheckAnalysis:
    report: CrosscheckReport
    stats: dict[str, Any]
    decomposition: dict[str, Any]
    series: dict[str, list[float]]          # для рисунка: t_ms, d_bps, premium, usdt, residual


def analyze_crosscheck(raw: Mapping[str, Any], threshold_bps: Decimal | int | str = 50) -> CrosscheckAnalysis:
    """Чисте обчислення зі збережених сирих відповідей (той самий вхід → той самий звіт, офлайн)."""
    from fuzzhelm.ingest.crosscheck import crosscheck  # noqa: PLC0415
    from fuzzhelm.ingest.normalize import normalize_kraken_ohlc, normalize_rest_klines  # noqa: PLC0415
    from fuzzhelm.ingest.symbols import BTC_USD_SPOT, BTC_USDT_PERP  # noqa: PLC0415

    k = raw["kraken"]
    b = raw["binance"]
    kraken = [c for c in normalize_kraken_ohlc(k["result"], BTC_USD_SPOT, int(k["ts_ingest_ns"]))
              if c.is_closed]
    binance = [c for c in normalize_rest_klines(b["rows"], BTC_USDT_PERP, int(b["ts_ingest_ns"]),
                                                server_time_ms=int(b["server_time_ms"])) if c.is_closed]
    rep = crosscheck(binance, kraken, threshold_bps)
    d = [r.diff_bps for r in rep.rows]
    ad = sorted(abs(x) for x in d)
    ds = sorted(d)
    thr = dec(str(threshold_bps))
    stats: dict[str, Any] = {
        "kraken_bars": len(k["result"][next(key for key in k["result"] if key != "last")]),
        "kraken_closed": len(kraken), "binance_closed": len(binance), "matched": rep.n_matched,
        "missing_in_binance": len(rep.missing_in_binance), "missing_in_kraken": len(rep.missing_in_kraken),
        "window_first_utc": utc_iso(rep.rows[0].open_time_ns // NS_PER_MS) if rep.rows else None,
        "window_last_utc": utc_iso(rep.rows[-1].open_time_ns // NS_PER_MS) if rep.rows else None,
        "threshold_bps": thr, "mean_bps": rep.mean_bps, "mean_abs_bps": rep.mean_abs_bps,
        "median_bps": _median(ds) if ds else None, "median_abs_bps": _median(ad) if ad else None,
        "p95_abs_bps": _order_stat(ad, Decimal("0.95")) if ad else None,
        "min_bps": ds[0] if ds else None, "max_bps": ds[-1] if ds else None,
        "max_abs_bps": rep.max_abs_bps, "count_above_threshold": sum(1 for x in ad if x > thr),
        "flagged": len(rep.flagged),
    }
    # розклад у лог-б.п. (точно адитивний): ln(P_b/P_k) = ln(P_b/I) − ln(r) + ln(I·r/P_k)
    idx = {int(r[0]): dec(str(r[4])) for r in raw.get("binance_index", {}).get("rows", [])}
    usdt = _kraken_closes(raw["kraken_usdtusd"]["result"]) if "kraken_usdtusd" in raw else {}
    t_ms: list[float] = []
    tot: list[float] = []
    prem: list[float] = []
    usd: list[float] = []
    resid: list[float] = []
    for r in rep.rows:
        om = r.open_time_ns // NS_PER_MS
        i_px, rate = idx.get(om), usdt.get(om)
        if i_px is None or rate is None:
            continue
        pb, pk = float(r.binance_close), float(r.kraken_close)
        fi, fr = float(i_px), float(rate)
        t_ms.append(float(om))
        tot.append(1e4 * math.log(pb / pk))
        prem.append(1e4 * math.log(pb / fi))
        usd.append(-1e4 * math.log(fr))
        resid.append(1e4 * math.log(fi * fr / pk))

    def _summ(xs: list[float]) -> dict[str, float | None]:
        if not xs:
            return {"mean": None, "median": None, "std": None}
        a = np.asarray(xs)
        std = float(a.std(ddof=1)) if a.size > 1 else 0.0
        return {"mean": float(a.mean()), "median": float(np.median(a)), "std": std}

    identity_err = max((abs(t - (p + u + e)) for t, p, u, e in zip(tot, prem, usd, resid, strict=True)),
                       default=None)
    decomp = {"n": len(tot), "total_log_bps": _summ(tot), "perp_premium_bps": _summ(prem),
              "usdt_discount_bps": _summ(usd), "residual_bps": _summ(resid), "identity_max_err": identity_err}
    series = {"t_ms": [float(r.open_time_ns // NS_PER_MS) for r in rep.rows],
              "d_bps": [float(x) for x in d], "dec_t_ms": t_ms, "premium": prem, "usdt": usd,
              "residual": resid}
    return CrosscheckAnalysis(rep, stats, decomp, series)


def crosscheck_report_md(a: CrosscheckAnalysis, raw: Mapping[str, Any], *, input_path: str,
                         command: str) -> str:
    s, dcmp = a.stats, a.decomposition
    prem = raw.get("premium_index") or {}
    lines = [
        "# Крос-звірка двох незалежних джерел: Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT",
        "",
        f"Згенеровано `{command}` (сирі відповіді збережено {raw['captured_utc']} у `{input_path}`; "
        f"`--from-input` перераховує цей звіт офлайн побайтово тим самим кодом). Автор: Андрій Жук, 2026.",
        "",
        "Вікно — останні закриті 1m-бари, які віддає Kraken (`/0/public/OHLC` повертає не більше 720 "
        "останніх "
        f"барів): {s['window_first_utc']} … {s['window_last_utc']}. Звіряються закриття свічок з однаковим "
        f"`open_time` (`ingest.crosscheck`, Decimal); d = 10⁴·(P_Binance − P_Kraken)/P_Kraken, поріг "
        f"|d| > {s['threshold_bps']} б.п.",
        "",
        "## Статистика розбіжності (б.п.)",
        "",
    ]
    lines += _md_table(["показник", "значення"], [
        ["барів Kraken у відповіді / закритих", f"{s['kraken_bars']} / {s['kraken_closed']}"],
        ["закритих барів Binance за те саме вікно", s["binance_closed"]],
        ["звірено хвилин (спільний open_time)", s["matched"]],
        ["бракує в Binance / в Kraken (у спільному вікні)",
         f"{s['missing_in_binance']} / {s['missing_in_kraken']}"],
        ["середнє d", _fmt(s["mean_bps"], 2)],
        ["медіана d", _fmt(s["median_bps"], 2)],
        ["min d / max d", f"{_fmt(s['min_bps'], 2)} / {_fmt(s['max_bps'], 2)}"],
        ["середнє |d|", _fmt(s["mean_abs_bps"], 2)],
        ["медіана |d|", _fmt(s["median_abs_bps"], 2)],
        ["p95 |d| (найближчий ранг)", _fmt(s["p95_abs_bps"], 2)],
        ["max |d|", _fmt(s["max_abs_bps"], 2)],
        [f"хвилин з |d| > {s['threshold_bps']} б.п.", s["count_above_threshold"]],
    ])
    top = sorted(a.report.rows, key=lambda r: abs(r.diff_bps), reverse=True)[:5]
    lines += ["", "П'ять хвилин з найбільшою |d|:", ""]
    lines += _md_table(["open_time (UTC)", "Binance close, USDT", "Kraken close, USD", "d, б.п."],
                       [[utc_iso(r.open_time_ns // NS_PER_MS), dec_str(r.binance_close),
                         dec_str(r.kraken_close), _fmt(r.diff_bps, 2)] for r in top])

    def row(name: str, key: str) -> list[str]:
        v = dcmp[key]
        return [name, _fmt(v["mean"], 2), _fmt(v["median"], 2), _fmt(v["std"], 2)]

    lines += [
        "",
        "## Чому розбіжність не нульова: база порівняння",
        "",
        "Порівнюються **різні інструменти**: безстроковий ф'ючерс Binance, номінований у USDT, і спот "
        "Kraken у "
        "доларах США. Тому d — не «похибка даних», а сума трьох економічно різних складових (лог-б.п., "
        "тотожність точна: ln(P_b/P_k) = ln(P_b/I) − ln r + ln(I·r/P_k)):",
        "",
        "* **премія перпетуала** ln(P_b/I): відхилення ціни ф'ючерса від індексу Binance I (кошик спотових "
        "цін BTC у USDT, `/fapi/v1/indexPriceKlines`); саме її гасить механізм фінансування;",
        "* **дисконт USDT** −ln r, де r — закриття USDT/USD на Kraken за ту саму хвилину: ціна в USDT "
        "вища за ціну в USD, коли 1 USDT < 1 USD;",
        "* **залишок** ln(I·r/P_k): міжбіржова різниця спотових цін у доларах + неодночасність «закриттів» "
        "(close — ціна останньої угоди хвилини, на двох біржах це різні моменти) + ліквідність Kraken.",
        "",
        f"Розклад на {dcmp['n']} хвилинах, де є всі чотири ціни (макс. похибка тотожності "
        f"{_fmt(dcmp['identity_max_err'], 12) if dcmp['identity_max_err'] is not None else '—'} б.п.):",
        "",
    ]
    lines += _md_table(["складова, б.п.", "середнє", "медіана", "σ"], [
        row("разом ln(P_b/P_k)", "total_log_bps"), row("премія перпетуала", "perp_premium_bps"),
        row("дисконт USDT", "usdt_discount_bps"), row("залишок (спот USD ↔ спот USD)", "residual_bps")])
    if prem:
        mark, index = dec(str(prem["markPrice"])), dec(str(prem["indexPrice"]))
        lines += ["", f"Знімок `premiumIndex` у момент запису: mark {dec_str(mark)}, index {dec_str(index)} "
                  f"(премія {_fmt((mark - index) / index * 10_000, 2)} б.п.), остання ставка фінансування "
                  f"{prem['lastFundingRate']}.", ""]
    structural = None
    if dcmp["n"]:
        structural = dcmp["perp_premium_bps"]["mean"] + dcmp["usdt_discount_bps"]["mean"]
    mx = s["max_abs_bps"]
    ratio = f"{float(s['threshold_bps']) / float(mx):.1f}×" if mx else "—"
    lines += [
        f"Висновок: на цьому вікні середня структурна складова (премія + дисконт USDT) дорівнює "
        f"{_fmt(structural, 2)} б.п., а найбільша розбіжність |d| = {_fmt(mx, 2)} б.п. — поріг "
        f"{s['threshold_bps']} б.п. лежить у {ratio} вище за максимум, тож його перевищення вказувало б на "
        "збій "
        "одного з джерел (застиглий тик, пропуск, помилковий масштаб), а не на різницю інструментів. "
        "Обмеження: "
        "це одне 12-годинне вікно (Kraken не віддає довшої 1m-історії); у періоди стресу премія перпетуала і "
        "дисконт USDT можуть бути значно більшими. Рисунок: `docs/figures/crosscheck_divergence.png`.",
        "",
    ]
    return "\n".join(lines)


def plot_crosscheck(a: CrosscheckAnalysis, out: Path) -> Path:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.dates as mdates  # noqa: PLC0415
    import matplotlib.pyplot as plt  # noqa: PLC0415

    def _dt(ms: list[float]) -> list[datetime]:
        return [datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(x)) for x in ms]

    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(11.5, 6.8), sharex=True, layout="constrained")
    thr = float(a.stats["threshold_bps"])
    ax1.axhspan(-thr, thr, color=GRID, alpha=0.35, linewidth=0)
    for y in (-thr, thr):
        ax1.axhline(y, color=MUTED, linewidth=0.8, linestyle=(0, (4, 3)))
    ax1.plot(_dt(a.series["t_ms"]), a.series["d_bps"], color=SERIES[0], linewidth=1.2)
    ax1.set_ylabel("d, б.п.", color=INK_2)
    ax1.set_title("Розбіжність закриттів 1m: Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT "
                  f"(смуга — поріг ±{thr:g} б.п.)", loc="left", fontsize=10, color=INK)
    t = _dt(a.series["dec_t_ms"])
    for (name, key), color in zip((("премія перпетуала", "premium"), ("дисконт USDT", "usdt"),
                                   ("залишок (спот ↔ спот)", "residual")), SERIES, strict=True):
        ax2.plot(t, a.series[key], color=color, linewidth=1.2, label=name)
    ax2.axhline(0, color=AXIS, linewidth=0.8)
    ax2.set_ylabel("складова, лог-б.п.", color=INK_2)
    ax2.set_title("Розклад: ln(P_b/P_k) = премія + дисконт USDT + залишок", loc="left", fontsize=10,
                  color=INK)
    ax2.legend(loc="lower right", bbox_to_anchor=(1.0, 1.0), frameon=False, fontsize=8, ncols=3,
               labelcolor=INK_2, borderaxespad=0.2)
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=UTC))  # type: ignore[no-untyped-call]
    ax2.set_xlabel("час, UTC", color=INK_2)
    for ax in (ax1, ax2):
        _style_axes(ax)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out


def _style_axes(ax: Any) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(0.8)
    ax.grid(True, color=GRID, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.tick_params(colors=INK_2, labelsize=8, length=3, color=AXIS)


def _load_raw(path: Path) -> dict[str, Any]:
    data = path.read_bytes()
    if path.suffix == ".gz":
        data = gzip.decompress(data)
    doc = orjson.loads(data)
    if not isinstance(doc, dict) or doc.get("v") != 1:
        raise CliError(f"{_rel(path)} is not a crosscheck input v1")
    return doc


async def cmd_crosscheck(args: argparse.Namespace) -> int:
    settings = get_settings()
    if args.dry_run and args.from_input is None:
        print("DRY RUN (no network): would request")
        print(f"  Kraken  {settings.kraken_rest_base}/0/public/OHLC pair=XBTUSD interval=1 "
              "(≤ 720 last bars)")
        print(f"  Kraken  {settings.kraken_rest_base}/0/public/OHLC pair=USDTUSD interval=1")
        print(f"  Binance {settings.binance_rest_base}/fapi/v1/time × {TIME_SAMPLES}, /fapi/v1/klines "
              f"(limit=1000, weight {klines_weight(1000)}), /fapi/v1/indexPriceKlines (weight "
              f"{request_weight('/fapi/v1/indexPriceKlines', {'limit': 1000})}), /fapi/v1/premiumIndex")
        print(f"  then save raw input to {_rel(args.save_input)} and write {_rel(args.report)}, "
              f"{_rel(args.figure)}")
        return 0
    if args.from_input is not None:
        raw = _load_raw(args.from_input)
        input_path = _rel(args.from_input)
    else:
        log = HttpLog()
        raw = await capture_crosscheck_input(settings, args.timeout, log)
        raw["http"] = {"by_path": log.by_path(), "max_used_weight_1m_header": log.max_used_weight()}
        args.save_input.parent.mkdir(parents=True, exist_ok=True)
        args.save_input.write_bytes(gzip.compress(orjson.dumps(raw), mtime=0))
        input_path = _rel(args.save_input)
    a = analyze_crosscheck(raw, args.threshold_bps)
    if args.dry_run:
        printable = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in a.stats.items()}
        print(json.dumps(printable, indent=2))
        return 0
    command = f"uv run fuzzhelm crosscheck --from-input {input_path}"
    _write_text(args.report, crosscheck_report_md(a, raw, input_path=input_path, command=command))
    plot_crosscheck(a, args.figure)
    s = a.stats
    print(f"matched {s['matched']} minutes: mean d = {_fmt(s['mean_bps'], 2)} bps, p95 |d| = "
          f"{_fmt(s['p95_abs_bps'], 2)}, max |d| = {_fmt(s['max_abs_bps'], 2)}, > {s['threshold_bps']} bps: "
          f"{s['count_above_threshold']}")
    print(f"wrote {_rel(args.report)}, {_rel(args.figure)}")
    return 0


# ================================================================== fetch-funding


async def cmd_fetch_funding(args: argparse.Namespace) -> int:
    settings = get_settings()
    window, _ = load_window(args.window_json)
    symbols = tuple(args.symbols) if args.symbols else window.symbols
    lo, hi = window.start_ms, window.end_ms - 1
    if args.dry_run:
        print("DRY RUN (no network)")
        for sym in symbols:
            print(f"  GET {settings.binance_rest_base}/fapi/v1/fundingRate symbol={sym} startTime={lo} "
                  f"endTime={hi} limit=1000 → {_rel(args.out_dir / f'funding_{sym}.json')}")
        return 0

    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
    from fuzzhelm.ingest.funding import (  # noqa: PLC0415
        fetch_funding_history,
        funding_document,
        write_funding_json,
    )
    from fuzzhelm.ingest.ratelimit import binance_request_bucket  # noqa: PLC0415
    from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415
    from fuzzhelm.ingest.symbols import symbol_ref  # noqa: PLC0415

    clock = SystemClock()
    log = HttpLog()
    async with _http(args.timeout, log) as http:
        bn = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                               RetryPolicy(rng_seed=settings.seed), clock=clock)
        for sym in symbols:
            ref = symbol_ref(Venue.BINANCE_USDM, sym)
            rates, n_req = await fetch_funding_history(bn, ref, lo, hi)
            doc = funding_document(rates, ref, base_url=settings.binance_rest_base,
                                   window=window.to_dict(), fetched_at_utc=_now_utc_iso(), requests=n_req)
            path = write_funding_json(args.out_dir / f"funding_{sym}.json", doc)
            vals = [r.funding_rate for r in rates]
            total = sum(vals, Decimal(0))
            print(f"{sym}: {len(rates)} funding records "
                  f"[{utc_iso(rates[0].funding_time_ms) if rates else '—'} … "
                  f"{utc_iso(rates[-1].funding_time_ms) if rates else '—'}], "
                  f"min {min(vals) if vals else '—'}, max {max(vals) if vals else '—'}, "
                  f"sum {dec_str(total)}; {n_req} request(s) → {_rel(path)}")
    print(f"HTTP: {log.by_path()}; X-MBX-USED-WEIGHT-1M on fundingRate responses: "
          f"{[u for p, _, u in log.calls if p.endswith('fundingRate')]}")
    return 0


# ================================================================== calibrate


@dataclass(frozen=True)
class CalibrationSeries:
    """Входи калібрування по барах IS-вікна: NaN до прогріву (однакова множина барів для T і ознак)."""

    t_ns: npt.NDArray[np.int64]
    T: npt.NDArray[np.float64]
    R: npt.NDArray[np.float64]
    V: npt.NDArray[np.float64]
    vol_pct: npt.NDArray[np.float64]
    ema_slope_norm: npt.NDArray[np.float64]
    volume_z: npt.NDArray[np.float64]
    warmup_bars: int

    @property
    def n_valid(self) -> int:
        return int(np.isfinite(self.T).sum())


def calibration_series(bars: Sequence[Bar], detectors_cfg: Mapping[str, Any]) -> CalibrationSeries:
    """Бари → FeaturePipeline → 6 детекторів → decision.consensus: емпіричний T і вектори ознак режиму.

    Той самий код і ті самі параметри (config/detectors.yaml), що й у рушії рішень; бари до повного прогріву
    (max(warmup детекторів, max_lookback конвеєра)) відкидаються — там T = 0 «за замовчуванням», а не вимір.
    """
    from fuzzhelm.decision.aggregator import consensus  # noqa: PLC0415
    from fuzzhelm.detectors.registry import build_detectors, max_warmup  # noqa: PLC0415
    from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline  # noqa: PLC0415
    from fuzzhelm.features.window import BarWindow  # noqa: PLC0415
    from fuzzhelm.regimes.calibrate_mf import features_to_arrays  # noqa: PLC0415

    params = FeatureParams.from_config(detectors_cfg)
    pipe = FeaturePipeline(params)
    detectors = build_detectors(detectors_cfg)
    warm = max(max_warmup(detectors), pipe.max_lookback)
    n = len(bars)
    T = np.full(n, np.nan)
    R = np.full(n, np.nan)
    V = np.full(n, np.nan)
    window = BarWindow(capacity=64)
    feats = []
    prev_t: int | None = None
    for i, bar in enumerate(bars):
        if prev_t is not None and bar.t_ns <= prev_t:
            raise ValueError(f"bars must be strictly increasing in time (index {i})")
        prev_t = bar.t_ns
        f = pipe.update(bar)
        feats.append(f)
        window.append(bar, f)
        if i + 1 >= warm:
            cons = consensus([d.compute(window) for d in detectors])
            T[i], R[i], V[i] = cons.T, cons.R, cons.V
    vol, slope, vz = features_to_arrays(feats, horizon=params.ema_horizon)
    cut = min(n, warm - 1)
    for arr in (vol, slope, vz):
        arr[:cut] = np.nan
    t_ns = np.fromiter((b.t_ns for b in bars), dtype=np.int64, count=n)
    return CalibrationSeries(t_ns, T, R, V, vol, slope, vz, warm)


@dataclass(frozen=True)
class CalibrationRun:
    result: dict[str, Any]
    series: CalibrationSeries
    manifest: dict[str, Any]
    run_id: str


K_RANGE: Final = range(2, 7)
N_INIT: Final = 10
SILHOUETTE_SAMPLE: Final = 10_000
# σ гаусіан V: "cover" — покриття без мертвих зон (≥ e^−½), бо буквальне «0.5·d до найближчого» (§5.5) на
# реальних даних лишає max μ < 0.5 біля V = 1 (docs/deviations.d/data.md, DATA-05)
SIGMA_RULE: Final = "cover"
# T-точки — перцентилі симетризованої вибірки T ∪ −T: база правил і R непарно-симетричні, і дрейф одного
# 15-денного IS-вікна не має робити систему «довгою» чи «короткою» за побудовою МФ (DATA-06)
T_SYMMETRIC: Final = True


def manifest_id(manifest: Mapping[str, Any]) -> str:
    """Ідентифікатор калібрування = 'cal-' + перші 16 hex BLAKE2b-256 канонічного маніфесту."""
    from fuzzhelm.backtest.manifest import config_hash  # noqa: PLC0415

    return "cal-" + config_hash(manifest)[:16]


def run_calibration(bars: Sequence[Bar], *, seed: int, detectors_cfg: Mapping[str, Any],
                    dataset: Mapping[str, Any]) -> CalibrationRun:
    """Повний ланцюг калібрування над барами; `dataset` — опис даних (вікно, хеш) для маніфесту."""
    from fuzzhelm.backtest.manifest import config_hash  # noqa: PLC0415
    from fuzzhelm.features.pipeline import FeatureParams  # noqa: PLC0415
    from fuzzhelm.regimes.calibrate_mf import FEATURE_COLUMNS, T_PERCENTILES, V_K, calibrate  # noqa: PLC0415

    series = calibration_series(bars, detectors_cfg)
    manifest = {
        "kind": "mf_calibration", "v": 1, "dataset": dict(dataset), "n_bars": len(bars),
        "warmup_bars": series.warmup_bars, "seed": seed,
        "detectors_config_hash": config_hash(detectors_cfg),
        "feature_params": asdict(FeatureParams.from_config(detectors_cfg)),
        "method": {"T": {"source": "decision.aggregator.consensus", "percentiles": list(T_PERCENTILES),
                         "symmetrized": T_SYMMETRIC},
                   "V": {"kmeans_k": V_K, "features": list(FEATURE_COLUMNS), "k_range": list(K_RANGE),
                         "n_init": N_INIT, "standardize": True, "silhouette_sample": SILHOUETTE_SAMPLE,
                         "sigma_rule": SIGMA_RULE}},
    }
    run_id = manifest_id(manifest)
    result = calibrate(series.vol_pct, series.ema_slope_norm, series.volume_z, series.T, seed=seed,
                       k_range=K_RANGE, n_init=N_INIT, run_id=run_id, silhouette_sample=SILHOUETTE_SAMPLE,
                       sigma_rule=SIGMA_RULE, t_symmetric=T_SYMMETRIC)
    return CalibrationRun(result, series, manifest, run_id)


def cluster_labels(series: CalibrationSeries, centroids: Sequence[Sequence[float]]
                   ) -> tuple[npt.NDArray[np.float64], npt.NDArray[np.int64]]:
    """(X відфільтрований, мітки) — найближчий центроїд у стандартизованих координатах (як у fit_regimes)."""
    X = np.column_stack([series.vol_pct, series.ema_slope_norm, series.volume_z])
    X = X[np.all(np.isfinite(X), axis=1)]
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd > 0.0, sd, 1.0)
    Z, C = (X - mu) / sd, (np.asarray(centroids, dtype=np.float64) - mu) / sd
    d2 = ((Z[:, None, :] - C[None, :, :]) ** 2).sum(axis=2)
    return X, np.argmin(d2, axis=1).astype(np.int64)


FEATURE_LABELS: Final = ("vol_pct", "нахил EMA", "z обсягу")


def standardized_centroids(series: CalibrationSeries, centroids: Sequence[Sequence[float]]
                           ) -> npt.NDArray[np.float64]:
    """Центроїди в стандартизованих одиницях (x − μ)/σ по колонках IS-вибірки — як їх бачить KMeans."""
    X, _ = cluster_labels(series, centroids)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd = np.where(sd > 0.0, sd, 1.0)
    return (np.asarray(centroids, dtype=np.float64) - mu) / sd


def cluster_interpretation_md(series: CalibrationSeries, centroids: Sequence[Sequence[float]]) -> list[str]:
    """Яка ознака «тримає» кожен кластер: найбільша |координата| стандартизованого центроїда.

    V-терм бере лише координату vol_pct, тож якщо кластер виділено іншою ознакою (напр. сплеском обсягу),
    його центр на осі V — це середній перцентиль волатильності ЦИХ барів, а не «проміжний режим» за побудовою.
    """
    z = standardized_centroids(series, centroids)
    rows, notes = [], []
    for name, zc in zip(("LO", "MID", "HI"), z, strict=True):
        j = int(np.argmax(np.abs(zc)))
        rows.append([name, *[f"{x:+.3f}" for x in zc], FEATURE_LABELS[j]])
        if j != 0:
            notes.append(f"{name} (домінує «{FEATURE_LABELS[j]}», {zc[j]:+.2f} σ; по vol_pct лише "
                         f"{zc[0]:+.2f} σ)")
    out = ["Центроїди в стандартизованих одиницях (так їх розділяє KMeans) і ознака, що домінує:", ""]
    out += _md_table(["терм V", "vol_pct, σ", "нахил EMA, σ", "z обсягу, σ", "домінантна ознака"], rows)
    if notes:
        out += ["", "**Інтерпретація.** Не кожен кластер відрізняється саме волатильністю: "
                + "; ".join(notes) + ". Терм V бере з центроїда лише координату vol_pct (брифінг §5.5), "
                "тож центр такого терму на осі V — середній перцентиль волатильності барів цього кластера, "
                "а не окремий «проміжний режим» волатильності; на осі V терми все одно впорядковані й "
                "покривають [0, 1] без мертвих зон (див. нижче). Обмеження зафіксовано в "
                "docs/deviations.d/data.md (DATA-09)."]
    return out


def calibration_report_md(run: CalibrationRun, *, window_doc: Mapping[str, Any] | None, command: str,
                          membership_path: str) -> str:
    r, m, s = run.result, run.manifest, run.series
    ds = m["dataset"]
    _, labels = cluster_labels(s, r["centroids_k3"])
    sizes = np.bincount(labels, minlength=3)
    t = s.T[np.isfinite(s.T)]
    spec_T = [-0.70, -0.35, 0.0, 0.35, 0.70]
    win_txt = ""
    if window_doc:
        wd = window_doc["window"]
        win_txt = f"`[{wd['start_utc']}, {wd['end_utc_exclusive']})`"
    spec_V = [0.17, 0.51, 0.88]
    lines = [
        "# Калібрування функцій належності з даних (T — перцентилі, V — KMeans)",
        "",
        f"Згенеровано `{command}`. Ідентифікатор калібрування (`source_run_id` у `{membership_path}`): "
        f"**`{run.run_id}`** = `cal-` + BLAKE2b-256 канонічного маніфесту `data/calibration_manifest.json` "
        "(дані + параметри методу + seed): той самий вхід дає той самий id. Автор: Андрій Жук, 2026.",
        "",
        "## Дані",
        "",
        f"* Символ {ds.get('symbol')} ({ds.get('symbol_canon')}), tf {ds.get('tf')}, IS-вікно "
        f"`[{ds.get('from_utc')}, {ds.get('to_utc')})` — дні {ds.get('days')} зафіксованого вікна датасету "
        f"{win_txt}.",
        "* Лише ПЕРШЕ IS-вікно walk-forward (15 днів): усі OOS-вікна (IS 15 / OOS 5 / крок 5 днів × 6 "
        "фолдів, "
        "`config/profiles/backtest.yaml`, D-03) починаються після нього, тож калібрування не бачить жодного "
        "OOS-бару.",
        f"* Барів: {m['n_bars']}; прогрів {s.warmup_bars} барів (max(warmup детекторів, max_lookback "
        "конвеєра)) → "
        f"T і вектори ознак на {s.n_valid} барах; у KMeans — {r['n_samples']} рядків без NaN.",
        "* Хеш даних (`backtest.manifest.dataset_hash`, колонки "
        f"{', '.join(ds.get('dataset_hash_columns', []))}): "
        f"`{ds.get('dataset_hash')}`; seed {m['seed']}; хеш config/detectors.yaml "
        f"`{m['detectors_config_hash'][:16]}…`.",
        "",
        "## V: KMeans на (перцентиль σ_P, нормований нахил EMA, z-score обсягу)",
        "",
    ]
    lines += _md_table(["k", "силует", "інерція (станд. одиниці)"],
                       [[k, _fmt(v, 4) if math.isfinite(v) else "n/a", _fmt(r["inertia_by_k"][k], 1)]
                        for k, v in sorted(r["silhouette_by_k"].items())])
    kb = r["k_best"]
    lines += ["", f"Силует обирає **k = {kb}**" + (" — збігається з k = 3 брифінгу." if kb == 3 else
              ", а не 3: три терми V (LO/MID/HI) узято з k = 3 (силует k = 3: "
              f"{_fmt(r['V']['silhouette'], 4)}), "
              "як вимагає §5.5; розбіжність зафіксовано в docs/deviations.d/data.md."), ""]
    lines += ["Центроїди k = 3 (відсортовані за перцентилем волатильності):", ""]
    lines += _md_table(["терм V", "vol_pct (центр m)", "нахил EMA", "z обсягу", f"σ ({r['V']['sigma_rule']})",
                        "частка барів (найближчий центроїд)"],
                       [[name, _fmt(c[0], 4), _fmt(c[1], 4), _fmt(c[2], 4), _fmt(sig, 4),
                         f"{sizes[i] / max(1, sizes.sum()):.3f}"]
                        for i, (name, c, sig) in enumerate(zip(("LO", "MID", "HI"), r["centroids_k3"],
                                                               r["V"]["sigmas"], strict=True))])
    V = r["V"]
    vv = s.V[np.isfinite(s.V)]
    mu_near = np.exp(-((vv[None, :] - np.asarray(V["centres"])[:, None]) ** 2)
                     / (2.0 * np.asarray(V["sigmas_nearest"])[:, None] ** 2)).max(axis=0)
    lines += ["", f"Для порівняння — приклад зі спеки m ≈ ({', '.join(f'{x:.2f}' for x in spec_V)}). "
              f"Записані в membership.yaml числа округлено до 6 знаків.", ""]
    lines += cluster_interpretation_md(s, r["centroids_k3"])
    lines += ["",
              "**Ширини σ і покриття (мертві зони).** Брифінг вимагає МФ без мертвих зон (тест "
              "`test_mf_coverage_no_dead_zones`: max μ ≥ 0.5 на реальному конфігу). Перевірено обидва "
              "правила σ "
              "на сітці 2001 точки V ∈ [0, 1]:", ""]
    lines += _md_table(["правило σ", "σ (LO, MID, HI)", "min max μ", "де",
                        "частка IS-барів з max μ(V) < 0.5"], [
        ["буквально §5.5: 0.5·d до найближчого центру",
         ", ".join(f"{x:.4f}" for x in V["sigmas_nearest"]), _fmt(V["coverage_min_nearest"], 4),
         f"V = {V['coverage_min_nearest_at']:.4f}", f"{float((mu_near < 0.5).mean()):.4f}"],
        ["**cover** (записано): 0.5·max(d⁻, d⁺), дзеркальні привиди на межах",
         ", ".join(f"{x:.4f}" for x in V["sigmas"]), _fmt(V["coverage_min"], 4),
         f"V = {V['coverage_min_at']:.4f}",
         "0 (теорема: max μ ≥ e^−½)"]])
    lines += ["", "Крайні центри лежать далеко від меж [0, 1] (V — перцентильний ранг, розподілений майже "
              "рівномірно), тож «до найближчого» дає вузькі крайні гаусіани і провал покриття біля V = 1; "
              "правило "
              "cover зберігає центри KMeans і множник 0.5·d, але бере більший із суміжних проміжків і "
              "відбиває "
              "крайній центр від межі (DATA-05).", ""]
    lines += ["## T: перцентилі емпіричного консенсусу тренду", ""]
    lines += _md_table(["перцентиль", *[str(p) for p in r["T"]["percentiles"]]],
                       [["T (виміряно, як є)", *[_fmt(x, 4) for x in r["T"]["raw_breakpoints"]]],
                        ["T ∪ −T (симетризовано) — **записано**" if r["T"].get("symmetric") else "записано",
                         *[_fmt(x, 4) for x in r["T"]["breakpoints"]]],
                        ["спека §5.5 (приклад)", *[f"{x:.2f}" for x in spec_T]]])
    if r["T"].get("symmetric"):
        asym = max(abs(a - b) for a, b in zip(r["T"]["raw_breakpoints"], r["T"]["breakpoints"], strict=True))
        lines += ["", "Точки зламу — ті самі перцентилі {8, 25, 50, 75, 92}, але симетризованого розподілу "
                  "(вибірка T ∪ −T, тобто p75 = медіана |T|, p92 = 84-й перцентиль |T|): база правил і "
                  "терми R "
                  "непарно-симетричні, і рушій зберігає u(−T, −R, V) = −u(T, R, V) лише за симетричних "
                  "термів T. "
                  f"Несиметричні сирі перцентилі (зсув до {asym:.4f}) відображають дрейф ціни в конкретному "
                  "15-денному IS-вікні, а не структуру сигналу; записати їх означало б зробити систему "
                  "«довшою» чи «коротшою» за побудовою МФ (DATA-06)."]
    lines += ["", f"Розподіл T на {t.size} барах: min {_fmt(t.min(), 4)}, max {_fmt(t.max(), 4)}, середнє "
              f"{_fmt(t.mean(), 4)}, медіана {_fmt(float(np.median(t)), 4)}; частка T = 0 рівно "
              f"{float((t == 0.0).mean()):.4f}.", ""]
    if r["warnings"]:
        lines += ["Попередження калібрування:", "", *[f"* {w}" for w in r["warnings"]], ""]
    lines += ["Рисунок: `docs/figures/regimes_clusters.png` (кластери, силует за k, розподіл T з точками "
              "зламу, "
              "V-терми над розподілом перцентиля σ_P).", ""]
    return "\n".join(lines)


def plot_calibration(run: CalibrationRun, out: Path) -> Path:
    import matplotlib  # noqa: PLC0415

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt  # noqa: PLC0415

    r, s = run.result, run.series
    X, labels = cluster_labels(s, r["centroids_k3"])
    cents = np.asarray(r["centroids_k3"])
    plt.rcParams.update({"font.family": "DejaVu Sans", "figure.facecolor": SURFACE})
    fig, axes = plt.subplots(2, 2, figsize=(12.5, 8.6), layout="constrained")
    names = ("LO", "MID", "HI")
    # (a) кластери у площині (vol_pct, z обсягу): LO/HI розділяє волатильність, MID — сплески обсягу.
    # Підвибірка для читабельності — детермінована (кожен k-й рядок), частки — за всіма рядками.
    from matplotlib.lines import Line2D  # noqa: PLC0415

    ax = axes[0, 0]
    step = max(1, X.shape[0] // 6000)
    handles = []
    for i in range(3):
        sel = labels[::step] == i
        ax.scatter(X[::step, 0][sel], X[::step, 2][sel], s=6, color=SERIES[i], alpha=0.35, linewidths=0)
        handles.append(Line2D([], [], marker="o", linestyle="", markersize=6, color=SERIES[i],
                              label=f"{names[i]} ({(labels == i).mean():.1%} барів)"))
    ax.scatter(cents[:, 0], cents[:, 2], s=70, marker="X", color=INK, edgecolors=SURFACE, linewidths=1.2,
               zorder=3)
    handles.append(Line2D([], [], marker="X", linestyle="", markersize=8, color=INK, label="центроїди k = 3"))
    ax.set_xlabel("перцентиль σ_P (vol_pct)", color=INK_2)
    ax.set_ylabel("z-score обсягу (60 барів)", color=INK_2)
    ax.set_title("KMeans(k=3): кластери режимів (проєкція vol_pct × z обсягу)", loc="left", fontsize=10,
                 color=INK)
    lo_q, hi_q = np.percentile(X[:, 2], [0.5, 99.5])
    ax.set_ylim(lo_q, hi_q)
    ax.legend(handles=handles, loc="upper left", frameon=True, facecolor=SURFACE, edgecolor=GRID, fontsize=8,
              labelcolor=INK_2)
    # (b) силует за k
    ax = axes[0, 1]
    ks = sorted(r["silhouette_by_k"])
    vals = [r["silhouette_by_k"][k] for k in ks]
    ax.bar([str(k) for k in ks], vals, width=0.55,
           color=[SERIES[0] if k == 3 else AXIS for k in ks], edgecolor=SURFACE, linewidth=2)
    for k, v in zip(ks, vals, strict=True):
        ax.annotate(f"{v:.3f}", (str(k), v), textcoords="offset points", xytext=(0, 3), ha="center",
                    fontsize=8, color=INK_2)
    ax.set_xlabel("k", color=INK_2)
    ax.set_ylabel("силуетний коефіцієнт", color=INK_2)
    ax.set_title(f"Силует за k (найкращий k = {r['k_best']}; для V — k = 3)", loc="left", fontsize=10,
                 color=INK)
    # (c) розподіл T і точки зламу
    ax = axes[1, 0]
    t = s.T[np.isfinite(s.T)]
    ax.hist(t, bins=120, range=(-1, 1), color=SERIES[0], alpha=0.55, edgecolor=SURFACE, linewidth=0.3)
    for p, x in zip(r["T"]["percentiles"], r["T"]["breakpoints"], strict=True):
        ax.axvline(x, color=INK, linewidth=0.9, linestyle=(0, (4, 3)))
        ax.annotate(f"p{p}", (x, 1.0), xycoords=("data", "axes fraction"), xytext=(2, -12),
                    textcoords="offset points", fontsize=8, color=INK_2)
    ax.set_xlabel("T (консенсус тренду)", color=INK_2)
    ax.set_ylabel("барів", color=INK_2)
    ax.set_title("Емпіричний розподіл T і точки зламу термів", loc="left", fontsize=10, color=INK)
    # (d) V-терми; сірим — розподіл V на тій самій шкалі [0, 1] (відносна частота, найвищий стовпчик = 1)
    ax = axes[1, 1]
    v = s.vol_pct[np.isfinite(s.vol_pct)]
    counts, edges = np.histogram(v, bins=50, range=(0.0, 1.0))
    ax.bar(edges[:-1], counts / counts.max(), width=np.diff(edges), align="edge", color=GRID,
           edgecolor=SURFACE, linewidth=0.5, label="розподіл V (відн. частота)")
    xs = np.linspace(0, 1, 501)
    for i, term in enumerate(names):
        spec = r["V"]["terms"][term]
        mu = np.exp(-((xs - spec["m"]) ** 2) / (2 * spec["sigma"] ** 2))
        ax.plot(xs, mu, color=SERIES[i], linewidth=1.6,
                label=f"{term}: m={spec['m']:.3f}, σ={spec['sigma']:.3f}")
    ax.axhline(0.5, color=MUTED, linewidth=0.8, linestyle=(0, (4, 3)))
    ax.set_ylim(0, 1.32)
    ax.set_yticks([0, 0.25, 0.5, 0.75, 1.0])
    ax.set_ylabel("μ", color=INK_2)
    ax.legend(loc="upper center", frameon=False, fontsize=8, labelcolor=INK_2, ncols=2)
    ax.set_xlabel("перцентиль σ_P (V)", color=INK_2)
    ax.set_title(f"Гаусові терми V (правило σ: {r['V'].get('sigma_rule', 'nearest')}; пунктир — μ = 0.5)",
                 loc="left", fontsize=10, color=INK)
    for a in axes.flat:
        _style_axes(a)
    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, facecolor=SURFACE)
    plt.close(fig)
    return out


def load_fixture_bars(path: Path) -> tuple[list[Bar], dict[str, Any]]:
    """Офлайн-джерело барів: fixtures/golden/source_klines_btcusdt_1m.json (сирі klines Binance)."""
    from fuzzhelm.features.convert import bar_from_candle  # noqa: PLC0415
    from fuzzhelm.ingest.normalize import normalize_rest_klines  # noqa: PLC0415
    from fuzzhelm.ingest.symbols import symbol_ref  # noqa: PLC0415

    doc = orjson.loads(path.read_bytes())
    ref = symbol_ref(Venue.BINANCE_USDM, doc.get("symbol", "BTCUSDT"))
    candles = normalize_rest_klines(doc["klines"], ref, 0, server_time_ms=2**62 // NS_PER_MS)
    bars = [bar_from_candle(c) for c in candles]
    meta = {"source": _rel(path), "sha256": _file_sha256(path), "symbol": ref.symbol_venue,
            "symbol_canon": ref.symbol_canon, "tf": "1m", "from_utc": utc_iso(bars[0].t_ns // NS_PER_MS),
            "to_utc": utc_iso(bars[-1].t_ns // NS_PER_MS + MIN_MS), "days": "fixture"}
    return bars, meta


CAL_OUTPUTS: Final = ("membership", "manifest", "report", "figure")


def resolve_calibration_outputs(args: argparse.Namespace) -> set[str]:
    """Підставити шляхи за замовчуванням і повернути, які з CAL_OUTPUTS цей запуск має право писати.

    Робочі артефакти (config/membership.yaml, data/calibration_manifest.json, docs/figures/calibration_*,
    regimes_clusters.png) — одне узгоджене ціле: source_run_id у YAML = id маніфесту (gate фази 3). Тому за
    замовчуванням їх пише лише справжнє калібрування з БД без --no-write; --from-fixture і --no-write
    пишуть тільки явно задані шляхи, а --no-write ніколи не пише membership.yaml.
    """
    defaults = {"membership": CONFIG_DIR / "membership.yaml", "manifest": CAL_MANIFEST_JSON,
                "report": FIG_DIR / "calibration_report.md", "figure": FIG_DIR / "regimes_clusters.png"}
    explicit = {k for k in CAL_OUTPUTS if getattr(args, k) is not None}
    for k in CAL_OUTPUTS:
        if getattr(args, k) is None:
            setattr(args, k, defaults[k])
    production = args.from_fixture is None and not args.no_write
    writes = {k for k in CAL_OUTPUTS if k in explicit or production}
    if args.no_write:
        writes.discard("membership")
    return writes


async def cmd_calibrate(args: argparse.Namespace) -> int:
    settings = get_settings()
    seed = args.seed if args.seed is not None else settings.seed
    cfg = load_yaml("detectors")
    writes = resolve_calibration_outputs(args)
    window_doc: dict[str, Any] | None = None
    if args.from_fixture is not None:
        bars, dataset = load_fixture_bars(args.from_fixture)
        from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415

        cols = {"t_ns": np.array([b.t_ns for b in bars], dtype=np.int64),
                **{k: np.array([getattr(b, k) for b in bars]) for k in ("o", "h", "l", "c", "v")}}
        dataset |= {"dataset_hash": dataset_hash(cols), "dataset_hash_columns": sorted(cols)}
    else:
        window, window_doc = load_window(args.window_json)
        lo, hi = window.sub_window(1, args.is_days)
        if args.trim_end_bars:
            # перевірка чутливості: без останніх N барів IS (напр. embargo walk-forward, EXE-08) — DATA-10
            if args.trim_end_bars >= (hi - lo) // MIN_MS:
                raise CliError(f"--trim-end-bars {args.trim_end_bars} leaves no bars in the IS window")
            hi -= args.trim_end_bars * MIN_MS
        if args.dry_run:
            targets = ", ".join(_rel(getattr(args, k)) for k in CAL_OUTPUTS if k in writes) or "no files"
            print("DRY RUN (no DB): would load", args.symbol, f"[{utc_iso(lo)}, {utc_iso(hi)}) = "
                  f"{(hi - lo) // MIN_MS} bars (days 1–{args.is_days}), seed={seed}, then write {targets}")
            return 0
        bars, dataset = await _load_is_bars(args, settings, window, lo, hi)
    run = run_calibration(bars, seed=seed, detectors_cfg=cfg, dataset=dataset)
    r = run.result
    print(f"calibration {run.run_id}: n_samples={r['n_samples']}, silhouette_by_k="
          f"{ {k: round(v, 4) for k, v in r['silhouette_by_k'].items()} }, k_best={r['k_best']}")
    print(f"  V centres={[round(x, 4) for x in r['V']['centres']]} "
          f"sigmas={[round(x, 4) for x in r['V']['sigmas']]}")
    print(f"  T breakpoints={[round(x, 4) for x in r['T']['breakpoints']]}")
    for w in r["warnings"]:
        print(f"  WARNING: {w}")
    if args.dry_run:
        return 0
    from fuzzhelm.regimes.calibrate_mf import write_membership_yaml  # noqa: PLC0415

    if args.from_fixture:
        command = f"uv run fuzzhelm calibrate --from-fixture {_rel(args.from_fixture)}"
    else:
        command = f"uv run fuzzhelm calibrate --symbol {args.symbol} --is-days {args.is_days} --seed {seed}"
        if args.trim_end_bars:
            command += f" --trim-end-bars {args.trim_end_bars}"
    if not writes:
        print("no files written: --from-fixture / --no-write never touch the committed calibration "
              "artifacts; pass --membership/--manifest/--report/--figure explicitly to save outputs")
        return 0
    if "manifest" in writes:
        manifest_doc = {"id": run.run_id, **run.manifest, "result": _jsonable(r), "command": command,
                        "created_utc": _now_utc_iso()}
        _write_json(args.manifest, manifest_doc)
    if "membership" in writes:
        write_membership_yaml(r, args.membership, source_run_id=run.run_id)
        print(f"wrote {_rel(args.membership)} (source_run_id={run.run_id})")
    if "report" in writes:
        _write_text(args.report, calibration_report_md(run, window_doc=window_doc, command=command,
                                                       membership_path=_rel(args.membership)))
    if "figure" in writes:
        plot_calibration(run, args.figure)
    written = [_rel(getattr(args, k)) for k in ("manifest", "report", "figure") if k in writes]
    if written:
        print(f"wrote {', '.join(written)}")
    return 0


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, np.generic):
        return _jsonable(obj.item())
    return obj


async def _load_is_bars(args: argparse.Namespace, settings: Settings, window: DatasetWindow, lo: int, hi: int
                        ) -> tuple[list[Bar], dict[str, Any]]:
    from fuzzhelm.backtest.manifest import dataset_hash  # noqa: PLC0415
    from fuzzhelm.storage.repositories import CandleRepo, InstrumentRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    engine = _engine(_db_url(args, settings))
    try:
        async with session_scope(_factory(engine)) as s:
            row = await InstrumentRepo(s).get_by_venue_symbol(Venue.BINANCE_USDM, args.symbol)
            if row is None:
                raise CliError(f"instrument {args.symbol} is not in the DB: run `fuzzhelm backfill` first")
            arrays = await CandleRepo(s).load_arrays(row.id, window.tf, lo * NS_PER_MS, hi * NS_PER_MS)
    finally:
        await engine.dispose()
    expected = (hi - lo) // MIN_MS
    if len(arrays) != expected:
        raise CliError(f"{args.symbol}: {len(arrays)} closed bars in [{utc_iso(lo)}, {utc_iso(hi)}), "
                       f"expected {expected} — backfill the window first")
    cols = arrays.columns()
    dataset = {"symbol": args.symbol, "symbol_canon": row.symbol_canon, "tf": window.tf, "from_ms": lo,
               "to_ms": hi, "from_utc": utc_iso(lo), "to_utc": utc_iso(hi),
               "days": f"1–{args.is_days}"
               + (f" без останніх {args.trim_end_bars} барів" if args.trim_end_bars else ""),
               "n_bars": len(arrays), "dataset_hash": dataset_hash(cols),
               "dataset_hash_columns": sorted(cols)}
    return arrays.bars(), dataset


# ================================================================== replay-gap


def replay_run_id(session_path: Path) -> UUID:
    """Детермінований run_id журналу реплею: uuid5 від sha256 файлу сесії (повтор не дублює журнал)."""
    return uuid5(NAMESPACE_URL, f"fuzzhelm:replay-gap:{_file_sha256(session_path)}")


async def cmd_replay_gap(args: argparse.Namespace) -> int:
    from fuzzhelm.ingest.replay import read_header  # noqa: PLC0415

    settings = get_settings()
    header = read_header(args.session)
    symbol = str(header["symbol"])
    run_id = replay_run_id(args.session)
    if args.dry_run:
        print(f"DRY RUN (no network, no DB): replay {_rel(args.session)} ({symbol}) with REAL REST "
              "backfill from "
              f"{settings.binance_rest_base}; journal run_id={run_id}; write {_rel(args.report)}")
        return 0

    from fuzzhelm.core.journal import EventJournal  # noqa: PLC0415
    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
    from fuzzhelm.ingest.gap_detector import GapRecord  # noqa: PLC0415
    from fuzzhelm.ingest.pipeline import (  # noqa: PLC0415
        PipelineSinks,
        RestBackfiller,
        SessionCapture,
        journal_sink,
        recovery_stats,
        replay_session,
        session_reference,
    )
    from fuzzhelm.ingest.ratelimit import binance_request_bucket  # noqa: PLC0415
    from fuzzhelm.ingest.replay import FrameClock  # noqa: PLC0415
    from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415
    from fuzzhelm.storage.repositories import (  # noqa: PLC0415
        BufferedSink,
        CandleRepo,
        GapRepo,
        InstrumentRepo,
        JournalRepo,
    )
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    engine = _engine(_db_url(args, settings))
    factory = _factory(engine)
    try:
        async with session_scope(factory) as s:
            if await JournalRepo(s).count(run_id):
                print(f"journal {run_id} already exists: this session was already replayed into the DB "
                      "(idempotent no-op). Use `fuzzhelm db-stats` / `verify-journal` to inspect it.")
                return 0
            row = await InstrumentRepo(s).get_by_venue_symbol(Venue.BINANCE_USDM, symbol)
        if row is None:
            raise CliError(f"instrument {symbol} is not in the DB: run `fuzzhelm backfill` first")
        inst, iid = row.to_dto(), row.id
        cap = SessionCapture()
        buf: BufferedSink[Any] = BufferedSink()
        journal = EventJournal(run_id, sink=buf, keep=False)
        to_journal = journal_sink(journal)
        db_gap: dict[int, int] = {}
        transitions: list[tuple[int, str]] = []

        def on_event(ev: Any) -> None:
            cap.events += 1
            to_journal(ev)

        async def on_gap(g: GapRecord) -> None:
            cap.gaps.append(g)
            transitions.append((g.gap_id, g.status.value))
            async with session_scope(factory) as s:
                repo = GapRepo(s)
                if g.gap_id not in db_gap:
                    db_gap[g.gap_id] = await repo.open(iid, g.stream, g.ts_lo_ns, g.ts_hi_ns,
                                                       expected_count=g.expected_count, detector=g.detector,
                                                       detected_at_ns=g.detected_at_ns)
                    if g.status is GapStatus.OPEN:
                        return
                await repo.update_status(db_gap[g.gap_id], g.status, filled_rows=g.filled_rows,
                                         count_attempt=g.status is GapStatus.FILLING, at_ns=g.closed_at_ns)

        sinks = PipelineSinks(on_event=on_event, on_candle=cap.candles.append, on_trade=cap.trades.append,
                              on_gap=on_gap, on_invalid=cap.invalid.append,
                              on_disconnect=cap.disconnects.append)
        log = HttpLog()
        clock = SystemClock()
        t0 = time.perf_counter()
        async with _http(args.timeout, log) as http:
            client = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                                       RetryPolicy(rng_seed=settings.seed), clock=clock)
            report = await replay_session(args.session, inst, backfill=RestBackfiller(client, inst),
                                          sinks=sinks, clock=FrameClock())
        elapsed = time.perf_counter() - t0
        entries = buf.drain()
        async with session_scope(factory) as s:
            up = await CandleRepo(s).upsert(cap.candles, iid)
            await JournalRepo(s).append_many(entries)
        async with session_scope(factory) as s:
            bad = await JournalRepo(s).verify(run_id)
            rows = [await GapRepo(s).get(db_id) for db_id in db_gap.values()]
            gap_stats = await GapRepo(s).stats()
        rec = recovery_stats(session_reference(args.reference), cap, report)
    finally:
        await engine.dispose()

    gap_rows = [{"id": g.id, "stream": g.stream, "detector": g.detector, "status": g.status,
                 "ts_lo_utc": utc_iso(g.ts_lo_ns // NS_PER_MS) if g.ts_lo_ns is not None else None,
                 "ts_hi_utc": utc_iso(g.ts_hi_ns // NS_PER_MS) if g.ts_hi_ns is not None else None,
                 "expected_count": g.expected_count, "filled_rows": g.filled_rows, "attempts": g.attempts,
                 "detected_utc": utc_iso(g.detected_at_ns // NS_PER_MS) if g.detected_at_ns else None,
                 "closed_utc": utc_iso(g.closed_at_ns // NS_PER_MS) if g.closed_at_ns else None}
                for g in rows if g is not None]
    result = {
        "session": _rel(args.session), "session_sha256": _file_sha256(args.session),
        "reference": _rel(args.reference), "run_id": str(run_id), "journal_entries": len(entries),
        "journal_head": journal.head.hex(), "journal_verify_bad_seq": bad, "elapsed_s": round(elapsed, 3),
        "candles_released": report.candles_released, "trades_emitted": report.trades_emitted,
        "candle_upsert": {"inserted": up.inserted, "updated": up.updated, "skipped": up.skipped},
        "backfill_requests": report.backfill_requests, "backfill_errors": list(report.backfill_errors),
        "http": {"by_path": log.by_path(), "statuses": {str(k): v for k, v in log.statuses().items()},
                 "used_weight_headers": [(p, u) for p, _, u in log.calls]},
        "gap_rows": gap_rows, "transitions": transitions, "gap_stats_db": gap_stats,
        "recovery": asdict(rec) | {"zero_loss": rec.zero_loss},
        "invalid": len(cap.invalid), "disconnects": len(cap.disconnects), "run_utc": _now_utc_iso(),
    }
    _write_text(args.report, replay_report_md(result))
    filled = sum(1 for g in gap_rows if g["status"] == GapStatus.FILLED.value)
    print(f"replayed {_rel(args.session)}: {len(gap_rows)} gap rows ({filled} FILLED), "
          f"{report.backfill_requests} REST requests, zero_loss={rec.zero_loss}, journal {run_id} "
          f"({len(entries)} entries, verify={'OK' if bad is None else f'BROKEN at {bad}'})")
    print(f"wrote {_rel(args.report)}")
    return 0 if filled and rec.zero_loss and bad is None else 1


def replay_report_md(res: Mapping[str, Any]) -> str:
    rec = res["recovery"]
    bad = res["journal_verify_bad_seq"]
    verify_txt = "ціла" if bad is None else f"РОЗІРВАНА на seq {bad}"
    lines = [
        "# Реальні прогалини: реплей gap.jsonl.gz → ingest_gap → REST-добір зі справжньої Binance",
        "",
        f"Згенеровано `uv run fuzzhelm replay-gap` ({res['run_utc']}). Автор: Андрій Жук, 2026.",
        "",
        f"Сесія `{res['session']}` (sha256 `{res['session_sha256'][:16]}…`) — похідна від записаної "
        "2026-09-18 "
        "WS-сесії BTCUSDT, з якої вирізано 90 с кадрів kline+aggTrade. Її відтворено через `IngestPipeline` "
        "(віртуальний час кадрів, сторож тиші 10 с) з БД-стоками: кожна зміна стану прогалини — окрема "
        "транзакція в `ingest_gap`, свічки — `CandleRepo.upsert` (src=REPLAY), кожна прийнята подія — у "
        f"`event_journal` (run_id `{res['run_id']}`). Добір — **справжній** `BinanceRestClient` до "
        "`fapi.binance.com` (`RestBackfiller`: `/fapi/v1/klines` для свічок, `/fapi/v1/aggTrades?fromId` "
        "для угод), "
        "а не офлайн-фікстура.",
        "",
        "## Рядки ingest_gap (з БД після прогону)",
        "",
    ]
    lines += _md_table(["id", "stream", "detector", "перший відсутній", "останній відсутній", "очікувано",
                        "добрано", "спроб", "виявлено (відтвор. час)", "закрито (відтвор. час)", "статус"],
                       [[g["id"], g["stream"], g["detector"], g["ts_lo_utc"], g["ts_hi_utc"],
                         g["expected_count"], g["filled_rows"], g["attempts"], g["detected_utc"],
                         g["closed_utc"], f"**{g['status']}**"] for g in res["gap_rows"]])
    h = res["http"]
    lines += [
        "",
        "Мітки `detected_at`/`closed_at` у `ingest_gap` — віртуальний час кадрів реплею (`FrameClock`), а "
        "не настінний: у віртуальному часі добір миттєвий, тож вони збігаються. Справжні REST-запити "
        f"пішли під час прогону ({res['run_utc']}).",
        "",
        f"Переходи станів (gap_id → статус): {', '.join(f'{i}→{s}' for i, s in res['transitions'])}.",
        "",
        "## Результат",
        "",
        f"* REST-запитів добору: {res['backfill_requests']} "
        f"({', '.join(f'`{p}` × {n}' for p, n in h['by_path'].items())}; "
        f"статуси {h['statuses']}); помилок добору: {len(res['backfill_errors'])}.",
        f"* Заголовок `X-MBX-USED-WEIGHT-1M` після кожної відповіді: {h['used_weight_headers']}.",
        f"* Закритих свічок випущено: {res['candles_released']}, угод: {res['trades_emitted']}; upsert "
        "свічок: "
        f"{res['candle_upsert']}.",
        f"* Відновлення проти еталона `{res['reference']}` (сирі кадри чистої сесії): втрачено свічок "
        f"{rec['lost_candles']} з {rec['expected_candles']}, угод {rec['lost_trades']} з "
        f"{rec['expected_trades']}; "
        f"дублів {rec['duplicated_candles']} / {rec['duplicated_trades']}; розбіжних OHLCV "
        f"{rec['mismatched_candles']}; "
        f"zero_loss = **{rec['zero_loss']}**.",
        f"* Журнал: {res['journal_entries']} записів, голова `{res['journal_head'][:16]}…`, перевірка "
        "ланцюга з БД "
        "(`JournalRepo.verify`): "
        f"{verify_txt}.",
        f"* Час прогону (реплей + справжні REST-запити + запис у БД): {res['elapsed_s']:.1f} с.",
        f"* `ingest_gap` за статусами після прогону: {res['gap_stats_db']}.",
        "",
        "Повторний запуск — ідемпотентний no-op: run_id = uuid5(sha256 файлу сесії), і якщо журнал із таким "
        "run_id уже є, команда нічого не пише.",
        "",
    ]
    return "\n".join(lines)


# ================================================================== verify-journal, db-stats


async def cmd_verify_journal(args: argparse.Namespace) -> int:
    from sqlalchemy import text  # noqa: PLC0415

    from fuzzhelm.storage.repositories import JournalRepo, RunRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    settings = get_settings()
    engine = _engine(_db_url(args, settings))
    failures = 0
    try:
        async with session_scope(_factory(engine)) as s:
            if args.run_id is not None:
                run_ids = [args.run_id]
            else:
                res = await s.execute(text("SELECT DISTINCT run_id FROM event_journal ORDER BY run_id"))
                run_ids = [r[0] for r in res.all()]
            if not run_ids:
                print("event_journal is empty: nothing to verify")
                return 0
            repo = JournalRepo(s)
            for rid in run_ids:
                n = await repo.count(rid)
                bad = await repo.verify(rid)
                head = await repo.head(rid)
                run = await RunRepo(s).get(rid)
                anchor = "no run row"
                if run is not None and run.journal_head_hash is not None:
                    ok = bytes(run.journal_head_hash) == head.head
                    anchor = "anchor OK" if ok else "ANCHOR MISMATCH"
                    failures += not ok
                if n == 0:
                    status = "EMPTY"
                    failures += 1
                elif bad is None:
                    status = "OK"
                else:
                    status = f"BROKEN at seq {bad}"
                    failures += 1
                print(f"{rid}: {n} entries, head {head.head.hex()[:16]}…, chain {status}, {anchor}")
    finally:
        await engine.dispose()
    return 1 if failures else 0


async def cmd_db_stats(args: argparse.Namespace) -> int:
    from sqlalchemy import text  # noqa: PLC0415

    from fuzzhelm.storage.models import ALL_TABLES  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    settings = get_settings()
    engine = _engine(_db_url(args, settings))
    out: dict[str, Any] = {}
    try:
        async with session_scope(_factory(engine)) as s:
            out["tables"] = {t: int((await s.execute(text(f'SELECT count(*) FROM "{t}"'))).scalar_one())
                             for t in ALL_TABLES}
            res = await s.execute(text(
                "SELECT i.symbol_canon, c.tf, c.src, count(*), min(c.open_time), max(c.open_time), "
                "sum(CASE WHEN c.is_closed THEN 1 ELSE 0 END) FROM candle c JOIN instrument i ON i.id = "
                "c.instrument_id "
                "GROUP BY i.symbol_canon, c.tf, c.src ORDER BY 1, 2, 3"))
            out["candles"] = [{"instrument": r[0], "tf": r[1], "src": int(r[2]), "rows": int(r[3]),
                               "first_open": r[4].isoformat(), "last_open": r[5].isoformat(),
                               "closed": int(r[6])}
                              for r in res.all()]
            res = await s.execute(text("SELECT stream, status, count(*) FROM ingest_gap "
                                       "GROUP BY 1, 2 ORDER BY 1, 2"))
            out["ingest_gap"] = [{"stream": r[0], "status": r[1], "rows": int(r[2])} for r in res.all()]
            res = await s.execute(text("SELECT run_id, count(*), max(seq) FROM event_journal "
                                       "GROUP BY 1 ORDER BY 1"))
            out["journals"] = [{"run_id": str(r[0]), "entries": int(r[1]), "max_seq": int(r[2])}
                               for r in res.all()]
    finally:
        await engine.dispose()
    if args.json:
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0
    print("tables: " + ", ".join(f"{k}={v}" for k, v in out["tables"].items()))
    for c in out["candles"]:
        print(f"candle {c['instrument']} {c['tf']} src={c['src']}: {c['rows']} rows ({c['closed']} closed), "
              f"{c['first_open']} … {c['last_open']}")
    for g in out["ingest_gap"]:
        print(f"ingest_gap {g['stream']} {g['status']}: {g['rows']}")
    for j in out["journals"]:
        print(f"event_journal {j['run_id']}: {j['entries']} entries (max seq {j['max_seq']})")
    return 0


# ================================================================== argparse


def _symbols(text: str) -> tuple[str, ...]:
    syms = tuple(s.strip().upper() for s in text.split(",") if s.strip())
    if not syms or any(not s.isalnum() for s in syms):
        raise argparse.ArgumentTypeError(f"expected comma-separated symbols like BTCUSDT,ETHUSDT, "
                                         f"got {text!r}")
    return syms


def _positive_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}") from None
    if v < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {text!r}")
    return v


def _non_negative_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}") from None
    if v < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative integer, got {text!r}")
    return v


def _limit(text: str) -> int:
    v = _positive_int(text)
    if not 2 <= v <= MAX_KLINES_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be in [2, {MAX_KLINES_LIMIT}]")
    return v


def _utc_date(text: str) -> date:
    try:
        return date.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected a UTC date YYYY-MM-DD, got {text!r}") from None


def _decimal(text: str) -> Decimal:
    try:
        v = dec(text)
    except (ValueError, ArithmeticError):
        raise argparse.ArgumentTypeError(f"expected a decimal number, got {text!r}") from None
    if not v.is_finite() or v < 0:
        raise argparse.ArgumentTypeError(f"expected a non-negative finite number, got {text!r}")
    return v


def _default_is_days() -> int:
    try:
        return int((load_yaml("profiles/backtest").get("walkforward") or {}).get("is_days", 15))
    except (OSError, ValueError):
        return 15


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fuzzhelm", description="FuzzHelm data & calibration tools "
                                "(read-only market data; paper/testnet only — no mainnet by construction).")
    p.add_argument("--database-url", default=None,
                   help="SQLAlchemy URL (default: FUZZHELM_DATABASE_URL / .env)")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    b = sub.add_parser("backfill", help="REST backfill of 1m klines into PostgreSQL + dataset window")
    b.add_argument("--days", type=_positive_int, default=45)
    b.add_argument("--symbols", type=_symbols, default=DEFAULT_SYMBOLS,
                   help="comma-separated, e.g. BTCUSDT,ETHUSDT")
    b.add_argument("--end-date", type=_utc_date, default=None,
                   help="exclusive end (UTC midnight) YYYY-MM-DD; default: start of the current UTC day "
                        "by exchange time")
    b.add_argument("--limit", type=_limit, default=MAX_KLINES_LIMIT,
                   help="klines page size (weight by limit)")
    b.add_argument("--report", type=Path, default=FIG_DIR / "backfill_report.md")
    b.add_argument("--window-json", type=Path, default=WINDOW_JSON)
    b.add_argument("--timeout", type=float, default=15.0)
    b.add_argument("--dry-run", action="store_true", help="print the plan; no network, no DB")

    c = sub.add_parser("crosscheck",
                       help="Binance perp vs Kraken spot divergence over Kraken's last 720 bars")
    c.add_argument("--threshold-bps", type=_decimal, default=Decimal(50))
    c.add_argument("--report", type=Path, default=FIG_DIR / "crosscheck_report.md")
    c.add_argument("--figure", type=Path, default=FIG_DIR / "crosscheck_divergence.png")
    c.add_argument("--save-input", type=Path, default=CROSSCHECK_INPUT)
    c.add_argument("--from-input", type=Path, default=None, help="recompute offline from a saved raw input")
    c.add_argument("--timeout", type=float, default=15.0)
    c.add_argument("--dry-run", action="store_true")

    f = sub.add_parser("fetch-funding",
                       help="funding-rate history over the dataset window → data/funding_*.json")
    f.add_argument("--symbols", type=_symbols, default=None)
    f.add_argument("--window-json", type=Path, default=WINDOW_JSON)
    f.add_argument("--out-dir", type=Path, default=DATA_DIR)
    f.add_argument("--timeout", type=float, default=15.0)
    f.add_argument("--dry-run", action="store_true")

    k = sub.add_parser("calibrate", help="calibrate T/V membership functions on the FIRST in-sample window")
    k.add_argument("--symbol", default="BTCUSDT")
    k.add_argument("--window-json", type=Path, default=WINDOW_JSON)
    k.add_argument("--is-days", type=_positive_int, default=_default_is_days())
    k.add_argument("--trim-end-bars", type=_non_negative_int, default=0,
                   help="sensitivity check: drop the last N bars of the IS window "
                        "(e.g. the walk-forward embargo)")
    k.add_argument("--seed", type=int, default=None, help="default: FUZZHELM_SEED (20260918)")
    # None = робочий артефакт за замовчуванням; його пише лише справжнє калібрування з БД без --no-write
    k.add_argument("--membership", type=Path, default=None, help="default: config/membership.yaml")
    k.add_argument("--manifest", type=Path, default=None, help="default: data/calibration_manifest.json")
    k.add_argument("--report", type=Path, default=None, help="default: docs/figures/calibration_report.md")
    k.add_argument("--figure", type=Path, default=None, help="default: docs/figures/regimes_clusters.png")
    k.add_argument("--from-fixture", type=Path, default=None,
                   help="offline bars from a golden klines JSON; writes ONLY the paths given explicitly")
    k.add_argument("--no-write", action="store_true",
                   help="never rewrite membership.yaml; manifest/report/figure are written only to paths "
                        "given explicitly (the committed artifacts stay untouched)")
    k.add_argument("--dry-run", action="store_true",
                   help="DB mode: print the plan, touch no DB; "
                        "--from-fixture: compute and print, write no files")

    r = sub.add_parser("replay-gap", help="replay the gap scenario into the DB with the real REST backfill")
    r.add_argument("--session", type=Path, default=GAP_SESSION)
    r.add_argument("--reference", type=Path, default=GAP_REFERENCE)
    r.add_argument("--report", type=Path, default=FIG_DIR / "backfill_gap_replay.md")
    r.add_argument("--timeout", type=float, default=15.0)
    r.add_argument("--dry-run", action="store_true")

    v = sub.add_parser("verify-journal", help="recompute the event_journal hash chain of a run (or all runs)")
    v.add_argument("--run-id", type=UUID, default=None)

    s = sub.add_parser("db-stats", help="row counts, candle coverage, gap statuses, journals")
    s.add_argument("--json", action="store_true")
    return p


HANDLERS: Final[dict[str, Callable[[argparse.Namespace], Coroutine[Any, Any, int]]]] = {
    "backfill": cmd_backfill,
    "crosscheck": cmd_crosscheck,
    "fetch-funding": cmd_fetch_funding,
    "calibrate": cmd_calibrate,
    "replay-gap": cmd_replay_gap,
    "verify-journal": cmd_verify_journal,
    "db-stats": cmd_db_stats,
}


def main(argv: Sequence[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(raw)
    args.argv = raw
    try:
        return asyncio.run(HANDLERS[args.command](args))
    except CliError as e:
        print(f"fuzzhelm {args.command}: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
