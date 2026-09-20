"""Торговий воркер: потік ринку → IngestPipeline → закриті свічки → TradingLoop → БД + NOTIFY для SSE.

Найменування: workers/trading_worker.py
Призначення: живий контур брифінгу §4.1 для профілів replay (записана WS-сесія або стрес-сценарій, офлайн)
    і paper (живий публічний WS Binance, виконання — PaperBroker). Той самий TradingLoop.step(), що й у
    бектесті: прогрів ознак історією, що передує потоку (з БД; бракує — офлайн-фікстура REST для replay
    або публічний REST для paper), далі кожна закрита свічка — крок рішення/ризику/виконання, запис рядків
    DDL §6 (workers.persist.LivePersister) і NOTIFY `fuzzhelm_live` у тій самій транзакції. Команди API
    (`fuzzhelm_control`: зняття kill-switch адміністратором, зміна лімітів) застосовуються між барами.
Запуск: python -m fuzzhelm.workers.trading_worker --profile replay|paper [--speed X] [--scenario NAME]
Автор: Андрій Жук, 2026.

Шапка стану для LiveView (§8.2, §15): «MODE: PAPER · FEED: REPLAY · NO MAINNET KEYS · SEED · Q» — публікується
подією `health` (її віддає GET /market/health у полі `pipeline`). MODE завжди PAPER: виконавець циклу —
PaperBroker (ENG-19); NO MAINNET KEYS — властивість конфігурації (Settings не має жодного поля ключів
mainnet, хост виконання — лише з ALLOWED_TESTNET_HOSTS; воркер перевіряє це на старті).

Час. Рушій працює на ManualClock барів (межа детермінізму); воркер — поза межею: настінний годинник
(infra.wallclock) потрібен лише для темпу реплею, міток запуску і лагу live-даних; сторож тиші живого WS —
монотонний годинник (WS-07).

QualityGate (§4.1): конвеєр сесії скорить кожну закриту свічку MLP-автокодувальником з
`data/anomaly_mlp_<SYMBOL>.json` (немає файлу — попередження і робота без MLP). Прогрів екстрактора — ті самі
бари, що й прогрів TradingLoop; аномалії входять у N_invalid години Q (отже, у вхід StaleDataGuard), скор
пишеться в candle.anomaly_score разом зі свічкою, лічильники — у health (`anomalies`, `anomaly_scored`).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import math
import signal
import sys
from collections.abc import AsyncIterable, AsyncIterator, Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from fuzzhelm.backtest.dataset import Dataset
from fuzzhelm.backtest.engine import BacktestConfig, StepResult, TradingLoop, run_order_ids
from fuzzhelm.backtest.manifest import equity_hash, read_git_state
from fuzzhelm.config import FIXTURES_DIR, Settings, assert_testnet_url, get_settings, load_yaml
from fuzzhelm.core.digest import canonical_json, to_canonical
from fuzzhelm.core.dto import Candle, Instrument
from fuzzhelm.core.enums import RiskState, Role, RunKind, RunStatus, Src
from fuzzhelm.core.errors import ConfigValidationError, FuzzHelmError, PermissionDeniedError
from fuzzhelm.core.journal import EventJournal, JournalEntry
from fuzzhelm.core.ports import Clock
from fuzzhelm.ingest.pipeline import BackfillHook, IngestPipeline, PipelineReport, PipelineSinks
from fuzzhelm.ingest.recorder import RawFrame, SessionItem
from fuzzhelm.ingest.replay import FrameClock, iter_items, stream_kind
from fuzzhelm.logging_setup import setup_logging
from fuzzhelm.quality.anomaly_mlp import (
    AnomalyScorer,
    AnomalyVerdict,
    anomaly_health,
    db_anomaly_score,
    load_anomaly_scorer,
)
from fuzzhelm.risk.journal import AuditRecord, RiskEventRecord
from fuzzhelm.risk.state import Transition
from fuzzhelm.sizing.convert import float_to_decimal_exact

log = logging.getLogger("fuzzhelm.workers.trading")

MODE = "PAPER"                                   # виконання — лише PaperBroker (ENG-19)
TRADING_STREAMS: tuple[str, ...] = ("kline", "markPrice")
SCENARIO_DIR = FIXTURES_DIR / "ws" / "scenarios"
TF_NS = 60_000_000_000
RELEASE_ACTION = "risk.killswitch.release"      # = api.routers.risk.RELEASE_ACTION (протокол керування)


class WorkerError(FuzzHelmError):
    """Воркер не може стартувати: немає інструмента/історії/файлу сесії (зрозуміле повідомлення, код 2)."""


# ====================================================================== профіль запуску


@dataclass(frozen=True)
class WorkerProfile:
    """config/profiles/<name>.yaml: ключі рушія (BacktestConfig.from_profile) + `params` (перекриття полів
    BacktestConfig) + `feed` (джерело потоку воркера)."""

    name: str
    kind: RunKind
    instrument: str
    tf: str
    seed: int
    raw: Mapping[str, Any]
    params: Mapping[str, Any]
    session: Path | None
    history: Path | None
    speed: float
    streams: tuple[str, ...]
    heartbeat_timeout_s: float | None

    @classmethod
    def load(cls, name: str) -> WorkerProfile:
        raw = load_yaml(f"profiles/{name}")
        kind = RunKind(raw.get("kind", name))
        if kind not in (RunKind.REPLAY, RunKind.PAPER):
            raise ConfigValidationError(f"profile {name!r} is not a live profile (kind={kind.value})",
                                        path=f"profiles/{name}.kind")
        feed = dict(raw.get("feed") or {})
        speed = feed.get("speed", "inf")
        streams = tuple(feed.get("streams") or TRADING_STREAMS)
        hb = feed.get("heartbeat_timeout_s", 10.0 if kind is RunKind.REPLAY else None)
        return cls(
            name=name, kind=kind, instrument=str(raw.get("instrument", "BTC-USDT-PERP")),
            tf=str(raw.get("tf", "1m")), seed=int(raw.get("seed", 0)), raw=raw,
            params=dict(raw.get("params") or {}),
            session=_rel_path(feed.get("session")), history=_rel_path(feed.get("history")),
            speed=math.inf if str(speed) == "inf" else _pos_float(speed, "feed.speed"), streams=streams,
            heartbeat_timeout_s=None if hb is None else _pos_float(hb, "feed.heartbeat_timeout_s"),
        )

    def backtest_config(self, **overrides: Any) -> BacktestConfig:
        return BacktestConfig.from_profile(dict(self.raw), **{**dict(self.params), **overrides})


def _rel_path(p: Any) -> Path | None:
    if p is None:
        return None
    path = Path(str(p))
    return path if path.is_absolute() else FIXTURES_DIR.parent / path


def _rel_str(p: Path) -> str:
    """Шлях відносно кореня репозиторію (для паспорта сесії), або абсолютний, якщо файл поза ним."""
    root, full = FIXTURES_DIR.parent.resolve(), p.resolve()
    return str(full.relative_to(root)) if full.is_relative_to(root) else str(full)


def _pos_float(x: Any, path: str) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError) as e:
        raise ConfigValidationError(f"expected a number, got {x!r}", path=path) from e
    if not v > 0:
        raise ConfigValidationError("must be > 0", path=path)
    return v


def scenario_path(name: str) -> Path:
    path = SCENARIO_DIR / f"{name}.jsonl.gz"
    if not path.is_file():
        raise WorkerError(f"scenario {name!r} not found at {path}; build it with "
                          "`uv run python scripts/make_demo_scenario.py`")
    return path


def first_kline_open_ns(path: Path) -> int:
    """open_time першої свічки сесії (з неї починаються живі кроки; прогрів — строго раніше)."""
    for it in iter_items(path, ("kline",)):
        if isinstance(it, RawFrame):
            return int(it.data["k"]["t"]) * 1_000_000
    raise WorkerError(f"{path}: session has no kline frames")


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def header_line(*, feed: str, seed: int, q: float | None) -> str:
    """Шапка LiveView брифінгу §8.2/§15."""
    q_txt = "—" if q is None else f"{q:.2f}"
    return f"MODE: {MODE} · FEED: {feed} · NO MAINNET KEYS · SEED {seed} · Q={q_txt}"


def assert_no_mainnet(settings: Settings) -> str:
    """Хост виконання — лише testnet (Settings уже валідує; повторна перевірка — захист від підміни)."""
    return assert_testnet_url(settings.venue_base_url)


# ====================================================================== прогрів


def history_window(ds: Dataset, first_open_ns: int, n: int) -> Dataset:
    """Останні n барів СТРОГО до first_open_ns, без дірок і впритул до першої живої свічки."""
    idx = int((ds.t_ns < first_open_ns).sum())
    if idx < n:
        raise WorkerError(f"only {idx} history bars before the feed start, need {n}")
    w = ds.slice(idx - n, idx)
    t = w.t_ns
    if int(t[-1]) != first_open_ns - TF_NS or (n > 1 and bool((t[1:] - t[:-1] != TF_NS).any())):
        raise WorkerError("warm-up history is not contiguous up to the first live candle")
    return w


# ====================================================================== ядро сесії (без БД)


@dataclass(frozen=True, slots=True)
class ControlCommand:
    """Команда каналу керування (API → воркер)."""

    kind: str                                  # killswitch.release | risk.limits.changed
    audit_id: int | None = None
    actor: str | None = None
    role: str | None = None
    run_id: str | None = None
    sha256: str | None = None
    user_id: int | None = None                 # app_user.id автора запиту (для аудиту воркера)


@dataclass(frozen=True, slots=True)
class StepOutput:
    step: StepResult
    candle: Candle
    journal: tuple[JournalEntry, ...]
    q: float | None
    data_lag_ms: float
    anomaly: AnomalyVerdict | None = None        # MLP-скор цієї свічки (None — прогрів / без моделі / REST)


@dataclass(frozen=True)
class SessionSummary:
    run_id: UUID
    kind: RunKind
    status: RunStatus
    error: str | None
    warmup_bars: int
    bars: int
    fills: int
    closed_trades: int
    final_state: RiskState
    halted_at_ns: int | None
    transitions: tuple[tuple[int, str, str, str], ...]      # (ts, from, to, event)
    equity_first: Decimal | None
    equity_last: Decimal | None
    equity_hash: str | None
    journal_head: str
    journal_entries: int
    q_last: float | None
    report: PipelineReport | None = None
    max_abs_u_final: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id), "kind": self.kind.value, "status": self.status.value,
            "error": self.error, "warmup_bars": self.warmup_bars, "bars": self.bars, "fills": self.fills,
            "closed_trades": self.closed_trades, "final_state": self.final_state.value,
            "halted_at_ns": self.halted_at_ns, "transitions": [list(t) for t in self.transitions],
            "equity_first": None if self.equity_first is None else str(self.equity_first),
            "equity_last": None if self.equity_last is None else str(self.equity_last),
            "equity_hash": self.equity_hash, "journal_head": self.journal_head,
            "journal_entries": self.journal_entries, "q_last": self.q_last,
            "max_abs_u_final": self.max_abs_u_final,
            "candles_released": None if self.report is None else self.report.candles_released,
            "frames": None if self.report is None else self.report.health.frames,
            "gaps": None if self.report is None else len(self.report.gaps),
            "anomalies": None if self.report is None else self.report.health.anomalies,
        }


class SessionSink(Protocol):
    """Куди йдуть записи сесії: БД (DbSink) або пам'ять (MemorySink — тести, --no-db)."""

    async def on_start(self, s: TradingSession, journal: Sequence[JournalEntry]) -> None: ...
    async def on_step(self, s: TradingSession, out: StepOutput) -> None: ...
    async def on_out_of_band(self, s: TradingSession, *, risk: Sequence[RiskEventRecord],
                             audits: Sequence[AuditRecord], journal: Sequence[JournalEntry],
                             event: Mapping[str, Any]) -> None: ...
    async def on_finish(self, s: TradingSession, summary: SessionSummary,
                        journal: Sequence[JournalEntry]) -> None: ...


