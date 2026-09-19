# FuzzHelm — цілі за брифінгом §9 (+ фаза 7/10: експерименти, walk-forward, звіт, модель аномалій, аудит).
# Перевірка без виконання: `make -n <ціль>`. Параметри: make grid SYMBOL=ETHUSDT WORKERS=4
PY := uv run
SYMBOL ?= BTCUSDT
WORKERS ?= 8
TEST_DB_URL ?= postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test
.PHONY: up down migrate ingest record replay backtest grid walkforward experiments verify test test-int cov lint \
	audit report anomaly users backup restore

up:          ; docker compose up -d --build
down:        ; docker compose down
migrate:     ; $(PY) alembic upgrade head
ingest:      ; $(PY) python -m fuzzhelm.cli backfill --days 45
record:      ; $(PY) python scripts/record_ws_session.py --minutes 45
replay:      ; $(PY) python -m fuzzhelm.workers.trading_worker --profile replay
backtest:    ; $(PY) python scripts/run_backtest.py --symbol $(SYMBOL)
# 108 клітинок сітки + OOS кожної, фронт Парето, PSR/DSR; --persist commit лише із закоміченого коду (XS-11)
grid:        ; $(PY) python scripts/run_grid.py --db --symbol $(SYMBOL) --engine mamdani --workers $(WORKERS)
walkforward: ; $(PY) python scripts/run_walkforward.py --db --symbol $(SYMBOL) --engine both --workers $(WORKERS)
# повний прогін фази 7 (≈ години; журнал кроків — artifacts/exp_logs/summary.tsv)
experiments: ; bash scripts/run_all_experiments.sh
verify:      ; $(PY) python -m fuzzhelm.cli verify-journal
test:        ; $(PY) pytest -q -n 6        # pytest-xdist: ~13 с замість ~30 с послідовно (§17)
test-int:    ; docker compose -f docker-compose.test.yml up -d --wait && FUZZHELM_TEST_DATABASE_URL=$(TEST_DB_URL) $(PY) pytest -q -m integration -p no:randomly; docker compose -f docker-compose.test.yml down
cov:         ; $(PY) pytest -q -n 6 --cov --cov-report=term-missing && $(PY) python -m tests.helpers.cov_packages
lint:        ; $(PY) ruff check src tests scripts && $(PY) mypy src/fuzzhelm/core src/fuzzhelm/fuzzy src/fuzzhelm/risk
audit:       ; $(PY) pip-audit --skip-editable --progress-spinner off
# таблиці звіту з виводів експериментів і фактів БД (лише читання); відсутнє → <<TBD:…>>
report:      ; $(PY) python scripts/export_report_tables.py --db --results-dir docs/report_tables/raw --results-dir artifacts/exp_search --out docs/report_tables
# MLP-автокодувальник: ROC-AUC на ін'єкціях (docs/figures/quality_mlp_rocauc.md) + артефакт data/anomaly_mlp_$(SYMBOL).json
anomaly:     ; $(PY) python scripts/train_anomaly_mlp.py --symbol $(SYMBOL)
# користувачів API створює людина: пароль вводиться інтерактивно (getpass), ніколи не в argv [ЛЮДИНА]
users:
	@echo "Створити користувача API (пароль — інтерактивно, getpass):"
	@echo "  uv run fuzzhelm user add --login <логін> --role admin|operator|analyst|auditor"
	@echo "  docker compose run --rm -it api fuzzhelm user add --login <логін> --role admin   # у контейнері"
	@echo "Переглянути / змінити роль:  uv run fuzzhelm user list  |  uv run fuzzhelm user set-role --help"
backup:      ; docker compose exec -T db pg_dump -U fuzzhelm -Fc fuzzhelm > backups/fuzzhelm_$$(date +%Y%m%d_%H%M%S).dump
restore:     ; docker compose exec -T db pg_restore -U fuzzhelm -d fuzzhelm --clean --if-exists < $(FILE)
