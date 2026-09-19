# FuzzHelm

A service that aggregates exchange market data and automates margin trading operations. Decisions come from a fuzzy-logic
core, and a hierarchical subsystem controls risks and limits automatically. Ukrainian version: [`README.md`](README.md).

> **The soft part proposes, the hard part decides.** The fuzzy core only emits an intent `u ∈ [−1; 1]`. The deterministic
> risk loop has no technical way to increase exposure. This is a property of the verdict algebra, checked by a property-based
> test (`test_risk_chain_never_increases_exposure`).

Design-and-technology internship, Department of Automated Control Systems (ACS), Institute of Computer Science and Information
Technologies, Lviv Polytechnic National University, 2026. Author: Andrii Zhuk. Package version 0.1.0 (`pyproject.toml`);
changes are listed in [`CHANGELOG.md`](CHANGELOG.md) (in Ukrainian).

## Safety and disclaimer

- **Public read-only market data and paper/testnet execution only.** Real funds and mainnet cannot be reached by construction.
  `fuzzhelm.config` enforces a hard host allowlist. Orders may go only to `testnet.binancefuture.com` / `demo-fapi.binance.com`,
  and market data only to public Binance and Kraken hosts. Any other host stops start-up (`MainnetHostRejected`, test
  `test_mainnet_host_is_rejected_by_config`). The default executor of the trading loop is the `PaperBroker` simulator.
- **This is not investment advice.** The work is about the method and a verifiable test bench, not about profitability. On the
  45-day window the strategy **loses money after costs**, and this is reported as is: PSR = DSR = 0 (see "Results" below).
- Secrets live only in `.env`, which is git-ignored. The `.env.example` template holds no real secrets: the testnet keys and the
  Telegram token are empty, `FUZZHELM_JWT_SECRET=change-me` is a placeholder, and the rest are non-secret defaults (local database
  URL, testnet host, seed). User accounts,
  testnet keys and the Telegram token are created by a human. The coding agent never enters credentials.

## Results in brief

Every number below has a named source in [`docs/results.md`](docs/results.md) (in Ukrainian). Data: 45 days, 64,800 one-minute
bars each for BTCUSDT and ETHUSDT (Binance USDⓈ-M perpetuals). Sharpe ratios are annualised from one-minute returns.

| what | result |
|---|---|
| phase-6 run (BTCUSDT, clean code at `415acbd`) | run `4e5be0de-a4b7-4ef1-b84e-4a563b248be3`: −12.00 %, Sharpe −46.12, 303 trades, HALTED after 8.14 days; PSR 0, DSR 0 (N = 108); `git_dirty = 0`, `equity_hash 3b5009cd…` |
| 108-cell grid × 2 symbols | all 216 cells lose money (full-window Sharpe −54.11 … −44.58 on BTCUSDT); every cell is stopped by the HALTED latch at a drawdown of 0.1200–0.1204 |
| walk-forward, 6 folds (concatenated OOS) | Mamdani: Sharpe −76.64 (BTCUSDT) / −69.62 (ETHUSDT); linear vote: −34.53 / −34.80 (it trades 4.8–5.9× less often); PSR 0 |
| three cost models (clean OOS estimate) | BTCUSDT without costs: Sharpe 6.66, PSR 0.9727 (no multiple-testing correction); spread + impact: 1.38; full costs: −76.64 |
| ablation, sensitivity | trading frequency drives the result: u_enter and κ_min have the widest OOS Sharpe range |
| cost of having no hysteresis | measured 0.019–0.129 % of equity per day (the brief claims "1.15 %"; its own arithmetic gives 115.2 %) |
| VaR₉₅ / Kupiec test (bars in position, OOS) | historical VaR not rejected (LR 2.25 / 3.34); parametric rejected (LR 164 / 80): the normal model understates risk |
| Amdahl, 8 processes on Apple M3 (4P + 4E cores) | S(8) = 3.855; least-squares f = 0.1412, structural f = 0.0036; the gap comes from compute slowing down ×1.96, not from a serial fraction |
| MLP anomaly auto-encoder | ROC-AUC 5-3-5 0.9729 / 0.9703 vs 8-3-8 0.9531 / 0.9567 → 5-3-5 runs in the pipeline |
| Mamdani monotonicity with w ≡ 1 | the brief's claim is false: reversals up to 0.1435 (FZ-01) |

