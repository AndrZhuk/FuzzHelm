"""CLI `fuzzhelm`: розбір аргументів, dry-run без мережі й БД, вікно датасету, план добору, крос-звірка
зі збережених сирих відповідей, нормалізація і файли історії фінансування, REST-ендпоінти хвилі 2,
керування користувачами (`fuzzhelm user`) на репозиторіях у пам'яті.

Найменування: tests/unit/test_cli.py
Призначення: усе, що CLI робить БЕЗ мережі й БД, — детерміновано і швидко; мережеві шляхи — лише через respx.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import getpass
import gzip
import io
import json
import logging
import sys
from collections.abc import AsyncIterator, Iterator, Mapping
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import orjson
import pytest
import respx
from passlib.context import CryptContext

from fuzzhelm import cli
from fuzzhelm.backtest.manifest import dataset_hash
from fuzzhelm.core.clock import ManualClock
from fuzzhelm.core.enums import Role, Venue
from fuzzhelm.core.errors import NormalizationError
from fuzzhelm.ingest.funding import (
    fetch_funding_history,
    funding_columns,
    funding_document,
    load_funding_json,
    write_funding_json,
)
from fuzzhelm.ingest.normalize import FundingRate, normalize_funding_rate, normalize_funding_rates
from fuzzhelm.ingest.ratelimit import binance_request_bucket, request_weight
from fuzzhelm.ingest.rest_client import BinanceRestClient
from fuzzhelm.ingest.retry import RetryPolicy
from fuzzhelm.ingest.symbols import BTC_USDT_PERP, ETH_USDT_PERP
from fuzzhelm.storage.repositories import UserRow
from fuzzhelm.storage.repositories.user import hash_password, verify_password

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
DAY = 86_400_000
BASE = "https://fapi.binance.com"


async def _no_sleep(_: float) -> None:
    return None


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """Будь-яка спроба відкрити HTTP-клієнт або БД у dry-run — провал тесту (доказ «без мережі й БД»)."""

    def boom(*_: Any, **__: Any) -> Any:
        raise AssertionError("dry-run must not touch the network or the database")

    monkeypatch.setattr(httpx, "AsyncClient", boom)
    monkeypatch.setattr(cli, "_engine", boom)


def _window_json(tmp_path: Path, end: date = date(2026, 9, 18), days: int = 45) -> Path:
    w = cli.DatasetWindow.ending_at(cli.utc_date_ms(end), days)
    path = tmp_path / "dataset_window.json"
    path.write_bytes(orjson.dumps({"v": 1, "window": w.to_dict()}))
    return path


# ================================================================== argparse


def test_parser_backfill_defaults_and_symbol_parsing() -> None:
    a = cli.build_parser().parse_args(["backfill"])
    assert a.command == "backfill" and a.days == 45 and a.symbols == ("BTCUSDT", "ETHUSDT")
    assert a.limit == 1500 and a.end_date is None and a.dry_run is False
    a = cli.build_parser().parse_args(["backfill", "--days", "3", "--symbols", " btcusdt, ethusdt ,",
                                       "--end-date", "2026-09-18", "--limit", "500", "--dry-run"])
    assert a.days == 3 and a.symbols == ("BTCUSDT", "ETHUSDT") and a.end_date == date(2026, 9, 18)
    assert a.limit == 500 and a.dry_run is True


@pytest.mark.parametrize("argv", [
    ["backfill", "--days", "0"], ["backfill", "--days", "x"], ["backfill", "--symbols", ","],
    ["backfill", "--symbols", "BTC/USDT"], ["backfill", "--end-date", "18.09.2026"],
    ["backfill", "--limit", "1"], ["backfill", "--limit", "1501"],
    ["crosscheck", "--threshold-bps", "-1"], ["crosscheck", "--threshold-bps", "NaN"],
    ["verify-journal", "--run-id", "not-a-uuid"], ["calibrate", "--is-days", "0"], [], ["nope"],
    ["user"], ["user", "add", "--login", "a"], ["user", "add", "--role", "admin"],
    ["user", "add", "--login", "bad login", "--role", "admin"],
    ["user", "add", "--login", "a", "--role", "root"],
    ["user", "add", "--login", "-x", "--role", "admin"], ["user", "set-role", "--login", "a"],
    # пароля в argv не буває: такого прапорця немає (він був би видний у `ps` та історії оболонки)
    ["user", "add", "--login", "a", "--role", "admin", "--password", "s3cret-pass"],
    # і скорочення `--password-stdin` не приймаються (інакше `--password` мовчки вмикав би читання stdin)
    ["user", "add", "--login", "a", "--role", "admin", "--password"],
    ["user", "add", "--login", "a", "--role", "admin", "--pass"],
    ["user", "list", "--js"],
])
def test_parser_rejects_bad_arguments(argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as e:
        cli.build_parser().parse_args(argv)
    assert e.value.code == 2
    capsys.readouterr()


def test_every_subcommand_has_a_handler_and_parses() -> None:
    p = cli.build_parser()
    sub = next(a for a in p._actions if isinstance(a, argparse._SubParsersAction))
    assert set(sub.choices) == set(cli.HANDLERS) == {
        "backfill", "crosscheck", "fetch-funding", "calibrate", "replay-gap", "verify-journal", "db-stats",
        "user"}
    for name in set(cli.HANDLERS) - {"user"}:
        assert p.parse_args([name]).command == name
    a = p.parse_args(["user", "add", "--login", "olena", "--role", "Operator", "--password-stdin"])
    assert (a.command, a.user_command, a.login, a.role, a.password_stdin) == (
        "user", "add", "olena", Role.OPERATOR, True)
    assert p.parse_args(["user", "list", "--json"]).json is True
    assert p.parse_args(["user", "set-role", "--login", "olena", "--role", "admin"]).role is Role.ADMIN
    rid = "7d441046-5916-59d6-9ba7-3971dc3b1caa"
    assert str(p.parse_args(["verify-journal", "--run-id", rid]).run_id) == rid
    assert p.parse_args(["--database-url", "postgresql+asyncpg://x@h/db", "db-stats", "--json"]).json is True
    assert p.parse_args(["crosscheck"]).threshold_bps == Decimal(50)


def test_calibrate_is_days_default_comes_from_backtest_profile() -> None:
    from fuzzhelm.config import load_yaml  # noqa: PLC0415

    assert cli.build_parser().parse_args(["calibrate"]).is_days == \
        load_yaml("profiles/backtest")["walkforward"]["is_days"]


# ================================================================== вікно і план


def test_dataset_window_is_45_full_utc_days_ending_at_midnight() -> None:
    end = cli.utc_date_ms(date(2026, 9, 18))
    w = cli.DatasetWindow.ending_at(end, 45)
    assert cli.utc_iso(w.start_ms) == "2026-08-04T00:00:00Z"
    assert cli.utc_iso(w.end_ms) == "2026-09-18T00:00:00Z"
    assert w.days == 45 and w.bars_per_symbol == 45 * 1440 == 64_800
    assert cli.utc_iso(w.last_open_ms) == "2026-09-17T23:59:00Z"
    assert cli.DatasetWindow.from_dict(w.to_dict()) == w
    # перше IS-вікно (дні 1–15) закінчується там, де починається перше OOS
    lo, hi = w.sub_window(1, 15)
    assert lo == w.start_ms and hi == w.start_ms + 15 * DAY
    with pytest.raises(ValueError):
        w.sub_window(40, 10)
    with pytest.raises(ValueError):
        cli.DatasetWindow(w.start_ms + 1, w.end_ms)
    with pytest.raises(ValueError):
        cli.DatasetWindow(w.end_ms, w.start_ms)
    # «останній повний UTC-день»: кінець вікна = північ поточного дня за часом біржі
    now = end + 21 * 3_600_000 + 14 * 60_000 + 26_747
    assert cli.day_floor_ms(now) == end and cli.day_floor_ms(end) == end
    assert cli.utc_iso(now) == "2026-09-18T21:14:26.747000Z"


@pytest.mark.parametrize(("n", "limit", "pages"), [
    (1, 1500, 1), (1500, 1500, 1), (1501, 1500, 2), (2999, 1500, 2), (3000, 1500, 3), (64_800, 1500, 44),
    (10, 2, 9),
])
def test_pages_needed_counts_one_bar_overlap(n: int, limit: int, pages: int) -> None:
    assert cli.pages_needed(n, limit) == pages
    # перевірка незалежною симуляцією пагінації: перша сторінка limit, далі limit − 1 нових
    got, covered = 1, min(n, limit)
    while covered < n:
        covered += limit - 1
        got += 1
    assert got == pages


def test_plan_backfill_weight_uses_measured_klines_table() -> None:
    plan = cli.plan_backfill(cli.DatasetWindow.ending_at(cli.utc_date_ms(date(2026, 9, 18)), 45))
    assert plan.pages_per_symbol == 44 and plan.weight_per_symbol == 44 * 10
    assert plan.total_weight == 2 * 440 + 3 + 1 and plan.total_requests == 2 * 44 + 4
    assert plan.total_weight < 2400                                 # увесь добір — в одну хвилинну квоту
    small = cli.plan_backfill(cli.DatasetWindow.ending_at(cli.utc_date_ms(date(2026, 9, 18)), 1), limit=500)
    assert small.pages_per_symbol == 3 and small.weight_per_symbol == 3 * 2
    with pytest.raises(ValueError):
        cli.pages_needed(10, 1)


# ================================================================== dry-run: без мережі й БД


def test_backfill_dry_run_prints_plan_without_network_or_db(offline: None,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["backfill", "--dry-run", "--end-date", "2026-09-18"]) == 0
    out = capsys.readouterr().out
    assert "[2026-08-04T00:00:00Z, 2026-09-18T00:00:00Z)" in out and "64800 per symbol, 129600 total" in out
    assert "44 klines pages/symbol" in out and "= 884" in out


def test_other_dry_runs_touch_neither_network_nor_db(offline: None, tmp_path: Path,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    win = _window_json(tmp_path)
    assert cli.main(["crosscheck", "--dry-run"]) == 0
    assert "pair=XBTUSD" in capsys.readouterr().out
    assert cli.main(["fetch-funding", "--dry-run", "--window-json", str(win), "--symbols", "BTCUSDT"]) == 0
    out = capsys.readouterr().out
    assert "fundingRate symbol=BTCUSDT startTime=1785801600000 endTime=1789689599999" in out
    assert cli.main(["calibrate", "--dry-run", "--window-json", str(win)]) == 0
    assert "[2026-08-04T00:00:00Z, 2026-08-19T00:00:00Z) = 21600 bars (days 1–15)" in capsys.readouterr().out
    assert cli.main(["replay-gap", "--dry-run"]) == 0
    assert "journal run_id=" in capsys.readouterr().out


def test_missing_window_file_is_a_clean_cli_error(offline: None, tmp_path: Path,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["calibrate", "--dry-run", "--window-json", str(tmp_path / "none.json")]) == 2
    assert "run `fuzzhelm backfill` first" in capsys.readouterr().err


def test_replay_run_id_is_deterministic_per_session_file(tmp_path: Path) -> None:
    a = tmp_path / "a.jsonl.gz"
    a.write_bytes(b"x")
    b = tmp_path / "b.jsonl.gz"
    b.write_bytes(b"x")
    c = tmp_path / "c.jsonl.gz"
    c.write_bytes(b"y")
    assert cli.replay_run_id(a) == cli.replay_run_id(b) != cli.replay_run_id(c)
    assert cli.replay_run_id(a).version == 5


# ================================================================== крос-звірка зі збережених відповідей


def _raw_from_fixtures() -> dict[str, Any]:
    kraken = orjson.loads((REST / "kraken_ohlc.json").read_bytes())
    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))
    server_ms = orjson.loads((REST / "server_time.json").read_bytes())["serverTime"]
    return {"v": 1, "captured_utc": "2026-09-18T19:25:37Z",
            "kraken": {"pair": "XBTUSD", "interval": 1, "ts_ingest_ns": server_ms * 1_000_000,
                       "result": kraken["result"]},
            "binance": {"symbol": "BTCUSDT", "interval": "1m", "ts_ingest_ns": server_ms * 1_000_000,
                        "server_time_ms": server_ms, "rows": rows}}


def test_crosscheck_analysis_on_recorded_real_data_is_consistent_and_reproducible() -> None:
    raw = _raw_from_fixtures()
    a = cli.analyze_crosscheck(raw)
    s = a.stats
    assert s["matched"] == len(a.report.rows) > 700 and s["missing_in_binance"] == s["missing_in_kraken"] == 0
    d = sorted(r.diff_bps for r in a.report.rows)
    ad = sorted(abs(x) for x in d)
    assert s["mean_bps"] == a.report.mean_bps and s["max_abs_bps"] == ad[-1] == a.report.max_abs_bps
    assert s["p95_abs_bps"] == ad[int(np.ceil(0.95 * len(ad))) - 1]           # найближчий ранг
    assert s["median_abs_bps"] <= s["p95_abs_bps"] <= s["max_abs_bps"]
    assert s["count_above_threshold"] == len(a.report.flagged) == 0     # справжні дані: жодної > 50 б.п.
    assert a.decomposition["n"] == 0                                    # без індексу/USDT — без розкладу
    again = cli.analyze_crosscheck(orjson.loads(orjson.dumps(raw)))
    assert again.stats == s                                             # той самий вхід → той самий звіт


def test_crosscheck_analysis_flags_injected_divergence_and_decomposes_exactly() -> None:
    raw = _raw_from_fixtures()
    key = next(k for k in raw["kraken"]["result"] if k != "last")
    rows = raw["kraken"]["result"][key]
    victim = rows[100]
    victim[4] = str((Decimal(victim[4]) * Decimal("0.99")).quantize(Decimal("0.1")))   # застиглий тик −1 %
    victim[3] = min(victim[3], victim[4], key=Decimal)                                   # OHLC узгоджений
    # індекс і USDT/USD для розкладу: синтетичні, але з точно відомою відповіддю
    closes = {int(r[0]): r[4] for r in raw["binance"]["rows"]}
    raw["binance_index"] = {"rows": [[t, "0", "0", "0", str(Decimal(c) * Decimal("0.9995")), "0", 0, "0", 0,
                                      "0", "0", "0"] for t, c in closes.items()]}
    raw["kraken_usdtusd"] = {"result": {"USDTZUSD": [[int(r[0]), "1", "1", "1", "0.9990", "1", "1", 1]
                                                     for r in rows], "last": raw["kraken"]["result"]["last"]}}
    a = cli.analyze_crosscheck(raw)
    assert a.stats["count_above_threshold"] == 1 == len(a.report.flagged)
    assert a.report.flagged[0].open_time_ns == int(victim[0]) * 1_000_000_000
    dc = a.decomposition
    assert dc["n"] == a.stats["matched"] and dc["identity_max_err"] < 1e-9
    assert dc["perp_premium_bps"]["median"] == pytest.approx(-1e4 * np.log(0.9995), abs=1e-9)
    assert dc["usdt_discount_bps"]["median"] == pytest.approx(-1e4 * np.log(0.9990), abs=1e-9)


def test_committed_crosscheck_input_reproduces_its_report_invariants() -> None:
    path = ROOT / "data" / "crosscheck_input.json.gz"
    if not path.exists():
        pytest.skip("data/crosscheck_input.json.gz is produced by `fuzzhelm crosscheck`")
    raw = cli._load_raw(path)
    a = cli.analyze_crosscheck(raw)
    assert a.stats["matched"] == a.stats["kraken_closed"] == len(a.series["d_bps"])
    assert a.decomposition["n"] == a.stats["matched"] and a.decomposition["identity_max_err"] < 1e-9
    md = cli.crosscheck_report_md(a, raw, input_path="data/crosscheck_input.json.gz", command="x")
    assert f"| звірено хвилин (спільний open_time) | {a.stats['matched']} |" in md


def test_order_statistics_helpers() -> None:
    xs = [Decimal(x) for x in (1, 2, 3, 4)]
    assert cli._median(xs) == Decimal("2.5") and cli._median(xs[:3]) == 2
    assert cli._order_stat(xs, Decimal("0.95")) == 4 and cli._order_stat(xs, Decimal("0.5")) == 2
    assert cli._order_stat(xs, Decimal("0.01")) == 1


# ================================================================== funding: нормалізація, файл, хеш


ROW = {"symbol": "BTCUSDT", "fundingTime": 1785801600004, "fundingRate": "0.00003081",
       "markPrice": "63497.20000000", "rateType": "Regular"}


def test_funding_rate_normalization_is_strict_and_exact() -> None:
    fr = normalize_funding_rate(ROW, BTC_USDT_PERP)
    assert fr == FundingRate("BTC-USDT-PERP", Venue.BINANCE_USDM, 1785801600004 * 1_000_000,
                             Decimal("0.00003081"), Decimal("63497.20000000"), "Regular")
    assert fr.funding_time_ms == 1785801600004 and str(fr.funding_rate) == "0.00003081"   # масштаб збережено
    assert normalize_funding_rate(ROW | {"markPrice": ""}, BTC_USDT_PERP).mark_price is None
    no_type = {k: v for k, v in ROW.items() if k != "rateType"}
    assert normalize_funding_rate(no_type, BTC_USDT_PERP).rate_type is None
    assert normalize_funding_rate(ROW | {"fundingRate": "-0.00010810"}, BTC_USDT_PERP).funding_rate < 0
    bad = [
        (ROW | {"extra": 1}, "extra"), ({k: v for k, v in ROW.items() if k != "fundingRate"}, "fundingRate"),
        (ROW | {"fundingRate": 0.0001}, "fundingRate"), (ROW | {"fundingRate": "1e-4"}, "fundingRate"),
        (ROW | {"fundingTime": "1785801600004"}, "fundingTime"), (ROW | {"fundingTime": True}, "fundingTime"),
        (ROW | {"markPrice": "0"}, "markPrice"), (ROW | {"symbol": "ETHUSDT"}, "symbol"),
        (ROW | {"rateType": 1}, "rateType"),
    ]
    for row, field in bad:
        with pytest.raises(NormalizationError) as e:
            normalize_funding_rate(row, BTC_USDT_PERP)
        assert e.value.field == field and e.value.venue == "BINANCE_USDM"


def test_funding_batch_sorts_dedups_and_reports_row_index() -> None:
    r2 = ROW | {"fundingTime": ROW["fundingTime"] + 8 * 3_600_000 - 4}
    out = normalize_funding_rates([r2, ROW, ROW], BTC_USDT_PERP)
    assert [x.funding_time_ms for x in out] == [ROW["fundingTime"], r2["fundingTime"]]
    with pytest.raises(NormalizationError) as e:
        normalize_funding_rates([ROW, ROW | {"fundingRate": "0.5"}], BTC_USDT_PERP)
    assert e.value.field == "[1].fundingTime"
    with pytest.raises(NormalizationError) as e:
        normalize_funding_rates([ROW, ROW | {"x": 1}], BTC_USDT_PERP)
    assert e.value.field == "[1].x"


def _rates(n: int = 5) -> list[FundingRate]:
    return normalize_funding_rates([ROW | {"fundingTime": ROW["fundingTime"] + i * 28_800_000,
                                           "fundingRate": f"0.0000{i}081"} for i in range(n)], BTC_USDT_PERP)


def test_funding_file_roundtrip_digest_and_dataset_hash_columns(tmp_path: Path) -> None:
    rates = _rates()
    doc = funding_document(rates, BTC_USDT_PERP, base_url=BASE, window={"days": 45},
                           fetched_at_utc="2026-09-18T21:16:13Z", requests=1)
    assert doc["source"] == f"GET {BASE}/fapi/v1/fundingRate" and doc["count"] == 5
    path = write_funding_json(tmp_path / "funding_BTCUSDT.json", doc)
    series = load_funding_json(path, BTC_USDT_PERP)
    assert series.rates == tuple(rates) and series.meta["rows_digest"] == doc["rows_digest"]
    with pytest.raises(ValueError, match="symbol"):
        load_funding_json(path, ETH_USDT_PERP)
    tampered = orjson.loads(path.read_bytes())
    tampered["rows"][2][1] = "0.1"
    (tmp_path / "t.json").write_bytes(orjson.dumps(tampered))
    with pytest.raises(ValueError, match="rows_digest"):
        load_funding_json(tmp_path / "t.json", BTC_USDT_PERP)
    cols = funding_columns(rates)
    assert cols["funding_t_ns"].dtype == np.int64 and cols["funding_rate"].dtype == np.float64
    assert cols["funding_rate"][1] == float("0.00001081")
    base = {"t_ns": np.arange(3, dtype=np.int64), "c": np.array([1.0, 2.0, 3.0])}
    h_with = dataset_hash(base | cols)
    last = FundingRate(**{**_asdict(rates[-1]), "funding_rate": Decimal("0.9")})
    changed = funding_columns([*rates[:-1], last])
    assert h_with != dataset_hash(base) and h_with != dataset_hash(base | changed)   # funding входить у хеш


def _asdict(fr: FundingRate) -> dict[str, Any]:
    return {k: getattr(fr, k) for k in FundingRate.__slots__}


@pytest.mark.parametrize("name", ["BTCUSDT", "ETHUSDT"])
def test_committed_funding_files_are_valid_and_cover_the_dataset_window(name: str) -> None:
    path = ROOT / "data" / f"funding_{name}.json"
    win = ROOT / "data" / "dataset_window.json"
    if not (path.exists() and win.exists()):
        pytest.skip("data/ is produced by `fuzzhelm backfill` + `fuzzhelm fetch-funding`")
    ref = BTC_USDT_PERP if name == "BTCUSDT" else ETH_USDT_PERP
    series = load_funding_json(path, ref)
    w, _ = cli.load_window(win)
    ts = [r.funding_time_ms for r in series.rates]
    assert all(w.start_ms <= t < w.end_ms for t in ts) and ts == sorted(ts)
    # ставки кожні 8 год (00/08/16 UTC) з мілісекундним «хвостом» біржі
    assert all(t % (8 * 3_600_000) < 1000 for t in ts) and len(ts) == 3 * w.days


# ================================================================== REST хвилі 2 (respx, без мережі)


def _client(http: httpx.AsyncClient) -> BinanceRestClient:
    clock = ManualClock(1_789_765_359_000_000_000)
    return BinanceRestClient(BASE, http, binance_request_bucket(clock, sleep=_no_sleep),
                             RetryPolicy(rng_seed=0), clock=clock, sleep=_no_sleep)


async def test_funding_history_paginates_by_time_and_filters_window() -> None:
    t0 = 1785801600004
    rows = [ROW | {"fundingTime": t0 + i * 28_800_000} for i in range(5)]
    calls: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        q = dict(request.url.params)
        calls.append(q)
        start, lim = int(q["startTime"]), int(q["limit"])
        page = [r for r in rows if start <= r["fundingTime"] <= int(q["endTime"])][:lim]
        return httpx.Response(200, json=page)

    async with respx.mock(assert_all_called=True) as router:
        router.get(f"{BASE}/fapi/v1/fundingRate").mock(side_effect=handler)
        async with httpx.AsyncClient() as http:
            client = _client(http)
            rates, n = await fetch_funding_history(client, BTC_USDT_PERP, t0 - 4, t0 + 3 * 28_800_000,
                                                   limit=2)
    assert [r.funding_time_ms for r in rates] == [t0 + i * 28_800_000 for i in range(4)]
    # 2 повні сторінки; після другої курсор (t₃ + 1) уже за межею вікна — третього запиту немає
    assert n == 2 and calls[1]["startTime"] == str(t0 + 28_800_000 + 1)
    assert client.bucket.acquired_weight == n * request_weight("/fapi/v1/fundingRate")
    with pytest.raises(ValueError):
        await fetch_funding_history(client, BTC_USDT_PERP, 10, 5)


async def test_agg_trades_and_index_klines_endpoints_charge_measured_weights() -> None:
    assert request_weight("/fapi/v1/aggTrades", {"fromId": 1, "limit": 1000}) == 20
    assert request_weight("/fapi/v1/indexPriceKlines", {"limit": 1000}) == 5
    assert request_weight("/fapi/v1/indexPriceKlines", {"limit": 2}) == 1
    trade = {"a": 7, "p": "81118.60", "q": "0.001", "nq": "0.001", "f": 10, "l": 10, "T": 1789765359306,
             "m": False}
    async with respx.mock(assert_all_called=True) as router:
        agg = router.get(f"{BASE}/fapi/v1/aggTrades").mock(return_value=httpx.Response(200, json=[trade]))
        idx = router.get(f"{BASE}/fapi/v1/indexPriceKlines").mock(return_value=httpx.Response(200, json=[]))
        async with httpx.AsyncClient() as http:
            client = _client(http)
            assert await client.agg_trades("BTCUSDT", from_id=7, limit=1000) == [trade]
            assert await client.index_price_klines("BTCUSDT", start_ms=1, end_ms=2, limit=1000) == []
    assert dict(agg.calls[0].request.url.params) == {"symbol": "BTCUSDT", "fromId": "7", "limit": "1000"}
    assert dict(idx.calls[0].request.url.params)["pair"] == "BTCUSDT"
    assert client.bucket.acquired_weight == 20 + 5 and client.requests_sent == 2
    with pytest.raises(ValueError):
        await client.agg_trades("BTCUSDT", limit=1001)
    with pytest.raises(ValueError):
        await client.funding_rate_history("BTCUSDT", limit=0)


async def test_http_log_records_used_weight_header() -> None:
    log = cli.HttpLog()
    req = httpx.Request("GET", f"{BASE}/fapi/v1/time")
    await log.on_response(httpx.Response(200, headers={"X-MBX-USED-WEIGHT-1M": "22"}, request=req))
    await log.on_response(httpx.Response(200, headers=[(b"X-MBX-USED-WEIGHT-1M", b"\xb2")], request=req))
    await log.on_response(httpx.Response(429, request=req))
    assert log.by_path() == {"/fapi/v1/time": 3} and log.statuses() == {200: 2, 429: 1}
    assert log.max_used_weight() == 22


# ================================================================== хвиля 2: добір → ingest_gap, звіти, хеш


class _FakeKlines:
    """KlineSource без мережі: віддає рядки fixtures/rest (справжні klines Binance) за [start, end]."""

    def __init__(self, rows: list[list[Any]]) -> None:
        self.rows = rows
        self.clock = ManualClock(rows[-1][6] * 1_000_000)
        self.calls = 0

    async def klines(self, symbol: str, interval: str = "1m", start_ms: int | None = None,
                     end_ms: int | None = None, limit: int = 1500) -> list[list[Any]]:
        self.calls += 1
        lo = -1 if start_ms is None else start_ms
        hi = 2**62 if end_ms is None else end_ms
        return [r for r in self.rows if lo <= r[0] <= hi][:limit]


async def test_backfill_gaps_become_ingest_gap_rows_and_end_filled(monkeypatch: pytest.MonkeyPatch) -> None:
    """Шлях, якого справжній 45-денний прогін не пройшов (біржа віддала все): прогалина добору → рядок
    ingest_gap OPEN → FILLING → кінцевий статус, добрані бари — upsert у той самий instrument_id."""
    from contextlib import asynccontextmanager  # noqa: PLC0415
    from types import SimpleNamespace  # noqa: PLC0415

    import fuzzhelm.storage.repositories as repos  # noqa: PLC0415
    import fuzzhelm.storage.session as sess  # noqa: PLC0415
    from fuzzhelm.core.enums import GapStatus  # noqa: PLC0415
    from fuzzhelm.ingest.backfill import backfill_klines, fill_gaps  # noqa: PLC0415

    rows = orjson.loads(gzip.decompress((REST / "binance_klines.json.gz").read_bytes()))[:300]
    hole = [r[0] for r in rows[100:103]]
    now_ms = rows[-1][6] + 1
    res = await backfill_klines(_FakeKlines([r for r in rows if r[0] not in hole]), "BTCUSDT", rows[0][0],
                                rows[-1][0], limit=100, instrument=BTC_USDT_PERP, now_ms=now_ms)
    assert [(g.ts_lo_ns // 1_000_000, g.ts_hi_ns // 1_000_000, g.expected_count) for g in res.gaps] == \
        [(hole[0], hole[-1], 3)]
    events: list[tuple[Any, ...]] = []

    class FakeGapRepo:
        def __init__(self, _: Any) -> None: ...

        async def open(self, iid: int, stream: Any, lo: int, hi: int, **kw: Any) -> int:
            events.append(("open", iid, stream, lo, hi, kw["expected_count"], kw["detected_at_ns"]))
            return 41

        async def update_status(self, gid: int, status: GapStatus, **kw: Any) -> Any:
            events.append(("status", gid, GapStatus(status).value, kw.get("filled_rows"),
                           kw.get("count_attempt")))
            return SimpleNamespace(status=GapStatus(status).value)

    class FakeCandleRepo:
        def __init__(self, _: Any) -> None: ...

        async def upsert(self, candles: list[Any], iid: int) -> None:
            events.append(("upsert", iid, [c.open_time_ns // 1_000_000 for c in candles]))

    @asynccontextmanager
    async def fake_scope(_: Any) -> Any:
        yield object()

    monkeypatch.setattr(repos, "GapRepo", FakeGapRepo)
    monkeypatch.setattr(repos, "CandleRepo", FakeCandleRepo)
    monkeypatch.setattr(sess, "session_scope", fake_scope)
    full = _FakeKlines(rows)
    out = await cli._record_backfill_gaps(factory=None, fill_gaps=fill_gaps, client=full,  # type: ignore[arg-type]
                                          symbol="BTCUSDT", instrument=BTC_USDT_PERP,  # type: ignore[arg-type]
                                          instrument_id=7, result=res, now_ms=now_ms, now_ns=123)
    assert out == [{"id": 41, "status": "FILLED", "expected_count": 3, "filled_rows": 3,
                    "ts_lo_utc": cli.utc_iso(hole[0]), "ts_hi_utc": cli.utc_iso(hole[-1])}]
    assert events == [
        ("open", 7, res.gaps[0].stream, res.gaps[0].ts_lo_ns, res.gaps[0].ts_hi_ns, 3, 123),
        ("status", 41, "FILLING", None, True),
        ("upsert", 7, hole),
        ("status", 41, "FILLED", 3, False),
    ]
    # без прогалин — жодного звернення до БД
    events.clear()
    clean = await backfill_klines(full, "BTCUSDT", rows[0][0], rows[-1][0], limit=100,
                                  instrument=BTC_USDT_PERP, now_ms=now_ms)
    assert await cli._record_backfill_gaps(factory=None, fill_gaps=fill_gaps, client=full,  # type: ignore[arg-type]
                                           symbol="BTCUSDT", instrument=BTC_USDT_PERP,  # type: ignore[arg-type]
                                           instrument_id=7, result=clean, now_ms=now_ms, now_ns=1) == []
    assert events == []


def _stats(*, mismatches: int = 0, gaps: list[dict[str, Any]] | None = None,
           explicit: bool = False) -> dict[str, Any]:
    w = cli.DatasetWindow.ending_at(cli.utc_date_ms(date(2026, 9, 18)), 45, ("BTCUSDT",))
    sym = {"symbol_canon": "BTC-USDT-PERP", "rows_fetched": 64800, "inserted": 64800, "updated": 0,
           "skipped": 0, "requests": 44, "weight_charged": 440, "elapsed_s": 19.4,
           "seams": {"first": 1, "OK": 43},
           "overlap_mismatches": mismatches, "gaps_detected": [], "gap_rows": gaps or [],
           "rows_in_db_window": 64800, "db_gaps_in_window": 0, "first_open_utc": "2026-08-04T00:00:00Z",
           "last_open_utc": "2026-09-17T23:59:00Z", "dataset_hash": "ab" * 32,
           "dataset_hash_columns": ["c", "h", "l", "o", "t_ns", "v"], "tick_size": "0.10",
           "step_size": "0.001",
           "min_notional": "50"}
    return {"window": w.to_dict(), "clock_offset_ms": 84, "exchange_now_utc": "2026-09-18T21:14:26.747000Z",
            "run_started_utc": "2026-09-18T21:14:26Z", "run_finished_utc": "2026-09-18T21:15:06Z",
            "end_date_explicit": explicit, "elapsed_total_s": 40.8,
            "plan": {"requests": 48, "weight": 444, "pages_per_symbol": 44, "limit": 1500},
            "http": {"by_path": {"/fapi/v1/klines": 44}, "statuses": {"200": 44},
                     "max_used_weight_1m_header": 441,
                     "weight_charged_total": 444, "bucket_wait_s": 0.0, "retry_sleeps": 0},
            "symbols": {"BTCUSDT": sym}, "db": {"candle_total": 64800, "ingest_gap_by_status": {}},
            "command": "uv run fuzzhelm backfill"}


def test_backfill_report_states_seams_gaps_and_times_honestly() -> None:
    md = cli.backfill_report_md(_stats())
    assert "запуск 2026-09-18T21:14:26Z, завершення 2026-09-18T21:15:06Z" in md
    assert "MISMATCH = 0" in md and "останнім повним UTC-днем перед запуском" in md and "**виконано**" in md
    md = cli.backfill_report_md(_stats(mismatches=2, explicit=True))
    assert "MISMATCH = 0" not in md and "2 розбіжностей вмісту" in md and "заданою `--end-date`" in md
    gap = {"id": 5, "status": "PARTIAL", "expected_count": 3, "filled_rows": 1,
           "ts_lo_utc": "2026-08-05T00:00:00Z", "ts_hi_utc": "2026-08-05T00:02:00Z"}
    md = cli.backfill_report_md(_stats(gaps=[gap]))
    assert "| BTCUSDT | 5 | 2026-08-05T00:00:00Z | 2026-08-05T00:02:00Z | 3 | 1 | PARTIAL |" in md
    doc = cli.window_document(cli.DatasetWindow.from_dict(_stats()["window"]), _stats(explicit=True))
    assert "explicit --end-date" in doc["definition"] and doc["created_utc"] == "2026-09-18T21:15:06Z"
    assert doc["first_is_window"]["days"] == [1, 15]
    assert doc["first_is_window"]["end_ms"] - doc["first_is_window"]["start_ms"] == 15 * DAY


def test_replay_report_marks_gap_timestamps_as_replay_time() -> None:
    """detected_at = closed_at у ingest_gap після реплею — віртуальний час кадрів; звіт мусить це казати,
    а не створювати враження «добір за 0 мс» у настінному часі."""
    gap = {"id": 1, "stream": "klines", "detector": "time", "status": "FILLED",
           "ts_lo_utc": "2026-09-18T19:09:00Z", "ts_hi_utc": "2026-09-18T19:09:00Z", "expected_count": 1,
           "filled_rows": 1, "attempts": 1, "detected_utc": "2026-09-18T19:10:02.121000Z",
           "closed_utc": "2026-09-18T19:10:02.121000Z"}
    res = {"session": "s.jsonl.gz", "session_sha256": "ab" * 32, "reference": "r.jsonl.gz", "run_id": "x",
           "journal_entries": 3, "journal_head": "cd" * 32, "journal_verify_bad_seq": None, "elapsed_s": 2.3,
           "candles_released": 3, "trades_emitted": 5, "candle_upsert": {}, "backfill_requests": 1,
           "backfill_errors": [], "http": {"by_path": {"/fapi/v1/klines": 1}, "statuses": {"200": 1},
                                           "used_weight_headers": []},
           "gap_rows": [gap], "transitions": [(1, "OPEN"), (1, "FILLED")], "gap_stats_db": {"FILLED": 1},
           "recovery": {"lost_candles": 0, "expected_candles": 3, "lost_trades": 0, "expected_trades": 5,
                        "duplicated_candles": 0, "duplicated_trades": 0, "mismatched_candles": 0,
                        "zero_loss": True},
           "invalid": 0, "disconnects": 0, "run_utc": "2026-09-18T21:16:48Z"}
    md = cli.replay_report_md(res)
    assert "| виявлено (відтвор. час) | закрито (відтвор. час) | статус |" in md
    assert "віртуальний час кадрів реплею" in md and "(2026-09-18T21:16:48Z)" in md


def test_calibrate_trim_end_bars_shrinks_the_is_window(offline: None, tmp_path: Path,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    win = _window_json(tmp_path)
    assert cli.main(["calibrate", "--dry-run", "--window-json", str(win), "--trim-end-bars", "1046"]) == 0
    out = capsys.readouterr().out
    assert f"= {21600 - 1046} bars" in out and "2026-08-18T06:34:00Z" in out
    assert cli.main(["calibrate", "--dry-run", "--window-json", str(win), "--trim-end-bars", "21600"]) == 2
    assert "leaves no bars" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["calibrate", "--trim-end-bars", "-1"])
    capsys.readouterr()


def test_funding_columns_enter_the_backtest_dataset_hash() -> None:
    """data/funding_*.json → ingest.funding.funding_columns → backtest.dataset.Dataset: інша ставка — інший
    dataset_hash (так рушій бектесту фіксує фандинг у паспорті прогону)."""
    from fuzzhelm.backtest.dataset import load_fixture_dataset  # noqa: PLC0415

    ds = load_fixture_dataset()
    base = {k: getattr(ds, k) for k in ("t_ns", "o", "h", "l", "c", "v", "qv", "n")}
    cols = funding_columns(_rates())
    with_f = type(ds).from_arrays(ds.instrument, **base, funding_t_ns=cols["funding_t_ns"],
                                  funding_rate=cols["funding_rate"])
    assert set(with_f.columns()) == set(ds.columns()) | {"funding_t_ns", "funding_rate"}
    assert with_f.dataset_hash == dataset_hash({**ds.columns(), **cols}) != ds.dataset_hash
    last = FundingRate(**{**_asdict(_rates()[-1]), "funding_rate": Decimal("0.0005")})
    cols2 = funding_columns([*_rates()[:-1], last])
    other = type(ds).from_arrays(ds.instrument, **base, funding_t_ns=cols2["funding_t_ns"],
                                 funding_rate=cols2["funding_rate"])
    assert other.dataset_hash != with_f.dataset_hash


# ================================================================== user (без БД: репозиторії в пам'яті)

FAST_BCRYPT = CryptContext(schemes=["bcrypt"], bcrypt__rounds=4)   # швидкий bcrypt лише для тестів
PASSWORD = "Str0ng-pass-ph4se"


class _MemUsers:
    def __init__(self) -> None:
        self.rows: dict[int, UserRow] = {}

    async def create(self, login: str, password: str, role: Role | str) -> UserRow:
        row = UserRow(id=len(self.rows) + 1, login=login, pwd_hash=hash_password(password, FAST_BCRYPT),
                      role=Role(role).value, created_at_ns=1_789_776_000 * 10**9)
        self.rows[row.id] = row
        return row

    async def get_by_login(self, login: str) -> UserRow | None:
        return next((u for u in self.rows.values() if u.login == login), None)

    async def set_role(self, user_id: int, role: Role | str) -> None:
        self.rows[user_id] = replace(self.rows[user_id], role=Role(role).value)

    async def admins_for_update(self) -> list[UserRow]:
        return [self.rows[k] for k in sorted(self.rows) if self.rows[k].role == Role.ADMIN.value]

    async def list(self) -> list[UserRow]:
        return [self.rows[k] for k in sorted(self.rows)]


class _MemAudit:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def append(self, action: str, target: str, *, before: Mapping[str, Any] | None = None,
                     after: Mapping[str, Any] | None = None, user_id: int | None = None,
                     ip: str | None = None, ts_ns: int | None = None) -> int:
        self.rows.append({"id": len(self.rows) + 1, "action": action, "target": target,
                          "before": None if before is None else dict(before),
                          "after": None if after is None else dict(after), "user_id": user_id, "ip": ip})
        return len(self.rows)


class UserDb:
    """Сховище користувачів і аудиту з транзакційною семантикою: виняток усередині uow — відкат обох."""

    def __init__(self) -> None:
        self.users = _MemUsers()
        self.audit = _MemAudit()
        self.opened = 0
        self.closed = 0

    def stores(self, _args: argparse.Namespace) -> tuple[cli.UserUow, Any]:
        self.opened += 1

        @contextlib.asynccontextmanager
        async def uow() -> AsyncIterator[cli.UserStores]:
            snap = (copy.deepcopy(self.users.rows), copy.deepcopy(self.audit.rows))
            try:
                yield cli.UserStores(self.users, self.audit)
            except BaseException:
                self.users.rows, self.audit.rows = snap
                raise

        async def close() -> None:
            self.closed += 1

        return uow, close

    def actions(self) -> list[str]:
        return [r["action"] for r in self.audit.rows]


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def user_db(monkeypatch: pytest.MonkeyPatch) -> UserDb:
    db = UserDb()
    monkeypatch.setattr(cli, "_db_user_stores", db.stores)
    return db


@pytest.fixture
def no_getpass(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: Any, **__: Any) -> str:
        raise AssertionError("getpass must not be called")

    monkeypatch.setattr(getpass, "getpass", boom)


def _add(login: str, role: str, password: str, monkeypatch: pytest.MonkeyPatch) -> int:
    monkeypatch.setattr(sys, "stdin", io.StringIO(password + "\n"))
    return cli.main(["user", "add", "--login", login, "--role", role, "--password-stdin"])


def test_user_add_reads_password_from_stdin_never_echoes_or_logs(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    assert _add("olena", "admin", PASSWORD, monkeypatch) == 0
    out, err = capsys.readouterr()
    assert "created user id=1 login='olena' role=admin (audit_log #1)" in out
    (user,) = user_db.users.rows.values()
    assert user.pwd_hash is not None and user.pwd_hash.startswith("$2b$04$")
    assert verify_password(PASSWORD, user.pwd_hash, FAST_BCRYPT)            # збережено лише bcrypt-хеш
    audit_text = json.dumps(user_db.audit.rows, ensure_ascii=False)
    for text in (out, err, caplog.text, audit_text):
        assert PASSWORD not in text and user.pwd_hash not in text
    (rec,) = user_db.audit.rows
    assert (rec["action"], rec["target"], rec["user_id"], rec["before"]) == (
        "user.create", "user/olena", None, None)
    assert rec["after"]["role"] == "admin" and rec["after"]["id"] == 1
    assert rec["after"]["_actor"]["login"] == "cli" and rec["after"]["_actor"]["role"] is None
    assert user_db.opened == user_db.closed == 1                             # engine закрито


def test_user_add_prompts_twice_without_echo_and_rejects_mismatch(
        user_db: UserDb, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    answers: Iterator[str] = iter([PASSWORD, PASSWORD, PASSWORD, PASSWORD + "x"])
    prompts: list[str] = []

    def fake_getpass(prompt: str = "Password: ") -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr(getpass, "getpass", fake_getpass)
    monkeypatch.setattr(sys, "stdin", _Tty(""))
    assert cli.main(["user", "add", "--login", "ivan", "--role", "analyst"]) == 0
    assert prompts == ["Password: ", "Repeat password: "]
    assert cli.main(["user", "add", "--login", "petro", "--role", "analyst"]) == 2
    out, err = capsys.readouterr()
    assert "passwords do not match" in err and PASSWORD not in out + err
    assert [u.login for u in user_db.users.rows.values()] == ["ivan"] and user_db.actions() == ["user.create"]
    assert user_db.opened == 1                  # невдалий ввід пароля не відкриває з'єднання з БД


def test_user_add_without_terminal_requires_password_stdin(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(PASSWORD + "\n"))       # pipe, але без --password-stdin
    assert cli.main(["user", "add", "--login", "ivan", "--role", "analyst"]) == 2
    out, err = capsys.readouterr()
    assert "--password-stdin" in err and PASSWORD not in out + err
    assert not user_db.users.rows and user_db.opened == 0


def test_user_add_password_stdin_on_terminal_is_refused_not_echoed(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """`--password-stdin`, коли stdin — термінал: readline() показав би набраний пароль на екрані, тож
    команда відмовляє (exit 2) і нічого не читає зі stdin, не відкриває БД."""
    tty = _Tty(PASSWORD + "\n")
    monkeypatch.setattr(sys, "stdin", tty)
    assert cli.main(["user", "add", "--login", "ivan", "--role", "analyst", "--password-stdin"]) == 2
    out, err = capsys.readouterr()
    assert "expects a pipe" in err and PASSWORD not in out + err
    assert tty.tell() == 0                                  # зі stdin не прочитано жодного символу
    assert not user_db.users.rows and not user_db.audit.rows and user_db.opened == 0


@pytest.mark.parametrize("bad", [
    "", "short7!", "x" * 73, "пароль-ю" * 5, "with\ttab-1234", "Olena-Login",
])
def test_user_add_rejects_weak_or_unhashable_passwords(
        bad: str, user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Порожній, < 8 символів, > 72 байтів UTF-8 (bcrypt обрізав би; кирилиця — 2 байти на літеру), з
    керівними символами, рівний логіну — відхиляються до підключення до БД; сам пароль у повідомлення
    не потрапляє."""
    assert _add("olena-login", "operator", bad, monkeypatch) == 2
    out, err = capsys.readouterr()
    assert err.startswith("fuzzhelm user: password") and (not bad or bad not in out + err)
    assert not user_db.users.rows and not user_db.audit.rows and user_db.opened == 0


