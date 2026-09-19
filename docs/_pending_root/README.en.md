# FuzzHelm

> Draft of the root `README.en.md`. It stays in `docs/_pending_root/` until the phase-7 experiments finish, and the lead
> developer moves it. Ukrainian version: [`README.md`](README.md).

A service that aggregates exchange market data and automates margin trading operations. Decisions come from a fuzzy-logic
core. A hierarchical subsystem controls risks and limits automatically.

> **The soft part proposes, the hard part decides.** The fuzzy core only emits an intent `u ∈ [−1; 1]`. The deterministic
> risk loop has no technical way to increase exposure. This is a property of the verdict algebra, checked by a property-based
> test (`test_risk_chain_never_increases_exposure`).

Design-and-technology internship, Department of Automated Control Systems (ACS), Institute of Computer Science and Information
Technologies, Lviv Polytechnic National University, 2026. Author: Andrii Zhuk. Version 0.1.0.

## Safety and disclaimer

- **Public read-only market data and paper/testnet execution only.** Real funds and mainnet cannot be reached by construction.
  `fuzzhelm.config` enforces a hard host allowlist. Orders may go only to `testnet.binancefuture.com` / `demo-fapi.binance.com`,
  and market data only to public Binance and Kraken hosts. Any other host stops start-up (`MainnetHostRejected`, test
  `test_mainnet_host_is_rejected_by_config`). The default executor of the trading loop is the `PaperBroker` simulator.
- **This is not investment advice.** The work is about the method and a verifiable test bench, not about profitability. The
  default strategy loses money on the 45-day window, and this is reported as is (`docs/deviations.md` §2, `docs/journal.md`).
- Secrets live only in `.env`, which is git-ignored (the `.env.example` template contains no real values). User accounts,
  testnet keys and the Telegram token are created by a human. The coding agent never enters credentials.

## Architecture in brief

```
REST (Binance klines/exchangeInfo/fundingRate, Kraken OHLC) ─┐
WS Binance: /market (kline, aggTrade, markPrice) + /public (depth20) ─┴→ IngestPipeline: normalisation (Decimal),
   invariants, de-duplication, gap detection → REST backfill, data-quality score Q (AHP), MLP anomalies (optional scorer, not wired in the workers) → PostgreSQL + hash-chained journal
 → FeaturePipeline (incremental) → 6 detectors (s, c) → consensus T, R, V → Mamdani (45 rules as data) → κ → u_final
 → Schmitt trigger → sizer (vol target, ATR risk, leverage) → RiskGuard (6 limits + mode gate) → NORMAL/WARNING/COOLDOWN/HALTED machine
 → OrderRouter → PaperBroker | BinanceTestnetVenue → Portfolio → run passport, /explain, SSE
```

- **Determinism boundary:** the packages `core, features, detectors, fuzzy, decision, risk, sizing` read no wall clock and use no
  randomness (enforced by an AST test). Because of that, the same `TradingLoop.step()` runs in backtests, in live/replay mode
  and in grid cells.
- **Type boundary:** money, prices and quantities are only `Decimal`/`NUMERIC(38,18)`. Conversion to and from float happens in
  exactly two places (`features/convert.py`, `sizing/convert.py`).
- **Reproducibility:** every run carries a passport (`config_hash`, `dataset_hash`, `git_sha`, `seed`, `engine`,
  `journal_head_hash`, `equity_hash`).
- Diagrams: [`docs/diagrams/`](../diagrams/) (components, deployment, risk statechart, sequences, ER, classes, activity).

Stack: Python 3.12 (`uv`), FastAPI, Pydantic 2, SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 16 (no extensions), numpy,
scikit-learn, websockets, httpx, PyJWT, APScheduler, Docker Compose. Exact versions are pinned in `uv.lock`.

## Quick start

Keep the repository **outside** iCloud (e.g. `~/dev/fuzzhelm`), because synchronisation corrupts the PostgreSQL volume.

