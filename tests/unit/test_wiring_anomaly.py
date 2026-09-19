"""Проводка MLP-детектора аномалій у робочий контур (WIRE-01): JSON-артефакт моделі, побітове відтворення
скорів, прогрів скорера, QualityGate конвеєра інжесту (скор → on_anomaly → свічка, аномалії → Q) і торгового
воркера (скор свічки у StepOutput, лічильники в health).

Найменування: tests/unit/test_wiring_anomaly.py
Автор: Андрій Жук, 2026.

Офлайн: бари — записані REST klines (fixtures/rest/binance_klines.json.gz), модель — або навчена в тесті на
перших барах фікстури (0.2 с), або закомічений артефакт data/anomaly_mlp_BTCUSDT.json.
"""

from __future__ import annotations

import functools
import gzip
import json
import logging
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar
from uuid import UUID

import numpy as np
import orjson
import pytest

from fuzzhelm.core.dto import Candle
from fuzzhelm.core.enums import RunKind, RunStatus
from fuzzhelm.core.money import quantize_price
from fuzzhelm.features.convert import Bar, bar_from_candle
from fuzzhelm.ingest.normalize import normalize_exchange_info, normalize_rest_klines
from fuzzhelm.ingest.pipeline import HOUR_NS, IngestPipeline, PipelineSinks, contiguous_tail
from fuzzhelm.ingest.recorder import RawFrame
from fuzzhelm.ingest.replay import iter_items
from fuzzhelm.quality.anomaly_mlp import (
    ANOMALY_SCORE_DB_MAX,
    FEATURE_NAMES,
    AnomalyAutoencoder,
    AnomalyModelError,
    AnomalyScorer,
    AnomalyVerdict,
    db_anomaly_score,
    default_model_path,
    feature_matrix,
    load_anomaly_scorer,
    load_model_artifact,
    model_artifact,
    write_model_artifact,
)

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
ARTIFACT = ROOT / "data" / "anomaly_mlp_BTCUSDT.json"
INSTR = normalize_exchange_info(json.loads((REST / "exchange_info.json").read_text()), ["BTCUSDT"])["BTCUSDT"]
SEED = 20260918
MIN_NS = 60_000_000_000


def _candles() -> list[Candle]:
    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
    now_ms = rows[-1][6] + 1_000
    return normalize_rest_klines(rows, INSTR, now_ms * 1_000_000, server_time_ms=now_ms)


CANDLES = _candles()
BARS = [bar_from_candle(c) for c in CANDLES]


@functools.cache
def _trained_5_3_5() -> tuple[AnomalyAutoencoder, np.ndarray]:
    X, _ = feature_matrix(BARS)
    return AnomalyAutoencoder(seed=SEED).fit(X[:1300, :5]), X[:, :5]


# ================================================================ артефакт моделі


def test_model_artifact_roundtrip_reproduces_scores_bit_for_bit(tmp_path: Path) -> None:
    """JSON-артефакт (не pickle) відтворює мережу точно: ті самі скори до біта — пакетом і потоково."""
    model, X = _trained_5_3_5()
    doc = model_artifact(model, symbol="BTCUSDT", training={"note": "test"})
    path = write_model_artifact(tmp_path / "anomaly_mlp_BTCUSDT.json", doc)
    loaded = load_model_artifact(path)
    assert loaded.doc["architecture"] == "5-3-5" and loaded.model.features == FEATURE_NAMES[:5]
    assert loaded.model.threshold == model.threshold and loaded.doc["model"]["warmup_bars"] == 219
    assert np.array_equal(loaded.model.score(X), model.score(X))                     # пакет: побітово
    a, b = AnomalyScorer(model), loaded.scorer()
    va = [v for bar in BARS if (v := a.update(bar)) is not None]
    vb = [v for bar in BARS if (v := b.update(bar)) is not None]
    assert len(va) == len(vb) == len(BARS) - 218
    assert [(v.t_ns, v.score, v.anomaly) for v in va] == [(v.t_ns, v.score, v.anomaly) for v in vb]
    assert all(len(v.features) == 5 for v in vb) and a.flagged == b.flagged > 0
    # 8-ознакова модель і далі працює з тим самим скорером (зворотна сумісність)
    X8, _ = feature_matrix(BARS[:1500])
    assert AnomalyScorer(AnomalyAutoencoder(seed=SEED).fit(X8)).n_features == 8