Conclusion: the signal has a weak positive gross edge, and execution costs on one-minute bars wipe it out. The risk loop contains
the loss at the 12 % latch, as designed.

## Architecture in brief

```
REST (Binance klines/exchangeInfo/fundingRate, Kraken OHLC) ─┐
WS Binance: /market (kline, aggTrade, markPrice) + /public (depth20) ─┴→ IngestPipeline: normalisation (Decimal),
   invariants, de-duplication, gap detection → REST backfill, data-quality score Q (AHP), MLP anomalies 5-3-5 → PostgreSQL + hash-chained journal
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
  `journal_head_hash`, `equity_hash`) and the flag `run_metric.git_dirty`, which marks uncommitted code changes outside
  `docs/` and `artifacts/`.
- Diagrams: [`docs/diagrams/`](docs/diagrams/) (components, deployment, risk statechart, sequences, ER, classes, activity).

Stack: Python 3.12 (`uv`), FastAPI, Pydantic 2, SQLAlchemy 2 async + asyncpg, Alembic, PostgreSQL 16 (no extensions), numpy,
scikit-learn, websockets, httpx, PyJWT, APScheduler, Docker Compose. Exact versions are pinned in `uv.lock`.

## Quick start

Keep the repository **outside** iCloud (e.g. `~/dev/fuzzhelm`), because synchronisation corrupts the PostgreSQL volume.

```bash
cp .env.example .env                      # fill it in yourself; defaults are enough for a local bench
docker compose up -d db                   # PostgreSQL 16 on host port 5442
uv sync --frozen                          # dependencies exactly as in uv.lock
uv run alembic upgrade head               # revisions 0001…0004, role fuzzhelm_app, append-only journal and audit log
uv run fuzzhelm backfill --days 45        # 45 full UTC days of 1m candles, BTCUSDT and ETHUSDT (+ data/dataset_window.json)
uv run fuzzhelm fetch-funding             # funding-rate history for the window
uv run fuzzhelm db-stats                  # database state
uv run uvicorn fuzzhelm.api.main:app --port 8000     # API: http://127.0.0.1:8000/docs, health check at /healthz
uv run python -m fuzzhelm.workers.trading_worker --profile replay --speed inf --no-db   # offline replay of the recorded session
```

Without `.env`, the default database URL is `postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm` (`fuzzhelm.config`).
The offline replay needs neither a database nor a network: 45 candles, `equity_hash d0f45aa3…`.

With containers: `docker compose up -d --build db api worker` (api on `127.0.0.1:8000`, worker replays the `replay` profile).
The `ui` service sits behind the `ui` profile, and the web panel is not implemented yet. Details:
[`docs/manuals/deployment.md`](docs/manuals/deployment.md) (in Ukrainian).

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

Other entry points: `python -m fuzzhelm.workers.trading_worker --profile replay|paper`,
`python -m fuzzhelm.workers.ingest_worker [--symbols …] [--minutes N]`, `python -m fuzzhelm.scheduler.jobs`,
`python -m fuzzhelm.backtest.runner [--db SYMBOL] [--report]`, `scripts/run_backtest.py`, `scripts/demo_flash_crash.py`,
`scripts/run_all_experiments.sh` (phase 7). Only a human with testnet keys in `.env` runs the live testnet order
`scripts/testnet_one_order.py --confirm`. Full reference: [`docs/api/cli.md`](docs/api/cli.md),
[`docs/manuals/user_guide.md`](docs/manuals/user_guide.md).

## Makefile targets

`make -n <target>` prints any target without running it. Parameters: `make grid SYMBOL=ETHUSDT WORKERS=4`.

| Target | Command |
|---|---|
| `make up` / `make down` | `docker compose up -d --build` / `docker compose down` |
| `make migrate` | `uv run alembic upgrade head` |
| `make ingest` | `uv run python -m fuzzhelm.cli backfill --days 45` |
| `make record` | `scripts/record_ws_session.py --minutes 45` (record a live WS session) |
| `make replay` | `python -m fuzzhelm.workers.trading_worker --profile replay` |
| `make backtest` | `scripts/run_backtest.py --symbol $(SYMBOL)` (phase-6 run written to the DB) |
| `make grid` / `make walkforward` | 108-cell grid / walk-forward with both engines, `--db --workers $(WORKERS)` |
| `make experiments` | `scripts/run_all_experiments.sh`, the full phase-7 run (hours) |
| `make verify` | `python -m fuzzhelm.cli verify-journal` |
| `make test` | `uv run pytest -q -n 6` (pytest-xdist; unit, property, architecture, e2e; offline) |
| `make test-int` | test DB from `docker-compose.test.yml` (port 5443) + `pytest -m integration`, then `down` |
| `make cov` | offline coverage + per-package table and thresholds (`tests.helpers.cov_packages`) |
| `make lint` | `ruff check src tests scripts` + `mypy` (`core`, `fuzzy`, `risk`) |
| `make audit` | `pip-audit --skip-editable` |
| `make report` | `scripts/export_report_tables.py --db …` → `docs/report_tables/` (see the caveat below) |
| `make anomaly` | train the MLP auto-encoder → `data/anomaly_mlp_$(SYMBOL).json` |
| `make users` | how to create an API user (a human types the password) |
| `make backup` / `make restore FILE=…` | `pg_dump -Fc` / `pg_restore --clean --if-exists` ([runbook](docs/manuals/backup_runbook.md)); run `mkdir -p backups` before the first backup |

Caveat for `make report`: the target passes two result directories with identical contents, so the exp_search tables come out
duplicated. The command that built the current `docs/report_tables/` is recorded in
[`docs/report_tables/index.md`](docs/report_tables/index.md) (FIN-02).

## Data

- Window: `[2026-08-04T00:00Z, 2026-09-18T00:00Z)`, 45 full UTC days, 64,800 closed 1m bars per symbol (BTCUSDT, ETHUSDT),
  Binance USDⓈ-M. The machine-readable copy with hashes is `data/dataset_window.json`.
- Candle hash (BLAKE2b-256): BTCUSDT `41bc9f0b20a8d4d2…`, ETHUSDT `d9f11c7e9f1e718f…` (`docs/figures/backfill_report.md`).
- The first in-sample window (days 1–15) is the only one used to calibrate the membership functions (`cal-727167b30ad655a5`,
  `config/membership.yaml`; k = 3 by silhouette 0.3354).
- Funding rates: `data/funding_BTCUSDT.json`, `data/funding_ETHUSDT.json` (135 records each). MLP models:
  `data/anomaly_mlp_{BTCUSDT,ETHUSDT}.json`. Recorded WS sessions: `fixtures/ws/btcusdt_2026-09-18.jsonl.gz` (45 min,
  92,101 frames); six pathological scenarios live in `fixtures/ws/pathological/`.

## Reproducing a run by `run_id`

1. Read the passport (example: the clean phase-6 run):
   ```bash
   docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -c \
     "select id, kind, config_hash, dataset_hash, git_sha, seed, engine, equity_hash from run where id = '4e5be0de-a4b7-4ef1-b84e-4a563b248be3'"
   ```
   To see whether the code had uncommitted changes: `select value from run_metric where run_id = '<run_id>' and name = 'git_dirty'`
   (0 here).
2. Check out the same code: `git checkout <git_sha>`.
3. Check the data: the candle hash of the window must match `data/dataset_window.json` (the scripts verify this themselves).
4. Re-run with the same profile and seed. For the `backtest` profile, use `uv run python scripts/run_backtest.py`. If an
   identical run is already stored, the script rebuilds the report from it. By default (unless `--no-verify` is given) it also
   re-runs the engine with the current code and reports whether `equity_hash` and the journal head are reproduced. For any
   symbol, use `uv run python -m fuzzhelm.backtest.runner --db <SYMBOL> --report`.
5. Verify the journal and its anchor: `uv run fuzzhelm verify-journal --run-id <run_id>` (expected: "chain OK, anchor OK").

Example of reproducibility: runs `a848fa56…` (commit `b933802`) and `4e5be0de…` (commit `415acbd`) share the same
`equity_hash 3b5009cd…`. `GET /decisions/{id}/explain` explains any decision of a run. It rebuilds the engine from `run.config`,
and the `consistency` block shows whether the recomputation matches the stored values.

## Tests

```bash
make test                                          # uv run pytest -q -n 6: no integration/live/slow
docker compose -f docker-compose.test.yml up -d --wait
FUZZHELM_TEST_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test \
  uv run pytest -q -m 'integration or not integration' --cov=fuzzhelm --cov-report=term   # official coverage