@dataclass
class MemorySink:
    """Записи сесії в пам'яті (e2e-тест, демо без БД)."""

    started: bool = False
    steps: list[StepOutput] = field(default_factory=list)
    journal: list[JournalEntry] = field(default_factory=list)
    out_of_band: list[dict[str, Any]] = field(default_factory=list)
    risk_events: list[RiskEventRecord] = field(default_factory=list)
    audits: list[AuditRecord] = field(default_factory=list)
    summary: SessionSummary | None = None

    async def on_start(self, s: TradingSession, journal: Sequence[JournalEntry]) -> None:
        self.started = True
        self.journal.extend(journal)

    async def on_step(self, s: TradingSession, out: StepOutput) -> None:
        self.steps.append(out)
        self.journal.extend(out.journal)
        self.risk_events.extend(out.step.risk_events)

    async def on_out_of_band(self, s: TradingSession, *, risk: Sequence[RiskEventRecord],
                             audits: Sequence[AuditRecord], journal: Sequence[JournalEntry],
                             event: Mapping[str, Any]) -> None:
        self.out_of_band.append(dict(event))
        self.risk_events.extend(risk)
        self.audits.extend(audits)
        self.journal.extend(journal)

    async def on_finish(self, s: TradingSession, summary: SessionSummary,
                        journal: Sequence[JournalEntry]) -> None:
        self.summary = summary
        self.journal.extend(journal)


CommandSource = Callable[[], Awaitable[list[ControlCommand]]]
GapFiller = Callable[[int, int], Awaitable[list[Candle]]]