def test_user_add_duplicate_login_is_a_clean_error(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    assert _add("olena", "operator", PASSWORD, monkeypatch) == 0
    assert _add("olena", "admin", PASSWORD + "-2", monkeypatch) == 2
    assert "already exists" in capsys.readouterr().err
    assert [u.role for u in user_db.users.rows.values()] == ["operator"]
    assert user_db.actions() == ["user.create"]


def test_user_set_role_audits_before_after_and_keeps_last_admin(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    assert _add("root", "admin", PASSWORD, monkeypatch) == 0
    assert _add("ann", "analyst", PASSWORD, monkeypatch) == 0
    assert cli.main(["user", "set-role", "--login", "ann", "--role", "operator"]) == 0
    rec = user_db.audit.rows[-1]
    assert (rec["action"], rec["target"]) == ("user.set_role", "user/ann")
    assert rec["before"] == {"id": 2, "login": "ann", "role": "analyst"}
    assert rec["after"]["role"] == "operator" and rec["after"]["_actor"]["login"] == "cli"
    assert "role analyst -> operator" in capsys.readouterr().out
    # та сама роль — нічого не змінюється і не пишеться
    assert cli.main(["user", "set-role", "--login", "ann", "--role", "operator"]) == 0
    assert "nothing changed" in capsys.readouterr().out and len(user_db.audit.rows) == 3
    # останнього адміністратора понизити не можна (інакше ліміти й kill-switch стали б недоступні)
    assert cli.main(["user", "set-role", "--login", "root", "--role", "analyst"]) == 2
    assert "last admin" in capsys.readouterr().err and user_db.users.rows[1].role == "admin"
    assert cli.main(["user", "set-role", "--login", "ann", "--role", "admin"]) == 0
    assert cli.main(["user", "set-role", "--login", "root", "--role", "auditor"]) == 0
    assert user_db.users.rows[1].role == "auditor"
    assert cli.main(["user", "set-role", "--login", "ghost", "--role", "admin"]) == 2
    assert "no user 'ghost'" in capsys.readouterr().err
    assert user_db.actions() == ["user.create"] * 2 + ["user.set_role"] * 3


def test_user_set_role_rechecks_target_after_locking_admins(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    """Поки set-role чекав на блокування рядків admin, інша транзакція вже понизила цього ж користувача:
    після блокування ціль перевіряється ще раз — exit 2 без зміни і без запису аудиту."""
    assert _add("root", "admin", PASSWORD, monkeypatch) == 0
    assert _add("second", "admin", PASSWORD, monkeypatch) == 0
    real = user_db.users.admins_for_update

    async def after_concurrent_demotion() -> list[UserRow]:
        user_db.users.rows[2] = replace(user_db.users.rows[2], role=Role.ANALYST.value)
        return await real()

    monkeypatch.setattr(user_db.users, "admins_for_update", after_concurrent_demotion)
    capsys.readouterr()
    assert cli.main(["user", "set-role", "--login", "second", "--role", "auditor"]) == 2
    assert "changed concurrently" in capsys.readouterr().err
    assert user_db.actions() == ["user.create", "user.create"]


def test_user_list_shows_no_hashes_and_is_audited(
        user_db: UserDb, no_getpass: None, monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str]) -> None:
    assert _add("root", "admin", PASSWORD, monkeypatch) == 0
    assert _add("ann", "auditor", PASSWORD, monkeypatch) == 0
    capsys.readouterr()
    assert cli.main(["user", "list"]) == 0
    out = capsys.readouterr().out
    assert "root" in out and "auditor" in out and "2 user(s)" in out and "$2b$" not in out
    assert cli.main(["user", "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [(r["id"], r["login"], r["role"]) for r in rows] == [(1, "root", "admin"), (2, "ann", "auditor")]
    assert all(set(r) == {"id", "login", "role", "created_at"} for r in rows)
    assert rows[0]["created_at"] == "2026-09-19T00:00:00Z"
    assert user_db.actions()[-2:] == ["user.list", "user.list"]
    assert user_db.audit.rows[-1]["after"]["count"] == 2