uv run python -m tests.helpers.cov_packages        # per-package table and thresholds
docker compose -f docker-compose.test.yml down
```

Measured at `415acbd` on an Apple M3 ([`docs/report_tables/raw/test_runs.txt`](docs/report_tables/raw/test_runs.txt)):

| run | result |
|---|---|
| `make test` (6 workers) | 979 passed in 12.83 s |
| serial `uv run pytest -q` | 979 passed, 55 deselected in 29.90 s |
| unit + property, serial | 807 passed in 23.37 s |
| with integration tests (PostgreSQL 16) | 1034 passed in 108.17 s (979 + 54 integration + 1 slow) |
| coverage, combined run | **89.38 %**; fuzzy 99.4 %, risk 99.9 %, decision 98.7 %, sizing 100.0 % |
| coverage, offline (`make cov`) | 83.72 % |
| `make lint` | ruff: "All checks passed!"; mypy: "Success: no issues found in 35 source files" |
| `uv run pip-audit` | "No known vulnerabilities found" |

Section §10 of the brief names 133 test functions. All 133 exist in `tests/` and expand to 198 test nodes
([`docs/report_tables/test_groups.md`](docs/report_tables/test_groups.md)).

## Status

**Done:** the Python backend (phases 0–8 of the brief), the phase-7 computational experiment with results, and the code part of
phase 10 (coverage, mypy, CI, the MLP in the pipeline). Documentation is also done: technical specification, risk register,
deviations, work journal and report tables.

**Not done** (reason; what is needed and by whom):

- Vue web panel (3 screens, i18n). Reason: work order D-06, backend first. The author builds it as a separate stage,
  starting with `ExplainView`.
- Live testnet order with a screenshot. Reason: there are no testnet keys. The student registers on Binance Futures Testnet,
  puts the keys into `.env` and runs `scripts/testnet_one_order.py --confirm`.
- Fly.io deployment. Reason: it needs registration. The student follows [`docs/manuals/deployment.md`](docs/manuals/deployment.md)
  §6; `fly.toml` is ready.
- Telegram token, first API administrator, database LOGIN role. Reason: only the student enters credentials.
- Demo screencast and an offline rehearsal with Wi-Fi off. The student does both, following §15 of the brief.
- Cost estimate, WBS/Gantt/network chart, IDEF0/BPMN, English abstract and the report itself (phase 11). These are the
  author's report artefacts.

## Documentation

The project documentation is in Ukrainian. The map of all documents is [`docs/index.md`](docs/index.md). Key documents:
[experiment results](docs/results.md) ·
[technical specification](docs/tz/technical_specification.md) ·
[specification deviations](docs/deviations.md) ·
[work journal](docs/journal.md) ·
[project risk register](docs/risk_register.md) ·
[report tables](docs/report_tables/index.md) ·
[data model](docs/db_schema.md) ·
[security](docs/security.md) ·
[user guide](docs/manuals/user_guide.md) ·
[deployment](docs/manuals/deployment.md) ·
[module APIs](docs/api/).