def test_model_artifact_rejects_tampering_and_foreign_documents(tmp_path: Path) -> None:
    model, _ = _trained_5_3_5()
    doc = model_artifact(model, symbol="BTCUSDT")
    tampered = json.loads(json.dumps(doc))
    tampered["model"]["layers"][0]["coef"][0][0] += 1e-9                              # дайджест не збігся
    bad = tmp_path / "t.json"
    bad.write_text(json.dumps(tampered), encoding="utf-8")
    with pytest.raises(AnomalyModelError, match="params_sha256"):
        load_model_artifact(bad)
    bad.write_text(json.dumps({**doc, "kind": "pickle"}), encoding="utf-8")
    with pytest.raises(AnomalyModelError):
        load_model_artifact(bad)
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(AnomalyModelError):
        load_model_artifact(bad)
    # артефакт іншого символу не підміняє модель інструмента
    other = write_model_artifact(tmp_path / "anomaly_mlp_ETHUSDT.json", doc)
    with pytest.raises(AnomalyModelError, match="BTCUSDT"):
        load_anomaly_scorer("ETHUSDT", path=other)


def test_missing_model_file_disables_scoring_with_a_warning(tmp_path: Path,
                                                            caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="fuzzhelm.quality.anomaly"):
        assert load_anomaly_scorer("ETHUSDT", model_dir=tmp_path) is None
    assert "not found" in caplog.text and "disabled" in caplog.text
    assert default_model_path("btcusdt", tmp_path) == tmp_path / "anomaly_mlp_BTCUSDT.json"


def test_committed_btcusdt_artifact_is_the_5_3_5_model_trained_on_the_calibration_window() -> None:
    loaded = load_model_artifact(ARTIFACT)
    d = loaded.doc
    cal = json.loads((ROOT / "data" / "calibration_manifest.json").read_text(encoding="utf-8"))
    assert d["architecture"] == "5-3-5" and loaded.model.features == FEATURE_NAMES[:5]
    assert d["training"]["dataset_hash"] == cal["dataset"]["dataset_hash"]          # ті самі бари, дні 1–15
    assert d["training"]["n_bars"] == cal["dataset"]["n_bars"] == 21_600
    assert d["training"]["random_state"] == SEED and d["training"]["converged"] is True
    assert d["evaluation"]["same_threshold_as_evaluated_model"] is True
    assert d["model"]["threshold"] == loaded.model.threshold > 0
    assert {"sklearn", "numpy", "python"} <= set(d["versions"]) and d["decision"]
    # на записаних барах іншої доби: скори скінченні, стрибок +2 % за хвилину — аномалія
    sc = loaded.scorer()
    verdicts = [v for b in BARS[:2500] if (v := sc.update(b)) is not None]
    assert verdicts and all(np.isfinite(v.score) for v in verdicts)
    spiked = AnomalyScorer(loaded.model)
    spiked.warm_up(BARS[:2499])
    b = BARS[2499]
    c = BARS[2498].c * 1.02
    v = spiked.update(Bar(b.t_ns, b.o, max(b.h, c), b.l, c, b.v, b.qv, b.n))
    assert v is not None and v.anomaly and v.score > v.threshold


def test_db_anomaly_score_fits_numeric_10_6() -> None:
    assert db_anomaly_score(2.8652771208869940) == Decimal("2.865277")
    assert db_anomaly_score(1e9) == ANOMALY_SCORE_DB_MAX == Decimal("9999.999999")   # без numeric overflow
    assert db_anomaly_score(float("nan")) is None and db_anomaly_score(float("inf")) is None
    assert db_anomaly_score(np.float64(0.5)) == Decimal("0.500000")


# ================================================================ конвеєр інжесту (QualityGate §4.1)


def test_contiguous_tail_keeps_only_the_gapless_run_adjacent_to_the_first_live_bar() -> None:
    bars = BARS[:20]
    lo, hi = bars[0].t_ns, bars[-1].t_ns + MIN_NS
    assert [b.t_ns for b in contiguous_tail(reversed(bars), lo, hi, MIN_NS)] == [b.t_ns for b in bars]
    holed = bars[:5] + bars[6:]                                     # дірка на 6-й хвилині
    assert [b.t_ns for b in contiguous_tail(holed, lo, hi, MIN_NS)] == [b.t_ns for b in bars[6:]]
    assert contiguous_tail(bars[:-1], lo, hi, MIN_NS) == []          # немає бару впритул до першої свічки