class TradingSession:
    """Один прогін live/replay: прогрів → потік → кроки → завершення. Стан — лише тут (один asyncio-таск)."""

    def __init__(self, instrument: Instrument, cfg: BacktestConfig, *, seed: int, run_id: UUID,
                 kind: RunKind, feed: str, warmup_bars: int, sink: SessionSink | None = None,
                 streams: Sequence[str] = TRADING_STREAMS, heartbeat_timeout_s: float | None = 10.0,
                 pipeline_clock: Clock | None = None, wall_clock: Clock | None = None,
                 backfill: BackfillHook | None = None, src: Src = Src.REPLAY,
                 commands: CommandSource | None = None, gap_filler: GapFiller | None = None,
                 limits_path: Path | None = None, anomaly: AnomalyScorer | None = None) -> None:
        if warmup_bars < cfg.resolved_warmup():
            raise WorkerError(f"warm-up needs ≥ {cfg.resolved_warmup()} bars, got {warmup_bars}")
        self.instrument = instrument
        self.cfg = cfg
        self.seed = seed
        self.run_id = run_id
        self.kind = kind
        self.feed = feed
        self.streams = tuple(streams)
        self.warmup_bars = warmup_bars
        self.sink: SessionSink = sink or MemorySink()
        self._jbuf: list[JournalEntry] = []
        self.journal = EventJournal(run_id, sink=self._jbuf.append, keep=False)
        self._audits: list[AuditRecord] = []
        self.loop = TradingLoop(instrument, cfg, seed=seed, run_id=run_id, event_journal=self.journal,
                                audit_sink=self._audits.append, keep_records=False, trade_start=warmup_bars,
                                ids=run_order_ids(seed, run_id))
        self._pclock: Clock = pipeline_clock if pipeline_clock is not None else FrameClock()
        self._wall = wall_clock
        self.pipeline = IngestPipeline(
            instrument, sinks=PipelineSinks(on_event=self._on_event, on_candle=self.on_candle,
                                            on_anomaly=self._on_anomaly),
            backfill=backfill, clock=self._pclock, src=src, heartbeat_timeout_s=heartbeat_timeout_s,
            anomaly=anomaly)
        self._pending_anomaly: AnomalyVerdict | None = None
        self._commands = commands
        self._gap_filler = gap_filler
        self._limits_path = limits_path
        self._limits_stat: tuple[int, int] | None = _stat(limits_path)
        self._limits_sha: str | None = _sha(limits_path)
        self.pending: asyncio.Queue[ControlCommand] = asyncio.Queue()
        self._next_open: int | None = None
        self._last_event_ns: int | None = None
        self.equity: list[Decimal] = []
        self.equity_ts: list[int] = []
        self.transitions: list[tuple[int, str, str, str]] = []
        self.fills = 0
        self.closed_trades = 0
        self.halted_at_ns: int | None = None
        self.q_last: float | None = None
        self.max_abs_u: float | None = None
        self.report: PipelineReport | None = None
        self.started = False

    # ------------------------------------------------------------------ час

    def _now_ns(self) -> int:
        return self._pclock.now_ns()

    def _wall_ns(self) -> int:
        return self._wall.now_ns() if self._wall is not None else self._now_ns()

    def _take_journal(self) -> tuple[JournalEntry, ...]:
        out = tuple(self._jbuf)
        self._jbuf.clear()
        return out

    # ------------------------------------------------------------------ старт і прогрів

    async def begin(self, meta: Mapping[str, Any]) -> None:
        """Перший запис журналу прогону — паспорт сесії (джерело потоку, прогрів, git, профіль)."""
        ts = self._wall_ns()
        self.journal.append("session.start", to_canonical(dict(meta)), ts, ts)
        self.started = True
        await self.sink.on_start(self, self._take_journal())

    def warm_up(self, history: Dataset) -> None:
        """Прогнати history (рівно warmup_bars барів) через цикл: ознаки, σ-оцінка, σ_base; рішень немає."""
        if len(history) != self.warmup_bars:
            raise WorkerError(f"warm-up has {len(history)} bars, session was built for {self.warmup_bars}")
        for bar, dbar, close_ns in history.feed():
            sr = self.loop.step(bar, close_ns, dbar=dbar)
            if sr.decided or sr.fills or sr.orders:            # pragma: no cover — trade_start = warmup_bars
                raise WorkerError("warm-up produced a trading step")
        # ті самі бари прогріву — в екстрактор MLP-скорера: перша ж жива свічка отримує скор
        self.pipeline.prime_anomaly(history.bar(i) for i in range(len(history)))
        self._next_open = int(history.t_ns[-1]) + TF_NS
        self._take_journal()            # прогрів записів журналу не породжує (рішень немає) — страховка

    # ------------------------------------------------------------------ потік

    def _on_event(self, ev: Any) -> None:
        self._last_event_ns = ev.ts_ingest_ns

    def _on_anomaly(self, v: AnomalyVerdict) -> None:
        self._pending_anomaly = v                  # конвеєр кличе on_anomaly ДО on_candle тієї самої свічки

    def q(self) -> float | None:
        """Скор якості Q поточної години (перший вхід ризик-ланцюга: StaleDataGuard, Q < 0.90 → VETO)."""
        scores = self.pipeline.dq_scores()
        self.q_last = scores[-1].score.score if scores else None
        return self.q_last

    async def run(self, items: AsyncIterable[SessionItem] | Iterable[SessionItem], *,
                  stop: asyncio.Event | None = None) -> PipelineReport:
        async def gated() -> AsyncIterator[SessionItem]:
            async for it in _aiter(items):
                if stop is not None and stop.is_set():
                    break
                if isinstance(it, RawFrame) and stream_kind(it.stream) not in self.streams:
                    continue
                yield it

        self.report = await self.pipeline.run(gated())
        return self.report

    async def on_candle(self, c: Candle) -> None:
        """Стік конвеєра: закрита свічка → (команди) → крок циклу → запис."""
        await self.process_commands()
        if self._next_open is not None and c.open_time_ns < self._next_open:
            return                                          # перекриття з прогрівом — бар уже враховано
        if self._next_open is not None and c.open_time_ns > self._next_open:
            await self._fill_discontinuity(self._next_open, c.open_time_ns)
        await self._step(c)

    async def _fill_discontinuity(self, lo: int, hi: int) -> None:
        """Між прогрівом (або попереднім баром) і свічкою потоку дірка: добрати закриті бари REST (paper)
        або зафіксувати розрив у журналі (дані не вигадуються)."""
        got: list[Candle] = []
        if self._gap_filler is not None:
            fetched = await self._gap_filler(lo, hi)
            got = sorted((x for x in fetched if lo <= x.open_time_ns < hi and x.is_closed),
                         key=lambda x: x.open_time_ns)
        for x in got:
            if self._next_open is not None and x.open_time_ns == self._next_open:
                await self._step(x)
        if self._next_open is not None and self._next_open < hi:
            ts = self._wall_ns()
            missing = (hi - self._next_open) // TF_NS
            self.journal.append("feed.discontinuity", {"from_open_ns": self._next_open, "to_open_ns": hi,
                                                       "missing_bars": missing}, ts, ts)
            log.warning("feed discontinuity: %d missing bars before %d", (hi - self._next_open) // TF_NS, hi)

    async def _step(self, c: Candle) -> None:
        self.reload_limits_if_changed()
        now = self._now_ns()
        lag_ns = max(0, now - c.ts_event_ns)
        # StaleDataGuard рахує лаг як ts_ns − last_data_ns, де ts_ns — закриття бару (годинник рушія). Лаг
        # обробки живої свічки = «зараз» конвеєра − ts_event закриття (для добраної REST-свічки — хвилини),
        # тож last_data_ns = close − лаг (W-06)
        last_data_ns = c.close_time_ns - lag_ns
        q = self.q()
        dq = float_to_decimal_exact(q) if q is not None else Decimal(1)
        self.journal.append("candle", to_canonical(c), c.ts_event_ns, c.ts_ingest_ns)
        sr = self.loop.on_candle(c, dq_score=dq, last_data_ns=last_data_ns)
        self._next_open = c.open_time_ns + TF_NS
        self.equity.append(sr.equity)
        self.equity_ts.append(sr.close_time_ns)
        self.fills += len(sr.fills)
        self.closed_trades += len(sr.closed_positions)
        if sr.decision is not None:
            u = abs(sr.decision.u_final)
            self.max_abs_u = u if self.max_abs_u is None else max(self.max_abs_u, u)
        if sr.transition is not None:
            tr = sr.transition
            self.transitions.append((tr.ts_ns, tr.state_from.value, tr.state_to.value, tr.event.value))
        if self.halted_at_ns is None and sr.risk_state is RiskState.HALTED:
            self.halted_at_ns = sr.close_time_ns
        # скор — лише своєї свічки: добраний REST-бар (_fill_discontinuity) іде кроком ПЕРЕД свічкою потоку,
        # чий скор уже чекає, тож його не можна ні приписати добраному бару, ні скинути (WIRE-07)
        v = self._pending_anomaly
        anomaly = None
        if v is not None and v.t_ns == c.open_time_ns:
            anomaly, self._pending_anomaly = v, None
        await self.sink.on_step(self, StepOutput(sr, c, self._take_journal(), q, lag_ns / 1e6, anomaly))

    # ------------------------------------------------------------------ команди (між барами)

    async def process_commands(self) -> int:
        if self._commands is not None:
            for cmd in await self._commands():
                self.pending.put_nowait(cmd)
        n = 0
        while not self.pending.empty():
            await self.apply(self.pending.get_nowait())
            n += 1
        return n

    async def apply(self, cmd: ControlCommand) -> None:
        if cmd.kind == "killswitch.release":
            if cmd.run_id is not None and cmd.run_id != str(self.run_id):
                return
            await self.apply_release(actor=cmd.actor, role=cmd.role, audit_id=cmd.audit_id,
                                     user_id=cmd.user_id)
        elif cmd.kind == "risk.limits.changed":
            self.reload_limits_if_changed(force=True)

    async def apply_release(self, *, actor: str | None, role: str | None, audit_id: int | None = None,
                            user_id: int | None = None) -> Transition | None:
        """Зняття HALTED (лише admin). Не-admin → відмова в журналі; стан не змінюється.

        Записи audit_log воркера (killswitch.release, risk.release) — операційний журнал «хто, що, коли»: час
        у них — настінний момент застосування команди (а не час бару реплею, яким живе автомат), автор —
        user_id запиту API, а `request_audit_id` пов'язує їх із записом запиту. Запис risk_event переходу
        лишається на шкалі часу прогону (закриття останнього бару).
        """
        ts = self._wall_ns()
        state_before = self.loop.fsm.state.value
        self.journal.append("control.killswitch_release", {"audit_id": audit_id, "actor": actor, "role": role,
                                                           "state_before": state_before}, ts, ts)
        tr: Transition | None = None
        outcome = "noop"
        try:
            tr = self.loop.release_halt(Role(role or ""), actor=actor)
            outcome = "released" if tr is not None else "noop"
        except (PermissionDeniedError, ValueError):
            outcome = "denied"
        if tr is not None:
            self.transitions.append((tr.ts_ns, tr.state_from.value, tr.state_to.value, tr.event.value))
        risk = self.loop.take_pending_risk_events()
        audits = tuple(replace(a, ts_ns=ts, after={**dict(a.after), "request_audit_id": audit_id})
                       for a in self._audits)
        self._audits.clear()
        event = {"kind": "killswitch.release", "outcome": outcome, "audit_id": audit_id, "actor": actor,
                 "role": role, "user_id": user_id, "state_before": state_before,
                 "state_after": self.loop.fsm.state.value}
        await self.sink.on_out_of_band(self, risk=risk, audits=audits, journal=self._take_journal(),
                                       event=event)
        log.info("killswitch release %s by %s (%s): %s → %s", outcome, actor, role, state_before,
                 self.loop.fsm.state.value)
        return tr

    def reload_limits_if_changed(self, *, force: bool = False) -> bool:
        """Перечитати config/risk_limits.yaml, якщо файл змінився (PUT /risk/limits або редагування)."""
        p = self._limits_path
        if p is None:
            return False
        st = _stat(p)
        if not force and st == self._limits_stat:
            return False
        self._limits_stat = st
        sha = _sha(p)
        if sha is None or sha == self._limits_sha:
            return False
        from fuzzhelm.config import parse_yaml_text  # noqa: PLC0415

        ts = self._wall_ns()
        try:
            tree = parse_yaml_text(p.read_text(encoding="utf-8"), p.name)
            self.loop.apply_risk_limits(tree)
        except (ConfigValidationError, KeyError) as e:
            self.journal.append("control.risk_limits_rejected", {"sha256": sha, "error": str(e)}, ts, ts)
            log.error("risk limits file %s rejected: %s", p, e)
            return False
        self.journal.append("control.risk_limits", {"sha256_before": self._limits_sha, "sha256": sha},
                            ts, ts)
        log.info("risk limits reloaded: %s → %s", self._limits_sha, sha)
        self._limits_sha = sha
        return True

    # ------------------------------------------------------------------ завершення

    def health(self) -> dict[str, Any]:
        snap = self.pipeline.health.snapshot()
        return {
            "source": "trading_worker",
            "header": header_line(feed=self.feed, seed=self.seed, q=self.q_last),
            "mode": MODE, "feed": self.feed, "mainnet_keys": False, "seed": self.seed, "q": self.q_last,
            "run_id": str(self.run_id), "kind": self.kind.value, "risk_state": self.loop.fsm.state.value,
            "frames": snap.frames, "lag_p95_ms": snap.lag_p95_ms, "reconnects": snap.reconnects,
            "gaps_open": snap.gaps_open, "gaps_by_status": snap.gaps_by_status,
            "candles_closed": snap.candles_closed, "invalid": snap.invalid, "duplicates": snap.duplicates,
            **anomaly_health(self.pipeline.anomaly, snap.anomalies),
        }

    def summary(self, status: RunStatus, error: str | None = None) -> SessionSummary:
        return SessionSummary(
            run_id=self.run_id, kind=self.kind, status=status, error=error, warmup_bars=self.warmup_bars,
            bars=len(self.equity), fills=self.fills, closed_trades=self.closed_trades,
            final_state=self.loop.fsm.state, halted_at_ns=self.halted_at_ns,
            transitions=tuple(self.transitions), equity_first=self.equity[0] if self.equity else None,
            equity_last=self.equity[-1] if self.equity else None,
            equity_hash=equity_hash(self.equity, self.equity_ts) if self.equity else None,
            journal_head=self.journal.head.hex(), journal_entries=self.journal.next_seq, q_last=self.q_last,
            report=self.report, max_abs_u_final=self.max_abs_u,
        )

    async def finish(self, status: RunStatus = RunStatus.DONE, error: str | None = None) -> SessionSummary:
        ts = self._wall_ns()
        self.journal.append("session.end", {"status": status.value, "error": error, "bars": len(self.equity),
                                            "final_state": self.loop.fsm.state.value}, ts, ts)
        s = self.summary(status, error)
        await self.sink.on_finish(self, s, self._take_journal())
        return s


async def _aiter(items: AsyncIterable[SessionItem] | Iterable[SessionItem]) -> AsyncIterator[SessionItem]:
    if isinstance(items, AsyncIterable):
        async for it in items:
            yield it
    else:
        for it in items:
            yield it


def _stat(p: Path | None) -> tuple[int, int] | None:
    if p is None:
        return None
    try:
        st = p.stat()
    except OSError:
        return None
    return st.st_mtime_ns, st.st_size


def _sha(p: Path | None) -> str | None:
    if p is None:
        return None
    try:
        return file_sha256(p)
    except OSError:
        return None


def session_dataset_hash(*, feed: str, source: str, source_sha256: str | None, scenario: str | None,
                         streams: Sequence[str], warmup_dataset_hash: str, started_ns: int) -> str:
    """dataset_hash live-прогону: джерело потоку + прогрів + момент старту сесії (W-02).

    Live/replay-сесія — подія в часі (команди адміністратора, зміна лімітів надходять ззовні), тож дві
    сесії з тими самими даними — різні прогони; чиста ідентичність даних лишається в журналі (session.start).
    """
    payload = {"feed": feed, "source": source, "source_sha256": source_sha256, "scenario": scenario,
               "streams": sorted(streams), "warmup_dataset_hash": warmup_dataset_hash,
               "session_started_ns": started_ns}
    return hashlib.blake2b(canonical_json(payload), digest_size=32).hexdigest()


# ====================================================================== БД: прогрів, запис, керування


async def load_warmup(factory: Any, instrument_id: int, instrument: Instrument, first_open_ns: int, n: int,
                      *, fallback: GapFiller | None, fallback_name: str) -> tuple[Dataset, str]:
    """Прогрів з БД: n закритих 1m-барів [first_open − n·1m, first_open); бракує — `fallback` (свічки
    записуються в `candle` з src=REST і читаються знову з БД — прогрів завжди «з БД»)."""
    from fuzzhelm.storage.repositories import CandleRepo  # noqa: PLC0415
    from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

    lo = first_open_ns - n * TF_NS

    async def read() -> Dataset | None:
        async with session_scope(factory) as s:
            arr = await CandleRepo(s).load_arrays(instrument_id, "1m", lo, first_open_ns, closed_only=True)
        if arr.t_ns.size != n:
            return None
        ds = Dataset.from_candle_arrays(arr, instrument, tf="1m", source=f"db:warmup:[{lo},{first_open_ns})")
        return ds if int(ds.t_ns[0]) == lo else None

    ds = await read()
    if ds is not None:
        return ds, "db"
    if fallback is None:
        raise WorkerError(f"DB has fewer than {n} closed candles before the feed start and no fallback")
    candles = [c for c in await fallback(lo, first_open_ns) if lo <= c.open_time_ns < first_open_ns]
    async with session_scope(factory) as s:
        await CandleRepo(s).upsert(candles, instrument_id)
    ds = await read()
    if ds is None:
        raise WorkerError(f"warm-up still incomplete after {fallback_name}: need {n} contiguous bars")
    return ds, f"db+{fallback_name}"


def fixture_history_filler(path: Path, instrument: Instrument) -> GapFiller:
    """Офлайн-джерело історії для replay: записані рядки /fapi/v1/klines (fixtures/rest)."""
    import gzip  # noqa: PLC0415

    from fuzzhelm.ingest.normalize import normalize_rest_klines  # noqa: PLC0415

    async def fill(lo: int, hi: int) -> list[Candle]:
        raw = path.read_bytes()
        rows = json.loads(gzip.decompress(raw) if path.suffix == ".gz" else raw)
        sel = [r for r in rows if lo <= int(r[0]) * 1_000_000 < hi]
        if not sel:
            return []
        # ts_ingest — момент запису фікстури (capture_meta.json), а не «зараз»: дані не видаються за свіжі
        meta = path.parent / "capture_meta.json"
        ts_ingest = (int(json.loads(meta.read_text())["captured_at_local_ns"]) if meta.is_file()
                     else int(sel[-1][6]) * 1_000_000 + 1)
        return normalize_rest_klines(sel, instrument, ts_ingest)

    return fill


def rest_history_filler(settings: Settings, instrument: Instrument) -> GapFiller:
    """Живе джерело історії для paper: публічний REST /fapi/v1/klines (read-only host з allowlist)."""

    async def fill(lo: int, hi: int) -> list[Candle]:
        import httpx  # noqa: PLC0415

        from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
        from fuzzhelm.ingest.backfill import backfill_klines  # noqa: PLC0415
        from fuzzhelm.ingest.ratelimit import binance_request_bucket  # noqa: PLC0415
        from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
        from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415

        clock = SystemClock()
        async with httpx.AsyncClient(timeout=15) as http:
            client = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                                       RetryPolicy(rng_seed=settings.seed), clock=clock)
            offset = await client.server_time_offset_ms()
            res = await backfill_klines(client, instrument.symbol_venue, lo // 1_000_000,
                                        hi // 1_000_000 - 1, instrument=instrument,
                                        now_ms=clock.now_ns() // 1_000_000 + int(offset))
        return list(res.candles)

    return fill


def _dec_s(x: Decimal | None) -> str | None:
    if x is None:
        return None
    from fuzzhelm.core.money import dec_str  # noqa: PLC0415

    return dec_str(x)


class DbSink:
    """Запис сесії в PostgreSQL (LivePersister) + NOTIFY `fuzzhelm_live` у тій самій транзакції."""

    def __init__(self, factory: Any, *, instrument_id: int, symbol: str, passport: Any,
                 write_candles: bool = True, publish: bool = True) -> None:
        from fuzzhelm.workers.persist import LivePersister  # noqa: PLC0415

        self.factory = factory
        self.instrument_id = instrument_id
        self.passport = passport
        self.write_candles = write_candles
        self.publish = publish
        self.persister = LivePersister(passport.run_id, instrument_id, symbol)

    async def _notify(self, s: Any, kind: str, payload: Mapping[str, Any]) -> None:
        if not self.publish:
            return
        from fuzzhelm.api.live import LIVE_CHANNEL, encode_notify_payload  # noqa: PLC0415
        from fuzzhelm.workers.persist import pg_notify  # noqa: PLC0415

        try:
            text = encode_notify_payload(kind, payload)
        except ValueError as e:        # подія для браузера не може зірвати запис кроку/свічки
            log.warning("NOTIFY %s skipped: %s", kind, e)
            return
        await pg_notify(s, LIVE_CHANNEL, text)

    async def on_start(self, sess: TradingSession, journal: Sequence[JournalEntry]) -> None:
        from fuzzhelm.storage.repositories import JournalRepo  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415
        from fuzzhelm.workers.persist import create_run  # noqa: PLC0415

        async with session_scope(self.factory) as s:
            await create_run(s, self.passport)
            await JournalRepo(s).append_many(list(journal))
            await self._notify(s, "run", {"run_id": str(sess.run_id), "kind": sess.kind.value,
                                          "status": "RUNNING", "header": sess.health()["header"]})

    async def on_step(self, sess: TradingSession, out: StepOutput) -> None:
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        sr, c = out.step, out.candle
        rid = str(sess.run_id)
        async with session_scope(self.factory) as s:
            w = await self.persister.write_step(
                s, sr, candle=c if self.write_candles else None, journal=out.journal,
                anomaly_score=None if out.anomaly is None else db_anomaly_score(out.anomaly.score))
            await self._notify(s, "candle", {
                "run_id": rid, "symbol": c.instrument, "open_time_ns": c.open_time_ns, "o": _dec_s(c.o),
                "h": _dec_s(c.h), "l": _dec_s(c.l), "c": _dec_s(c.c), "v": _dec_s(c.volume)})
            d = sr.decision
            if d is not None and w.decision_id is not None:
                top = d.trace.fuzzy.fired[0] if d.trace is not None and d.trace.fuzzy.fired else None
                await self._notify(s, "decision", {
                    "id": w.decision_id, "run_id": rid, "open_time_ns": d.open_time_ns, "u_raw": d.u_raw,
                    "kappa": d.kappa, "u_final": d.u_final, "action": d.action, "verdict": d.verdict,
                    "target_side": d.target_side, "target_qty": _dec_s(d.target_qty),
                    "top_rule": None if top is None else top.rule_id,
                    "alpha": None if top is None else top.alpha, "orders": len(d.order_ids)})
            for f in sr.fills:
                await self._notify(s, "fill", {"run_id": rid, "side": int(f.side), "qty": _dec_s(f.qty),
                                               "price": _dec_s(f.price), "fee": _dec_s(f.fee),
                                               "ts_ns": f.ts_fill_ns})
            vetoes = [r for r in sr.risk_events if r.verdict is not None and r.verdict.value == "VETO"]
            if vetoes:
                await self._notify(s, "rejection", {"run_id": rid, "ts_ns": sr.close_time_ns, "items": [
                    {"rule": r.rule, "observed": _dec_s(r.observed), "limit": _dec_s(r.limit_value)}
                    for r in vetoes[:10]]})
            if sr.transition is not None:
                tr = sr.transition
                await self._notify(s, "risk", {
                    "run_id": rid, "ts_ns": tr.ts_ns, "state_from": tr.state_from.value,
                    "state_to": tr.state_to.value, "event": tr.event.value, "drawdown": _dec_s(tr.drawdown),
                    "day_return": _dec_s(tr.day_return)})
            p = sr.equity_point
            if p is not None:
                await self._notify(s, "equity", {
                    "run_id": rid, "ts_ns": p.ts_ns, "equity": _dec_s(p.equity),
                    "drawdown": _dec_s(p.drawdown), "risk_state": p.risk_state.value,
                    "kappa": _dec_s(p.kappa),
                    "position_qty": _dec_s(p.position_qty), "var95": _dec_s(w.var95),
                    "cvar95": _dec_s(w.cvar95)})
            await self._notify(s, "health", sess.health())

    async def on_out_of_band(self, sess: TradingSession, *, risk: Sequence[RiskEventRecord],
                             audits: Sequence[AuditRecord], journal: Sequence[JournalEntry],
                             event: Mapping[str, Any]) -> None:
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        uid = event.get("user_id")
        async with session_scope(self.factory) as s:
            await self.persister.write_out_of_band(s, risk=risk, audits=audits, journal=journal,
                                                   user_id=uid if isinstance(uid, int) else None)
            await self._notify(s, "risk", {"run_id": str(sess.run_id), **dict(event)})
            await self._notify(s, "health", sess.health())

    async def on_finish(self, sess: TradingSession, summary: SessionSummary,
                        journal: Sequence[JournalEntry]) -> None:
        from fuzzhelm.storage.repositories import JournalRepo  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415
        from fuzzhelm.workers.persist import finish_run  # noqa: PLC0415

        done = summary.status is RunStatus.DONE
        async with session_scope(self.factory) as s:
            # FAILED: записи кроку, що впав, відкотились разом із його транзакцією — у БД лишається цілий
            # ПРЕФІКС ланцюга; дописувати session.end після дірки і писати неперевірювані хеші не можна
            if journal and done:
                await JournalRepo(s).append_many(list(journal))
            # кінець вікна даних прогону (виключно): open бару після останнього спожитого = close + 1 мс
            ts_to = sess.equity_ts[-1] + 1_000_000 if done and sess.equity_ts else None
            await finish_run(s, sess.run_id, summary.status,
                             journal_head_hash=summary.journal_head if done else None,
                             equity_hash=summary.equity_hash if done else None, error=summary.error,
                             ts_to_ns=ts_to,
                             metrics={"bars": summary.bars, "fills": summary.fills,
                                      "closed_trades": summary.closed_trades,
                                      "halted": 1.0 if summary.halted_at_ns is not None else 0.0})
            await self._notify(s, "run", {"run_id": str(sess.run_id), "status": summary.status.value,
                                          "final_state": summary.final_state.value})


class ControlChannel:
    """Канал керування: LISTEN fuzzhelm_control (миттєве пробудження) + опитування audit_log (джерело
    правди: команда не губиться, якщо LISTEN-з'єднання тимчасово впало). Застосовуються лише команди,
    записані ПІСЛЯ старту цієї сесії; роль актора перевіряється ще раз за app_user (захист у глибину)."""

    def __init__(self, factory: Any, dsn: str | None) -> None:
        self.factory = factory
        self.dsn = dsn
        self.wake = asyncio.Event()
        self.last_audit_id = 0
        self._conn: Any = None
        self._limits_changed = False

    async def start(self) -> None:
        from fuzzhelm.storage.repositories import AuditRepo  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        async with session_scope(self.factory) as s:
            rows = await AuditRepo(s).list(action=RELEASE_ACTION, limit=1)
        self.last_audit_id = rows[0].id if rows else 0
        if self.dsn is None:
            return
        import asyncpg  # noqa: PLC0415

        from fuzzhelm.api.live import CONTROL_CHANNEL, decode_notify_payload  # noqa: PLC0415

        def on_notify(_c: Any, _pid: int, _ch: str, payload: str) -> None:
            decoded = decode_notify_payload(payload)
            if decoded is not None and decoded[0] == "risk.limits.changed":
                self._limits_changed = True
            self.wake.set()

        try:
            self._conn = await asyncpg.connect(self.dsn, timeout=5)
            await self._conn.add_listener(CONTROL_CHANNEL, on_notify)
        except (OSError, asyncpg.PostgresError, TimeoutError) as e:     # опитування audit_log лишається
            log.warning("LISTEN %s unavailable (%s); falling back to audit_log polling",
                        CONTROL_CHANNEL, type(e).__name__)
            self._conn = None

    async def poll(self) -> list[ControlCommand]:
        from fuzzhelm.storage.repositories import AuditRepo, UserRepo  # noqa: PLC0415
        from fuzzhelm.storage.session import session_scope  # noqa: PLC0415

        self.wake.clear()
        out: list[ControlCommand] = []
        if self._limits_changed:
            self._limits_changed = False
            out.append(ControlCommand("risk.limits.changed"))
        async with session_scope(self.factory) as s:
            rows = [r for r in await AuditRepo(s).list(action=RELEASE_ACTION, limit=50)
                    if r.id > self.last_audit_id]
            for r in sorted(rows, key=lambda r: r.id):
                user = None if r.user_id is None else await UserRepo(s).get(r.user_id)
                after = r.after_json or {}
                out.append(ControlCommand("killswitch.release", audit_id=r.id,
                                          actor=None if user is None else user.login,
                                          role=None if user is None else user.role,
                                          run_id=after.get("run_id"),
                                          user_id=None if user is None else user.id))
                self.last_audit_id = max(self.last_audit_id, r.id)
        return out

    async def stop(self) -> None:
        if self._conn is not None:
            with contextlib.suppress(Exception):
                await self._conn.close()
            self._conn = None


# ====================================================================== запуск


@dataclass(frozen=True)
class WorkerOptions:
    profile: str = "replay"
    speed: float | None = None
    scenario: str | None = None
    session: Path | None = None
    warmup_bars: int | None = None
    minutes: float | None = None
    linger_s: float = 0.0
    database_url: str | None = None
    no_db: bool = False
    write_candles: bool | None = None
    publish: bool = True
    params: tuple[tuple[str, str], ...] = ()      # перекриття полів BacktestConfig (--param u_enter=0.25)
    anomaly: bool = True                          # MLP-скорер з data/anomaly_mlp_<SYMBOL>.json (QualityGate)
    anomaly_model_dir: Path | None = None


_INT_PARAMS = frozenset({"n_atr", "warmup_bars"})
_FLOAT_PARAMS = frozenset({"chi", "u_enter", "u_exit", "rho_base", "lam", "tp_multiple"})


def _param_overrides(params: Sequence[tuple[str, str]]) -> dict[str, Any]:
    """`--param k=v` → поля BacktestConfig (лише параметри стратегії; невідомий ключ → WorkerError)."""
    out: dict[str, Any] = {}
    for k, v in params:
        if k in _INT_PARAMS:
            out[k] = int(v)
        elif k in _FLOAT_PARAMS:
            out[k] = float(v)
        elif k in ("engine", "cost_mode"):
            out[k] = v
        else:
            raise WorkerError(f"unknown --param {k!r}")
    return out


def public_error(exc: BaseException) -> str:
    """Текст помилки для run.error (видно в GET /runs/{id}): для помилок СУБД — лише клас і SQLSTATE, без SQL
    і параметрів (та сама політика, що й у API, API-16)."""
    from fuzzhelm.api.backtests import public_error as api_public_error  # noqa: PLC0415

    return api_public_error(exc)[:500]


async def offline_warmup_history(path: Path, instrument: Instrument, first_open_ns: int, n: int) -> Dataset:
    filler = fixture_history_filler(path, instrument)
    candles = await filler(first_open_ns - n * TF_NS, first_open_ns)
    if len(candles) < n:
        raise WorkerError(f"{path} has {len(candles)} bars before the feed start, need {n}")
    return history_window(Dataset.from_candles(candles, instrument), first_open_ns, n)


async def run_worker(opts: WorkerOptions, *, stop: asyncio.Event | None = None,
                     settings: Settings | None = None, sink: SessionSink | None = None) -> SessionSummary:
    """Один прогін воркера (replay або paper). Без БД (`no_db`) — MemorySink і прогрів з фікстури."""
    from fuzzhelm.infra.wallclock import SystemClock, new_run_id  # noqa: PLC0415

    settings = settings or get_settings()
    assert_no_mainnet(settings)
    prof = WorkerProfile.load(opts.profile)
    wall = SystemClock()
    stop = stop or asyncio.Event()
    replay = prof.kind is RunKind.REPLAY
    scenario = opts.scenario
    if not replay and scenario is not None:
        raise WorkerError("--scenario applies to the replay profile only")
    source_path: Path | None = None
    if replay:
        source_path = scenario_path(scenario) if scenario else (opts.session or prof.session)
        if source_path is None or not source_path.is_file():
            raise WorkerError(f"replay session file not found: {source_path}")
    cfg = prof.backtest_config(check_invariants=True, **_param_overrides(opts.params))
    n_warm = opts.warmup_bars or cfg.resolved_warmup()
    speed = opts.speed if opts.speed is not None else prof.speed
    run_id = new_run_id()
    feed = "REPLAY" if replay else "LIVE"
    engine = None
    control: ControlChannel | None = None
    rest_http: Any = None
    started_ns = wall.now_ns()
    try:
        if opts.no_db:
            from fuzzhelm.backtest.dataset import load_exchange_instrument  # noqa: PLC0415

            inst = load_exchange_instrument(prof.instrument.split("-")[0] + "USDT")
            factory = None
            iid = None
        else:
            from fuzzhelm.storage.models import APP_ROLE  # noqa: PLC0415
            from fuzzhelm.storage.repositories import InstrumentRepo  # noqa: PLC0415
            from fuzzhelm.storage.session import make_engine, session_factory, session_scope  # noqa: PLC0415

            url = opts.database_url or settings.database_url
            engine = make_engine(url, role=APP_ROLE)
            factory = session_factory(engine)
            async with session_scope(factory) as s:
                row = await InstrumentRepo(s).get_by_canon(prof.instrument)
            if row is None:
                raise WorkerError(f"instrument {prof.instrument} is not in the DB: "
                                  "run `fuzzhelm backfill` first")
            inst, iid = row.to_dto(), row.id
        history_src = prof.history or FIXTURES_DIR / "rest" / "binance_klines.json.gz"
        if replay:
            assert source_path is not None
            first_open = first_kline_open_ns(source_path)
            filler: GapFiller | None = fixture_history_filler(history_src, inst)
            filler_name = "fixture"
        else:
            first_open = (wall.now_ns() // TF_NS) * TF_NS            # поточна (ще відкрита) хвилина
            filler = rest_history_filler(settings, inst)
            filler_name = "rest"
        if factory is None:
            if not replay:
                raise WorkerError("paper profile needs the DB (use replay for the offline demo)")
            assert filler is not None
            warm = await offline_warmup_history(history_src, inst, first_open, n_warm)
            warm_src = "fixture"
        else:
            assert iid is not None
            warm, warm_src = await load_warmup(factory, iid, inst, first_open, n_warm, fallback=filler,
                                               fallback_name=filler_name)
        source_sha = file_sha256(source_path) if source_path is not None else None
        ds_hash = session_dataset_hash(
            feed=feed, source=_rel_str(source_path) if source_path else
            settings.binance_ws_market, source_sha256=source_sha, scenario=scenario, streams=prof.streams,
            warmup_dataset_hash=warm.dataset_hash, started_ns=started_ns)
        gs = read_git_state()
        sha, dirty = gs.sha, gs.dirty
        scorer = (load_anomaly_scorer(inst.symbol_venue, model_dir=opts.anomaly_model_dir)
                  if opts.anomaly else None)
        commands: CommandSource | None = None
        if factory is not None:
            from fuzzhelm.api.live import asyncpg_dsn  # noqa: PLC0415
            from fuzzhelm.workers.persist import Passport  # noqa: PLC0415

            control = ControlChannel(factory, asyncpg_dsn(opts.database_url or settings.database_url))
            await control.start()
            commands = control.poll
            config = cfg.identity_dict()
            passport = Passport(run_id=run_id, kind=prof.kind, config=config, config_hash=cfg.config_hash,
                                dataset_hash=ds_hash, seed=prof.seed, engine=str(cfg.engine), git_sha=sha,
                                git_dirty=dirty, instrument_id=iid, tf=prof.tf, ts_from_ns=first_open,
                                started_at_ns=started_ns)
            write_candles = opts.write_candles if opts.write_candles is not None else scenario is None
            assert iid is not None
            sink = sink or DbSink(factory, instrument_id=iid, symbol=inst.symbol_canon, passport=passport,
                                  write_candles=write_candles, publish=opts.publish)
        backfill = None
        if not replay:
            backfill, rest_http = _live_backfill(settings, inst)
        session = TradingSession(
            inst, cfg, seed=prof.seed, run_id=run_id, kind=prof.kind, feed=feed, warmup_bars=n_warm,
            sink=sink, streams=prof.streams, heartbeat_timeout_s=prof.heartbeat_timeout_s,
            pipeline_clock=FrameClock() if replay else wall, wall_clock=wall, backfill=backfill,
            src=Src.REPLAY if replay else Src.WS, commands=commands,
            gap_filler=None if replay else filler, limits_path=settings.config_dir / "risk_limits.yaml",
            anomaly=scorer)
        await session.begin({
            "profile": prof.name, "kind": prof.kind.value, "mode": MODE, "feed": feed, "scenario": scenario,
            "source": None if source_path is None else _rel_str(source_path),
            "source_sha256": source_sha, "streams": list(prof.streams), "speed": None if math.isinf(speed)
            else str(speed), "warmup": {"bars": n_warm, "source": warm_src, "dataset_hash": warm.dataset_hash,
                                        "from_ns": int(warm.t_ns[0]), "to_ns": int(warm.t_ns[-1])},
            "config_hash": cfg.config_hash, "dataset_hash": ds_hash, "seed": prof.seed, "engine": cfg.engine,
            "git_sha": sha, "git_dirty": dirty, "git_dirty_paths": list(gs.dirty_paths),
            "anomaly_model": None if scorer is None else scorer.label, "instrument": inst.symbol_canon,
            "execution_host": settings.venue_base_url, "mainnet_keys": False})
        session.warm_up(warm)
        log.info("%s run %s: warm-up %d bars (%s), feed %s", prof.kind.value, run_id, n_warm, warm_src,
                 source_path or settings.binance_ws_market)
        try:
            items = _feed_items(prof, source_path, speed, settings=settings, inst=inst, wall=wall, stop=stop,
                                minutes=opts.minutes)
            await session.run(items, stop=stop)
            if opts.linger_s > 0:
                await _linger(session, control, opts.linger_s, stop)
            summary = await session.finish(RunStatus.DONE)
        except Exception as e:
            if session.started:
                with contextlib.suppress(Exception):
                    await session.finish(RunStatus.FAILED, public_error(e))
            raise
        return summary
    finally:
        if control is not None:
            await control.stop()
        if rest_http is not None:
            await rest_http.aclose()
        if engine is not None:
            await engine.dispose()


def _live_backfill(settings: Settings, inst: Instrument) -> tuple[BackfillHook, Any]:
    """REST-добір прогалин живого потоку (paper): той самий RestBackfiller, що й у ingest-воркері.
    Повертає і HTTP-клієнта — його закриває run_worker наприкінці прогону."""
    import httpx  # noqa: PLC0415

    from fuzzhelm.infra.wallclock import SystemClock  # noqa: PLC0415
    from fuzzhelm.ingest.pipeline import RestBackfiller  # noqa: PLC0415
    from fuzzhelm.ingest.ratelimit import binance_request_bucket  # noqa: PLC0415
    from fuzzhelm.ingest.rest_client import BinanceRestClient  # noqa: PLC0415
    from fuzzhelm.ingest.retry import RetryPolicy  # noqa: PLC0415

    clock = SystemClock()
    http = httpx.AsyncClient(timeout=15)       # живе до кінця прогону (закриває run_worker)
    client = BinanceRestClient(settings.binance_rest_base, http, binance_request_bucket(clock),
                               RetryPolicy(rng_seed=settings.seed), clock=clock)
    return RestBackfiller(client, inst), http


async def _feed_items(prof: WorkerProfile, source: Path | None, speed: float, *, settings: Settings,
                      inst: Instrument, wall: Clock, stop: asyncio.Event,
                      minutes: float | None) -> AsyncIterator[SessionItem]:
    if prof.kind is RunKind.REPLAY:
        from fuzzhelm.ingest.replay import ReplayFeed  # noqa: PLC0415

        assert source is not None
        feed = ReplayFeed(source, speed=speed, clock=wall, instrument=inst, streams=prof.streams)
        async for it in feed.items():
            yield it
        return
    ws = make_ws_client(settings, wall, [inst])
    deadline = None if minutes is None else wall.now_ns() + int(minutes * 60e9)
    try:
        async for it in ws.items():
            if stop.is_set() or (deadline is not None and wall.now_ns() >= deadline):
                break
            yield it
    finally:
        await ws.aclose()


def make_ws_client(settings: Settings, clock: Clock, instruments: Sequence[Instrument]) -> Any:
    """Живий WS-клієнт: мітки кадрів — `clock` (час події), сторож тиші — MonotonicClock (WS-07)."""
    from fuzzhelm.infra.wallclock import MonotonicClock  # noqa: PLC0415
    from fuzzhelm.ingest.ws_client import BinanceWsClient  # noqa: PLC0415

    return BinanceWsClient(settings, clock, instruments=list(instruments), src=Src.WS,
                           monotonic=MonotonicClock())


async def _linger(session: TradingSession, control: ControlChannel | None, seconds: float,
                  stop: asyncio.Event) -> None:
    """Після кінця потоку прогін лишається RUNNING `seconds` с і приймає команди адміністратора (демо:
    HALTED до зняття через POST /risk/killswitch/release)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while not stop.is_set() and loop.time() < end:
        await session.process_commands()
        wake = control.wake if control is not None else asyncio.Event()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(wake.wait(), timeout=min(0.5, max(0.0, end - loop.time())))
    await session.process_commands()


# ====================================================================== CLI


def parse_args(argv: Sequence[str] | None = None) -> WorkerOptions:
    ap = argparse.ArgumentParser(prog="python -m fuzzhelm.workers.trading_worker",
                                 description="FuzzHelm trading worker (paper execution only; no mainnet).")
    ap.add_argument("--profile", choices=("replay", "paper"), default="replay")
    ap.add_argument("--speed", type=str, default=None,
                    help="replay pace multiplier (e.g. 30), 'inf' = no waiting; default from the profile")
    ap.add_argument("--scenario", default=None, help="replay a stress scenario from fixtures/ws/scenarios")
    ap.add_argument("--session", type=Path, default=None, help="replay this session file instead")
    ap.add_argument("--warmup-bars", type=int, default=None)
    ap.add_argument("--minutes", type=float, default=None, help="paper: stop after N minutes")
    ap.add_argument("--linger", type=float, default=0.0,
                    help="after the feed ends keep the run RUNNING N seconds for admin commands")
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--no-db", action="store_true", help="in-memory run (replay only), prints a summary")
    ap.add_argument("--no-candles", action="store_true", help="do not upsert consumed candles")
    ap.add_argument("--no-notify", action="store_true", help="do not NOTIFY fuzzhelm_live")
    ap.add_argument("--no-anomaly", action="store_true",
                    help="do not load the MLP anomaly model (data/anomaly_mlp_<SYMBOL>.json)")
    ap.add_argument("--param", action="append", default=[], metavar="KEY=VALUE",
                    help="override a strategy parameter of the profile, e.g. u_enter=0.25 (repeatable)")
    a = ap.parse_args(argv)
    speed = None if a.speed is None else (math.inf if a.speed == "inf" else float(a.speed))
    if speed is not None and not speed > 0:
        ap.error("--speed must be > 0")
    return WorkerOptions(profile=a.profile, speed=speed, scenario=a.scenario, session=a.session,
                         warmup_bars=a.warmup_bars, minutes=a.minutes, linger_s=a.linger,
                         database_url=a.database_url, no_db=a.no_db,
                         write_candles=False if a.no_candles else None, publish=not a.no_notify,
                         params=tuple(_kv(x) for x in a.param), anomaly=not a.no_anomaly)


def _kv(x: str) -> tuple[str, str]:
    k, sep, v = x.partition("=")
    if not sep or not k:
        raise SystemExit(f"--param expects KEY=VALUE, got {x!r}")
    return k.strip(), v.strip()


def main(argv: Sequence[str] | None = None) -> int:
    setup_logging()
    opts = parse_args(argv)

    async def amain() -> SessionSummary:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        return await run_worker(opts, stop=stop)

    try:
        summary = asyncio.run(amain())
    except (WorkerError, ConfigValidationError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))
    return 0 if summary.status is RunStatus.DONE else 1


if __name__ == "__main__":
    raise SystemExit(main())
