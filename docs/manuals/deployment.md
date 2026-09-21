# Інструкція з розгортання FuzzHelm

Автор: Андрій Жук, 2026. Цільова машина — робоча станція з Docker, [uv](https://docs.astral.sh/uv/) і Node.js 20+.
Репозиторій краще тримати поза iCloud/Dropbox (напр. `~/dev/fuzzhelm`): синхронізація псує дані PostgreSQL.

## 1. Склад

| сервіс (`docker-compose.yml`) | порт хоста | що робить |
|---|---|---|
| `db` — PostgreSQL 16 | **5442** → 5432 | робоча база `fuzzhelm` (користувач і пароль `fuzzhelm` — лише для локального стенда) |
| `api` — `uvicorn fuzzhelm.api.main:app` | 8000 | REST API + потік подій (SSE), документація — `/docs` |
| `worker` — `python -m fuzzhelm.workers.trading_worker --profile replay` | — | реплей записаної сесії через торговий цикл |
| `ui` — Vite dev-сервер | **5173** | веб-панель, проксі `/api` → `api:8000` |

Окремо — **тестова** база (`docker-compose.test.yml`): PostgreSQL 16 на порту **5443**, дані в пам'яті.

## 2. Змінні оточення

`cp .env.example .env`; без `.env` стенд працює на значеннях за замовчуванням.

| змінна | за замовчуванням | призначення |
|---|---|---|
| `FUZZHELM_DATABASE_URL` | `postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm` | адреса бази |
| `FUZZHELM_JWT_SECRET` | `dev-only-change-me` | ключ підпису токенів входу — **змінити** поза локальним стендом |
| `FUZZHELM_VENUE_BASE_URL` | `https://testnet.binancefuture.com` | захисна межа: основну мережу біржі вказати неможливо |
| `FUZZHELM_SEED` | 20260918 | seed відтворюваності |

## 3. Перший запуск

```bash
docker compose up -d db
uv sync --frozen
uv run alembic upgrade head                        # схема бази і роль fuzzhelm_app
uv run fuzzhelm backfill --days 45                 # 45 днів хвилинних свічок з Binance
uv run fuzzhelm fetch-funding                      # ставки фінансування
uv run fuzzhelm user add --login admin --role admin   # пароль — з клавіатури
uv run uvicorn fuzzhelm.api.main:app --port 8000
```

Застосунок і воркери працюють роллю `fuzzhelm_app`: змінити чи видалити записи журналу подій і журналу аудиту
їй не дозволяє сама СУБД.

## 4. Усе в контейнерах

```bash
docker compose up -d --build                       # db, api, worker, ui
docker compose logs -f worker                      # хід реплею
```

Теку `./config` контейнери бачать спільно: зміна лімітів ризику через API (`PUT /risk/limits`) одразу видна воркеру.

## 5. Тести і зупинка

```bash
make test         # офлайн-тести
make test-int     # інтеграційні з тестовою базою (порт 5443)
docker compose down            # зупинити; дані бази лишаються в томі pgdata
docker compose down -v         # зупинити і стерти базу
```