async def test_pipeline_warms_up_scores_every_candle_and_counts_anomalies_into_q() -> None:
    """Прогрів з хука → перша ж жива свічка має скор; on_anomaly раніше за on_candle (один запис у БД);
    аномалія (стрибок +1 %) — у health.anomalies і в anomaly_count/validity години Q (§5.17)."""
    feed = list(CANDLES[1000:1120])
    spike_i = 100
    spike = quantize_price(feed[spike_i - 1].c * Decimal("1.01"), INSTR.tick_size)
    feed[spike_i] = feed[spike_i].model_copy(update={"c": spike, "h": max(feed[spike_i].h, spike)})
    calls: list[tuple[int, int]] = []

    async def history(lo: int, hi: int) -> list[Bar]:
        calls.append((lo, hi))
        return BARS[:1000]                                          # зайве конвеєр відкидає сам

    order: list[tuple[str, int]] = []
    verdicts: list[AnomalyVerdict] = []

    def on_anomaly(v: AnomalyVerdict) -> None:
        verdicts.append(v)
        order.append(("anomaly", v.t_ns))

    sinks = PipelineSinks(on_anomaly=on_anomaly, on_candle=lambda c: order.append(("candle", c.open_time_ns)))
    scorer = load_model_artifact(ARTIFACT).scorer()
    pipe = IngestPipeline(INSTR, sinks=sinks, anomaly=scorer, anomaly_warmup=history)
    for c in feed:
        await pipe.process_event(c)
    rep = await pipe.finish()
    first = feed[0].open_time_ns
    assert calls == [(first - 218 * MIN_NS, first)] and scorer.warmup_bars_fed == 218
    assert [v.t_ns for v in verdicts] == [c.open_time_ns for c in feed]          # скор з першої свічки
    for t in (c.open_time_ns for c in feed):
        assert order.index(("anomaly", t)) + 1 == order.index(("candle", t))
    assert verdicts[spike_i].anomaly
    flagged = sum(v.anomaly for v in verdicts)
    assert rep.health.anomalies == flagged == scorer.flagged and scorer.scored == len(feed)
    assert sum(h.score.inputs.anomaly_count for h in rep.dq) == flagged
    hour = next(h for h in rep.dq if h.hour_start_ns == feed[spike_i].open_time_ns // HOUR_NS * HOUR_NS)
    i = hour.score.inputs
    assert i.anomaly_count >= 1 and hour.score.validity == pytest.approx(
        1 - (i.invalid_count + i.anomaly_count) / i.total_count) and hour.score.validity < 1.0


async def test_pipeline_warmup_hole_or_failure_means_partial_cold_start_not_a_crash() -> None:
    feed = CANDLES[1000:1010]
    first = feed[0].open_time_ns

    async def holed(lo: int, hi: int) -> list[Bar]:
        return [b for b in BARS[:1000] if b.t_ns != first - 50 * MIN_NS]       # дірка за 50 хв до потоку

    async def broken(lo: int, hi: int) -> list[Bar]:
        raise ConnectionError("db down")

    got: dict[str, list[AnomalyVerdict]] = {}
    for name, hook in (("holed", holed), ("broken", broken)):
        verdicts: list[AnomalyVerdict] = []
        sc = load_model_artifact(ARTIFACT).scorer()
        pipe = IngestPipeline(INSTR, sinks=PipelineSinks(on_anomaly=verdicts.append), anomaly=sc,
                              anomaly_warmup=hook)
        for c in feed:
            await pipe.process_event(c)
        await pipe.finish()
        got[name] = verdicts
        if name == "broken":
            assert pipe.anomaly_warmup_error == "ConnectionError: db down" and sc.warmup_bars_fed == 0
        else:
            assert sc.warmup_bars_fed == 49 and pipe.anomaly_warmup_error is None
    assert got["holed"] == [] and got["broken"] == []             # 49 < 218 барів — скорів ще немає


async def test_pipeline_without_model_counts_no_anomalies() -> None:
    pipe = IngestPipeline(INSTR)
    for c in CANDLES[:5]:
        await pipe.process_event(c)
    rep = await pipe.finish()
    assert rep.health.anomalies == 0 and all(h.score.inputs.anomaly_count == 0 for h in rep.dq)


# ================================================================ торговий воркер


async def test_trading_session_primes_scorer_with_warmup_history_and_reports_scores() -> None:
    """Прогрів TradingLoop (523 бари) іде й в екстрактор скорера: перша жива свічка вже має скор; скор
    потрапляє в StepOutput (DbSink пише його в candle.anomaly_score), лічильники — у health."""
    from fuzzhelm.backtest.dataset import load_exchange_instrument  # noqa: PLC0415
    from fuzzhelm.workers.trading_worker import (  # noqa: PLC0415
        MemorySink,
        TradingSession,
        WorkerProfile,
        first_kline_open_ns,
        offline_warmup_history,
    )

    session_path = ROOT / "fixtures" / "ws" / "btcusdt_2026-09-18.jsonl.gz"
    prof = WorkerProfile.load("replay")
    cfg = prof.backtest_config(check_invariants=True)
    inst = load_exchange_instrument("BTCUSDT")
    n = cfg.resolved_warmup()
    first_open = first_kline_open_ns(session_path)
    assert prof.history is not None
    warm = await offline_warmup_history(prof.history, inst, first_open, n)
    scorer = load_anomaly_scorer("BTCUSDT")
    assert scorer is not None
    sink = MemorySink()
    sess = TradingSession(inst, cfg, seed=prof.seed, run_id=UUID(int=7), kind=RunKind.REPLAY, feed="REPLAY",
                          warmup_bars=n, sink=sink, streams=prof.streams,
                          heartbeat_timeout_s=prof.heartbeat_timeout_s, anomaly=scorer)
    await sess.begin({"test": "wiring"})
    sess.warm_up(warm)
    assert scorer.warmup_bars_fed == n == 523
    stop_ns = first_open + 4 * MIN_NS + 30 * 1_000_000_000         # перші 4 закриті хвилини потоку

    def head() -> Any:
        for it in iter_items(session_path, prof.streams):
            if isinstance(it, RawFrame) and it.ts_ingest_ns > stop_ns:
                return
            yield it

    await sess.run(head())
    summary = await sess.finish(RunStatus.DONE)
    assert summary.status is RunStatus.DONE and len(sink.steps) >= 3
    for out in sink.steps:
        assert out.anomaly is not None and out.anomaly.t_ns == out.candle.open_time_ns
    h = sess.health()
    assert h["anomaly_scored"] == scorer.scored == len(sink.steps)
    assert h["anomaly_model"] == scorer.label and h["anomalies"] == scorer.flagged
    assert len(json.dumps(h)) < 7900                                # влазить у NOTIFY


# ================================================================ ingest-воркер: стік свічки + скору (без БД)


class _FakeCandleRepo:
    calls: ClassVar[list[tuple[str, Any]]] = []

    def __init__(self, session: Any) -> None:
        self.s = session

    async def upsert(self, candles: list[Candle], iid: int) -> Any:
        from fuzzhelm.storage.repositories.candle import UpsertResult  # noqa: PLC0415

        self.calls.append(("upsert", (self.s, candles[0].open_time_ns, iid)))
        return UpsertResult(1, 0, 0)

    async def set_anomaly_scores(self, iid: int, tf: str, scores: list[tuple[int, Decimal]]) -> int:
        self.calls.append(("score", (self.s, iid, tf, list(scores))))
        return len(scores)


class _FakeRepo:
    def __init__(self, session: Any) -> None:
        pass

    async def append_many(self, entries: list[Any]) -> int:
        return len(entries)

    async def upsert(self, row: Any) -> None:
        return None


async def test_ingest_worker_sink_writes_candle_and_its_score_in_one_transaction(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """InstrumentSink: скор приходить ДО свічки, тож upsert свічки і UPDATE anomaly_score — в одній транзакції
    (одна й та сама сесія); значення — db_anomaly_score; лічильники — у статистиці й health воркера."""
    from contextlib import asynccontextmanager  # noqa: PLC0415

    from fuzzhelm.core.journal import EventJournal, JournalEntry  # noqa: PLC0415
    from fuzzhelm.storage import repositories, session  # noqa: PLC0415
    from fuzzhelm.workers.ingest_worker import IngestStats, InstrumentSink  # noqa: PLC0415

    txns: list[object] = []

    @asynccontextmanager
    async def fake_scope(factory: Any) -> Any:
        s = object()
        txns.append(s)
        yield s

    _FakeCandleRepo.calls = []
    monkeypatch.setattr(repositories, "CandleRepo", _FakeCandleRepo)
    monkeypatch.setattr(repositories, "JournalRepo", _FakeRepo)
    monkeypatch.setattr(repositories, "DqRepo", _FakeRepo)
    monkeypatch.setattr(session, "session_scope", fake_scope)
    buf: list[JournalEntry] = []
    stats = IngestStats()
    sink = InstrumentSink(None, INSTR, 7, journal=EventJournal(UUID(int=1), sink=buf.append, keep=False),
                          buffer=buf, stats=stats, journal_kinds=frozenset({"candle"}), publish=False)
    scorer = load_model_artifact(ARTIFACT).scorer()

    async def history(lo: int, hi: int) -> list[Bar]:
        return BARS[:1000]

    sink.pipeline = IngestPipeline(INSTR, sinks=sink.sinks(), anomaly=scorer, anomaly_warmup=history)
    feed = CANDLES[1000:1010]
    for c in feed:
        await sink.pipeline.process_event(c)
    upserts = [a for k, a in _FakeCandleRepo.calls if k == "upsert"]
    scores = [a for k, a in _FakeCandleRepo.calls if k == "score"]
    assert [u[1] for u in upserts] == [c.open_time_ns for c in feed]
    assert len(scores) == len(feed) and all(s_[0] is u[0] for s_, u in zip(scores, upserts, strict=True))
    for (_, iid, tf, [(t, val)]), c in zip(scores, feed, strict=True):
        assert (iid, tf, t) == (7, "1m", c.open_time_ns) and isinstance(val, Decimal)
    assert stats.anomaly_scores == len(feed) == scorer.scored and stats.anomalies == scorer.flagged
    h = sink.health()
    assert h["anomaly_scored"] == len(feed) and h["anomaly_model"] == scorer.label and "anomalies" in h