```bash
cp .env.example .env                      # fill it in yourself; defaults are enough for a local bench
docker compose up -d db                   # PostgreSQL 16 on host port 5442
uv sync                                   # dependencies from uv.lock
uv run alembic upgrade head               # revisions 0001…0004, role fuzzhelm_app, append-only journal and audit log
uv run fuzzhelm backfill --days 45        # 45 full UTC days of 1m candles, BTCUSDT and ETHUSDT (+ data/dataset_window.json)
uv run fuzzhelm fetch-funding             # funding-rate history for the window
uv run fuzzhelm db-stats                  # database state
uv run uvicorn fuzzhelm.api.main:app --port 8000     # API; docs at http://localhost:8000/docs
uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db   # offline replay of the recorded session
```

With containers: `docker compose up -d --build db api worker` (api on `127.0.0.1:8000`, worker replays the `replay` profile).
The `ui` service sits behind the `ui` profile, and the web panel is not implemented yet. Details:
[`docs/manuals/deployment.md`](../manuals/deployment.md) (in Ukrainian).

The first API administrator is created manually. The password is typed twice without echo and never goes through argv:

```bash
uv run fuzzhelm user add --login <login> --role admin
```

## CLI commands (`uv run fuzzhelm …`)

| Command | What it does |
|---|---|
| `backfill [--days 45] [--symbols BTCUSDT,ETHUSDT] [--end-date YYYY-MM-DD] [--dry-run]` | REST backfill of 1m candles with a one-bar page overlap; the window is pinned in `data/dataset_window.json` |
| `fetch-funding [--dry-run]` | funding-rate history → `data/funding_<SYMBOL>.json` |
| `crosscheck [--from-input data/crosscheck_input.json.gz]` | cross-check Binance BTC-USDT-PERP ↔ Kraken BTC-USD-SPOT (50 bp threshold) |
| `calibrate [--symbol BTCUSDT] [--is-days 15] [--no-write]` | calibrate membership functions T (percentiles) and V (KMeans) on the first in-sample window |
| `replay-gap` | replay the `gap` pathological session into the DB with a real REST backfill (`ingest_gap` rows FILLED) |
| `verify-journal [--run-id UUID]` | recompute the event-journal hash chain and check it against the run passport |
| `db-stats [--json]` | row counts of the 15 tables, candle coverage, gap statuses |
| `user add \| list \| set-role` | API users (roles operator, analyst, auditor, admin); every action is written to `audit_log` |

Other entry points:
`python -m fuzzhelm.workers.trading_worker --profile replay|paper`,
`python -m fuzzhelm.workers.ingest_worker [--symbols …] [--minutes N]`, `python -m fuzzhelm.scheduler.jobs`,
`python -m fuzzhelm.backtest.runner [--db SYMBOL] [--report]`, `scripts/run_backtest.py`, `scripts/demo_flash_crash.py`,
`scripts/run_all_experiments.sh` (phase 7). The live testnet order `scripts/testnet_one_order.py --confirm` is run only by a human
with testnet keys in `.env`. Full reference: [`docs/api/cli.md`](../api/cli.md), [`docs/manuals/user_guide.md`](../manuals/user_guide.md).

## Makefile targets

| Target | Command |
|---|---|
| `make up` / `make down` | `docker compose up -d --build` / `docker compose down` |
| `make migrate` | `uv run alembic upgrade head` |
| `make ingest` | `uv run python -m fuzzhelm.cli backfill --days 45` |
| `make record` | `scripts/record_ws_session.py --minutes 45` (record a live WS session) |
| `make replay` | `python -m fuzzhelm.workers.trading_worker --profile replay` |
| `make backtest` | `scripts/run_backtest.py` |
| `make grid` | `scripts/run_grid.py` |
| `make verify` | `python -m fuzzhelm.cli verify-journal` |
| `make test` | `uv run pytest -q` (unit, property, architecture, e2e; offline) |
| `make test-int` | test DB from `docker-compose.test.yml` (port 5443) + `pytest -m integration` |
| `make cov` | `pytest --cov --cov-report=term-missing` |
| `make lint` | `ruff check src tests scripts` + `mypy` (`core`, `fuzzy`, `risk`) |
| `make report` | `scripts/export_report_tables.py` |
| `make backup` / `make restore FILE=…` | `pg_dump -Fc` / `pg_restore --clean --if-exists` ([runbook](../manuals/backup_runbook.md)) |

