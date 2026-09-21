"""Командний рядок FuzzHelm: реальні дані (добір, фінансування), журнал, БД, користувачі.

Найменування: cli.py
Призначення: точка входу `fuzzhelm` (pyproject [project.scripts] → fuzzhelm.cli:main). Підкоманди:
  backfill        — N повних UTC-днів 1m-свічок у PostgreSQL; прогалини → ingest_gap → REST-добір;
                    звіт docs/figures/backfill_report.md і зафіксоване вікно data/dataset_window.json;
  fetch-funding   — історія ставок фінансування за вікном датасету → data/funding_<SYMBOL>.json;
  replay-gap      — реплей fixtures/ws/pathological/gap.jsonl.gz через IngestPipeline з БД-стоками і
                    справжнім REST-добором → реальні рядки ingest_gap зі статусом FILLED;
  verify-journal  — перерахунок ланцюга хешів event_journal (і звірка з run.journal_head_hash, якщо є);
  db-stats        — кількості рядків, покриття свічок, статуси прогалин, журнали;
  user            — керування користувачами API: add (пароль лише з --password-stdin або getpass, bcrypt),
                    list, set-role; кожна дія пише audit_log з актором «cli».
Автор: Андрій Жук, 2026.

Мережа — лише allow-listed read-only хости: базові URL валідує Settings, а клієнти перевіряють їх
вдруге (`assert_readonly_url`); ордерних ендпоінтів тут немає. Асинхронний код — через asyncio.run.
`--dry-run` мережевих підкоманд друкує план без мережі й БД (так їх перевіряє tests/unit/test_cli.py).
Пароль користувача ніколи не береться з аргументів командного рядка (видно в `ps` та історії оболонки),
не друкується і не пишеться в журнали чи audit_log — лише bcrypt-хеш у app_user (PLAT-01).
Модуль поза межею детермінізму (настінний годинник для міток запуску і заміру часу), але все, що
потрапляє в дані й хеші, від нього не залежить.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import hashlib
import json
import math
import re
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Protocol, TextIO
from uuid import NAMESPACE_URL, UUID, uuid5

import orjson

from fuzzhelm.config import FIXTURES_DIR, ROOT, Settings, get_settings, load_yaml
from fuzzhelm.core.enums import GapStatus, Role, Venue
from fuzzhelm.core.money import dec_str
from fuzzhelm.ingest.ratelimit import USED_WEIGHT_HEADER, klines_weight, request_weight

if TYPE_CHECKING:
    import httpx
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

    from fuzzhelm.core.dto import Candle, Instrument
    from fuzzhelm.storage.repositories import UserRow

DAY_MS: Final = 86_400_000
MIN_MS: Final = 60_000
NS_PER_MS: Final = 1_000_000
DATA_DIR: Final = ROOT / "data"
FIG_DIR: Final = ROOT / "docs" / "figures"
WINDOW_JSON: Final = DATA_DIR / "dataset_window.json"
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
    """Машиночитне вікно датасету (data/dataset_window.json): джерело fetch-funding і бектесту."""
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


# ================================================================== user

LOGIN_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@-]{0,63}$")
MIN_PASSWORD_CHARS: Final = 8          # NIST SP 800-63B: щонайменше 8 символів для пароля, обраного людиною
BCRYPT_MAX_BYTES: Final = 72           # storage.repositories.user: довший пароль bcrypt мовчки обрізав би
CLI_ACTOR: Final = "cli"
SQLSTATE_UNIQUE_VIOLATION: Final = "23505"


class UserStoreLike(Protocol):
    """Підмножина storage.repositories.UserRepo, потрібна CLI."""

    async def create(self, login: str, password: str, role: Role | str) -> UserRow: ...

    async def get_by_login(self, login: str) -> UserRow | None: ...

    async def set_role(self, user_id: int, role: Role | str) -> None: ...

    async def admins_for_update(self) -> list[UserRow]: ...

    async def list(self) -> list[UserRow]: ...


class AuditStoreLike(Protocol):
    """Підмножина storage.repositories.AuditRepo."""

    async def append(self, action: str, target: str, *, before: Mapping[str, Any] | None = None,
                     after: Mapping[str, Any] | None = None, user_id: int | None = None,
                     ip: str | None = None, ts_ns: int | None = None) -> int: ...


@dataclass(frozen=True, slots=True)
class UserStores:
    """Репозиторії однієї транзакції: зміна користувача і її запис в audit_log комітяться разом."""

    users: UserStoreLike
    audit: AuditStoreLike


UserUow = Callable[[], contextlib.AbstractAsyncContextManager[UserStores]]


def _db_user_stores(args: argparse.Namespace) -> tuple[UserUow, Callable[[], Awaitable[None]]]:
    """Одиниця роботи над PostgreSQL (роль fuzzhelm_app) і функція закриття engine."""
    from fuzzhelm.storage.repositories import AuditRepo, UserRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    engine = _engine(_db_url(args, get_settings()))
    factory = _factory(engine)

    @contextlib.asynccontextmanager
    async def uow() -> AsyncIterator[UserStores]:
        async with session_scope(factory) as s:
            yield UserStores(UserRepo(s), AuditRepo(s))

    return uow, engine.dispose


def cli_actor() -> dict[str, Any]:
    """Хто виконав дію: логін «cli» (не користувач API) і обліковий запис ОС, якщо його можна визначити."""
    try:
        os_user: str | None = getpass.getuser()
    except (OSError, KeyError, ImportError):  # контейнер без USER/LOGNAME і без запису в /etc/passwd
        os_user = None
    return {"login": CLI_ACTOR, "role": None, "os_user": os_user}


def check_login(login: str) -> str:
    if not LOGIN_RE.fullmatch(login):
        raise CliError("login must be 1–64 characters [A-Za-z0-9_.@-] starting with a letter or digit")
    return login


def check_new_password(password: str, login: str) -> None:
    """Правила нового пароля; повідомлення ніколи не містять самого пароля."""
    if not password:
        raise CliError("password must not be empty")
    if len(password) < MIN_PASSWORD_CHARS:
        raise CliError(f"password must be at least {MIN_PASSWORD_CHARS} characters")
    if len(password.encode("utf-8")) > BCRYPT_MAX_BYTES:
        raise CliError(f"password longer than {BCRYPT_MAX_BYTES} bytes is not supported by bcrypt")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in password):
        raise CliError("password must not contain control characters")
    if password.casefold() == login.casefold():
        raise CliError("password must differ from the login")


def read_new_password(*, from_stdin: bool, stdin: TextIO | None = None,
                      prompt: Callable[[str], str] | None = None) -> str:
    """Пароль з першого рядка stdin (`--password-stdin`) або з термінала без відлуння (getpass, двічі)."""
    stream = sys.stdin if stdin is None else stdin
    if from_stdin:
        if stream.isatty():
            # readline() з термінала показує набрані символи (відлуння) — пароль лишився б на екрані
            raise CliError("--password-stdin expects a pipe, not a terminal: "
                           "run without it to be prompted without echo")
        line = stream.readline()
        return line.removesuffix("\n").removesuffix("\r")
    if not stream.isatty():
        # getpass без термінала відкотився б до читання stdin з попередженням — робимо це явним
        raise CliError("no terminal to prompt for the password: pipe it with --password-stdin")
    ask = prompt or getpass.getpass
    first = ask("Password: ")
    if ask("Repeat password: ") != first:
        raise CliError("passwords do not match")
    return first


def _role(text: str) -> Role:
    try:
        return Role(text.strip().lower())
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"role must be one of {', '.join(r.value for r in Role)}, got {text!r}") from None


def _login(text: str) -> str:
    try:
        return check_login(text)
    except CliError as e:
        raise argparse.ArgumentTypeError(str(e)) from None


def _user_public(u: UserRow) -> dict[str, Any]:
    """Рядок користувача для виводу: без pwd_hash."""
    created = None if u.created_at_ns is None else datetime.fromtimestamp(
        u.created_at_ns // 1_000_000_000, tz=UTC).isoformat().replace("+00:00", "Z")
    return {"id": u.id, "login": u.login, "role": u.role, "created_at": created}


async def user_add(uow: UserUow, *, login: str, role: Role, password: str) -> tuple[UserRow, int]:
    """Створити користувача (bcrypt у UserRepo) і запис аудиту `user.create` в одній транзакції."""
    check_login(login)
    check_new_password(password, login)
    async with uow() as st:
        if await st.users.get_by_login(login) is not None:
            raise CliError(f"user {login!r} already exists (use `fuzzhelm user set-role` to change the role)")
        row = await st.users.create(login, password, role)
        audit_id = await st.audit.append(
            "user.create", f"user/{login}", before=None,
            after={"id": row.id, "login": login, "role": role.value, "_actor": cli_actor()})
    return row, audit_id


async def user_set_role(uow: UserUow, *, login: str, role: Role) -> tuple[str | None, int | None]:
    """Змінити роль; повертає (стара роль, id запису аудиту | None, якщо роль уже така)."""
    async with uow() as st:
        row = await st.users.get_by_login(login)
        if row is None:
            raise CliError(f"no user {login!r}")
        old = row.role
        if old == role.value:
            return old, None
        if old == Role.ADMIN.value:
            # рядки адміністраторів блокуються до COMMIT: паралельне пониження іншого admin не проскочить
            admins = await st.users.admins_for_update()
            if all(a.id != row.id for a in admins):
                # поки чекали на блокування, роль цього користувача вже змінила інша транзакція
                raise CliError(f"the role of {login!r} was changed concurrently; run the command again")
            if len(admins) <= 1:
                raise CliError(f"refusing to demote {login!r}: it is the last admin "
                               "(create another admin first)")
        await st.users.set_role(row.id, role)
        audit_id = await st.audit.append(
            "user.set_role", f"user/{login}", before={"id": row.id, "login": login, "role": old},
            after={"id": row.id, "login": login, "role": role.value, "_actor": cli_actor()})
    return old, audit_id


async def user_list(uow: UserUow) -> tuple[list[UserRow], int]:
    """Усі користувачі (без хешів) + запис аудиту `user.list` (хто переглядав облікові записи)."""
    async with uow() as st:
        rows = await st.users.list()
        audit_id = await st.audit.append("user.list", "user/*",
                                         after={"count": len(rows), "_actor": cli_actor()})
    return rows, audit_id


def _unique_violation(e: BaseException) -> bool:
    return getattr(getattr(e, "orig", None), "sqlstate", None) == SQLSTATE_UNIQUE_VIOLATION


async def cmd_user(args: argparse.Namespace) -> int:
    from sqlalchemy.exc import DBAPIError  # noqa: PLC0415 — sqlalchemy лише для БД-команд

    password = ""
    if args.user_command == "add":
        # пароль читаємо ДО підключення до БД: помилка вводу не відкриває з'єднань
        password = read_new_password(from_stdin=args.password_stdin)
        check_new_password(password, args.login)
    uow, close = _db_user_stores(args)
    try:
        if args.user_command == "add":
            row, audit_id = await user_add(uow, login=args.login, role=args.role, password=password)
            print(f"created user id={row.id} login={row.login!r} role={row.role} (audit_log #{audit_id})")
        elif args.user_command == "set-role":
            old, changed_id = await user_set_role(uow, login=args.login, role=args.role)
            if changed_id is None:
                print(f"user {args.login!r} already has role {old}; nothing changed")
            else:
                print(f"user {args.login!r}: role {old} -> {args.role.value} (audit_log #{changed_id})")
        else:
            rows, _ = await user_list(uow)
            out = [_user_public(u) for u in rows]
            if args.json:
                print(json.dumps(out, indent=2, ensure_ascii=False))
            else:
                print(f"{'id':>4}  {'login':<24} {'role':<9} created_at")
                for u in out:
                    created = u["created_at"] or "—"
                    print(f"{u['id']:>4}  {u['login'] or '':<24} {u['role'] or '':<9} {created}")
                print(f"{len(out)} user(s)")
    except DBAPIError as e:
        # текст драйвера містить SQL і параметри (bcrypt-хеш) — назовні лише клас і SQLSTATE
        if _unique_violation(e):
            raise CliError(f"user {args.login!r} already exists") from None
        state = getattr(getattr(e, "orig", None), "sqlstate", None)
        raise CliError(f"database error {type(e).__name__} (SQLSTATE {state})") from None
    finally:
        await close()
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fuzzhelm", description="FuzzHelm data tools "
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

    f = sub.add_parser("fetch-funding",
                       help="funding-rate history over the dataset window → data/funding_*.json")
    f.add_argument("--symbols", type=_symbols, default=None)
    f.add_argument("--window-json", type=Path, default=WINDOW_JSON)
    f.add_argument("--out-dir", type=Path, default=DATA_DIR)
    f.add_argument("--timeout", type=float, default=15.0)
    f.add_argument("--dry-run", action="store_true")

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

    u = sub.add_parser("user", help="API users: add / list / set-role (every action goes to audit_log)")
    usub = u.add_subparsers(dest="user_command", required=True, metavar="ACTION")
    # allow_abbrev=False: інакше argparse мовчки приймав би `--password` / `--pass` як скорочення
    # `--password-stdin` і читав би пароль з термінала з відлунням
    ua = usub.add_parser("add", allow_abbrev=False,
                         help="create a user; the password is read with getpass or from stdin, "
                              "never from the command line")
    ua.add_argument("--login", type=_login, required=True)
    ua.add_argument("--role", type=_role, required=True, help="operator | analyst | auditor | admin")
    ua.add_argument("--password-stdin", action="store_true",
                    help="read the password from the first line of stdin, which must be a pipe "
                         "(for scripts; never from argv)")
    ul = usub.add_parser("list", allow_abbrev=False,
                         help="list users (id, login, role, created_at; never password hashes)")
    ul.add_argument("--json", action="store_true")
    ur = usub.add_parser("set-role", allow_abbrev=False, help="change the role of an existing user")
    ur.add_argument("--login", type=_login, required=True)
    ur.add_argument("--role", type=_role, required=True, help="operator | analyst | auditor | admin")
    return p


HANDLERS: Final[dict[str, Callable[[argparse.Namespace], Coroutine[Any, Any, int]]]] = {
    "backfill": cmd_backfill,
    "fetch-funding": cmd_fetch_funding,
    "replay-gap": cmd_replay_gap,
    "verify-journal": cmd_verify_journal,
    "db-stats": cmd_db_stats,
    "user": cmd_user,
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
