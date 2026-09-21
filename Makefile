# FuzzHelm — команди проєкту. Перевірка без виконання: `make -n <ціль>`. Параметри: make backtest SYMBOL=ETHUSDT
PY := uv run
SYMBOL ?= BTCUSDT
TEST_DB_URL ?= postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test
.PHONY: up down migrate ingest record replay backtest verify test test-int cov lint \
	audit users backup restore api-demo ui ui-build ui-check screens

up:          ; docker compose up -d --build
down:        ; docker compose down
migrate:     ; $(PY) alembic upgrade head
ingest:      ; $(PY) python -m fuzzhelm.cli backfill --days 45
record:      ; $(PY) python scripts/record_ws_session.py --minutes 45
replay:      ; $(PY) python -m fuzzhelm.workers.trading_worker --profile replay
backtest:    ; $(PY) python scripts/run_backtest.py --symbol $(SYMBOL)
verify:      ; $(PY) python -m fuzzhelm.cli verify-journal
test:        ; $(PY) pytest -q -n 6        # паралельно у 6 процесів (pytest-xdist)
test-int:    ; docker compose -f docker-compose.test.yml up -d --wait && FUZZHELM_TEST_DATABASE_URL=$(TEST_DB_URL) $(PY) pytest -q -m integration -p no:randomly; docker compose -f docker-compose.test.yml down
cov:         ; $(PY) pytest -q -n 6 --cov --cov-report=term-missing && $(PY) python -m tests.helpers.cov_packages
lint:        ; $(PY) ruff check src tests scripts && $(PY) mypy src/fuzzhelm/core src/fuzzhelm/fuzzy src/fuzzhelm/risk
audit:       ; $(PY) pip-audit --skip-editable --progress-spinner off
# користувача API створює людина: пароль вводиться інтерактивно (getpass), ніколи не в argv
users:
	@echo "Створити користувача API (пароль — інтерактивно, getpass):"
	@echo "  uv run fuzzhelm user add --login <логін> --role admin|operator|analyst|auditor"
	@echo "  docker compose run --rm -it api fuzzhelm user add --login <логін> --role admin   # у контейнері"
	@echo "Переглянути / змінити роль:  uv run fuzzhelm user list  |  uv run fuzzhelm user set-role --help"
# --- веб-панель ----------------------------------------------------------------
# API з кнопками швидкого входу під демо-користувачами (лише локальний стенд!)
api-demo:    ; FUZZHELM_DEMO_LOGIN=1 $(PY) uvicorn fuzzhelm.api.main:app --port 8000
# Vite проксіює /api на FUZZHELM_API (типово http://127.0.0.1:8000), тож CORS не потрібен.
ui:          ; cd ui && npm install && npm run dev
ui-build:    ; cd ui && npm install && npm run build
ui-check:    ; cd ui && npm run typecheck
# 14 екранограм: потрібні піднятий API, Vite і (для LiveView) воркер реплею.
# Креденшли лише з оточення: make screens UI_LOGIN=… UI_PASSWORD=…
UI_LOGIN ?=
UI_PASSWORD ?=
screens:
	@test -n "$(UI_LOGIN)" -a -n "$(UI_PASSWORD)" || \
		(echo "Вкажіть UI_LOGIN і UI_PASSWORD: make screens UI_LOGIN=… UI_PASSWORD=…"; exit 2)
	cd ui && FUZZHELM_UI_LOGIN=$(UI_LOGIN) FUZZHELM_UI_PASSWORD=$(UI_PASSWORD) \
		FUZZHELM_SHOTS_DIR=$(CURDIR)/docs/figures/screens node scripts/capture_screens.mjs

backup:      ; mkdir -p backups && docker compose exec -T db pg_dump -U fuzzhelm -Fc fuzzhelm > backups/fuzzhelm_$$(date +%Y%m%d_%H%M%S).dump
restore:     ; docker compose exec -T db pg_restore -U fuzzhelm -d fuzzhelm --clean --if-exists < $(FILE)