## Data

- Window: `[2026-08-04T00:00Z, 2026-09-18T00:00Z)`, 45 full UTC days, 64,800 closed 1m bars per symbol (BTCUSDT, ETHUSDT),
  Binance USDⓈ-M. The machine-readable copy with hashes is `data/dataset_window.json`.
- Candle hash (BLAKE2b-256): BTCUSDT `41bc9f0b20a8d4d2…`, ETHUSDT `d9f11c7e9f1e718f…` (`docs/figures/backfill_report.md`).
- The first in-sample window (days 1–15) is the only one used to calibrate the membership functions (`cal-727167b30ad655a5`,
  `config/membership.yaml`).
- Funding rates: `data/funding_BTCUSDT.json`, `data/funding_ETHUSDT.json` (135 records each). Recorded WS sessions:
  `fixtures/ws/btcusdt_2026-09-18.jsonl.gz` (45 min, 92,101 frames); six pathological scenarios live in `fixtures/ws/pathological/`.

## Reproducing a run by `run_id`

1. Read the passport:
   ```bash
   docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -c \
     "select id, kind, config_hash, dataset_hash, git_sha, seed, engine, equity_hash from run where id = '<run_id>'"
   ```
   To see whether the tree had uncommitted changes: `select value from run_metric where run_id = '<run_id>' and name = 'git_dirty'`.
2. Check out the same code: `git checkout <git_sha>`.
3. Check the data: the candle hash of the window must match `data/dataset_window.json` (the scripts verify this themselves).
4. Re-run with the same profile and seed. For the `backtest` profile, use `uv run python scripts/run_backtest.py`. If an
   identical run is already stored, the script re-runs the engine with the current code and reports whether `equity_hash` and
   the journal head are reproduced. For any symbol, use `uv run python -m fuzzhelm.backtest.runner --db <SYMBOL> --report`.
5. Verify the journal and its anchor: `uv run fuzzhelm verify-journal --run-id <run_id>` (expected: "chain OK, anchor OK").

`GET /decisions/{id}/explain` explains any decision of the run. It rebuilds the engine from `run.config`, and the `consistency`
block shows whether the recomputation matches the stored values.

## Tests

```bash
uv run pytest -q                                   # default selection: no integration/live/slow
docker compose -f docker-compose.test.yml up -d --wait
FUZZHELM_TEST_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test \
  uv run pytest -q -m integration -p no:randomly
docker compose -f docker-compose.test.yml down
uv run pytest --collect-only -q                    # at HEAD b933802: 918 of 973 collected, 55 deselected by markers
```

Final test run and coverage: `<<TBD:final_test_run>>`, `<<TBD:final_coverage>>`.

## Status

Done: the Python backend (phases 0–8 of the brief), the phase-7 experiment tooling and the documentation. Not done yet: the
Vue web panel (`<<TBD:ui_views>>`), the Fly.io deployment (`<<TBD:fly_deploy>>`) and the live testnet order, which needs keys
(`<<TBD:testnet_one_order>>`). Phase-7 results: `<<TBD:phase7_results>>`.

## Documentation

The project documentation is in Ukrainian. The map of all documents is [`docs/index.md`](../index.md). Key documents:
[technical specification](../tz/technical_specification.md) ·
[specification deviations](../deviations.md) ·
[work journal](../journal.md) ·
[project risk register](../risk_register.md) ·
[data model](../db_schema.md) ·
[security](../security.md) ·
[user guide](../manuals/user_guide.md) ·
[deployment](../manuals/deployment.md) ·
[module APIs](../api/) ·
[CHANGELOG](CHANGELOG.md).

> Relative links in this draft are relative to `docs/_pending_root/`. After moving the file to the repository root, replace
> `../` with `docs/`.
