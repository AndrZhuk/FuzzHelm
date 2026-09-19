# Інструкція з розгортання FuzzHelm

Автор: Андрій Жук, 2026. Цільова машина — робоча станція розробника (macOS/Linux) з Docker і `uv`. Репозиторій —
**не в iCloud** (`~/dev/fuzzhelm`, §0.3 брифінгу): синхронізація псує volume PostgreSQL.

## 1. Склад

| сервіс (`docker-compose.yml`, проєкт `fuzzhelm`) | порт хоста | що робить |
|---|---|---|
| `db` — PostgreSQL 16 (alpine), volume `pgdata` | **5442** → 5432 | робоча БД `fuzzhelm` (користувач/пароль `fuzzhelm` — лише для локального стенда) |
| `api` — `uvicorn fuzzhelm.api.main:app` | 8000 | REST + SSE, `/docs` |
| `worker` — `python -m fuzzhelm.workers.trading_worker --profile replay` | — | один реплей записаної сесії з темпом профілю (×30) і вихід |
| `ui` (профіль `ui`) | 5173 | Vue-панель (хвиля UI) |

Окремо — **тестова** БД (`docker-compose.test.yml`, проєкт `fuzzhelm-test`): PostgreSQL 16 на **5443**, дані в tmpfs
(нічого не зберігається). Порти 5432/5433 не використовуються — вони зайняті іншими проєктами на машині розробника.

## 2. Змінні оточення

`.env` (у `.gitignore`) створює людина: `cp .env.example .env` і заповнити **[ЛЮДИНА]** (агент секретів не вводить).
Без `.env` стенд працює на значеннях за замовчуванням (`env_file: required: false`).

| змінна | за замовчуванням | призначення |
|---|---|---|
| `FUZZHELM_DATABASE_URL` | `postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm` | БД (у контейнерах compose підставляє `db:5432`) |
| `FUZZHELM_JWT_SECRET` | `dev-only-change-me` (API попереджає в журналі) | підпис JWT HS256 — **змінити** поза локальним стендом |
| `FUZZHELM_VENUE_BASE_URL` | `https://testnet.binancefuture.com` | лише testnet-хости (інакше `MainnetHostRejected` на старті) |
| `BINANCE_TESTNET_KEY/SECRET` | — | лише для `scripts/testnet_one_order.py` **[ЛЮДИНА]** |
| `TELEGRAM_BOT_TOKEN/CHAT_ID` | — | нотифікації; без них — no-op |
| `FUZZHELM_SEED` | 20260918 | seed відтворюваності |
| `FUZZHELM_LOG` | INFO | рівень журналу воркерів |

## 3. Перший запуск

```bash
docker compose up -d db                                         # або: make up (усі сервіси, з білдом образу)
docker compose ps                                              # db — healthy
uv sync                                                        # залежності з uv.lock
uv run alembic upgrade head                                    # ревізії 0001…0004 (роль fuzzhelm_app, REVOKE журналу)
uv run fuzzhelm backfill --days 45 && uv run fuzzhelm fetch-funding
uv run fuzzhelm db-stats
uv run uvicorn fuzzhelm.api.main:app --port 8000               # або сервіс api з compose
uv run python -m fuzzhelm.workers.trading_worker --profile replay
```
Поточна ревізія робочої БД (2026-09-19): `0004_decision_trace_extras` (`docker exec fuzzhelm-db-1 psql -U fuzzhelm -d
fuzzhelm -Atc "select version_num from alembic_version"`). Застосунок і воркери працюють роллю `fuzzhelm_app`
(`make_engine(role=…)`): UPDATE/DELETE журналу подій і аудиту відхиляє СУБД. Для продакшн-розгортання окрема
LOGIN-роль `IN ROLE fuzzhelm_app` створюється адміністратором **[ЛЮДИНА]** (`docs/deviations.d/storage.md` ST-02).

**Гаряча заміна лімітів у контейнерах.** PUT /risk/limits атомарно переписує `config/risk_limits.yaml` у файловій системі
процесу API і будить воркер каналом `fuzzhelm_control`, а воркер перечитує СВІЙ `config/risk_limits.yaml`. З хоста (API і
воркер в одному дереві) це працює; у поточному `docker-compose.yml` кожен контейнер має власну копію `config/` з образу,
тож зміна до воркера не дійде. Обхід — спільний bind-mount для обох сервісів:
```yaml
  api:    {volumes: ["./config:/app/config"]}
  worker: {volumes: ["./config:/app/config:ro"]}
```
(у compose цієї хвилі не внесено — перевірка всіх сервісів у контейнерах ще попереду; `docs/deviations.d/workers.md`).

Образи `api`/`worker` будуються з `Dockerfile` (`uv sync --frozen --no-dev`, у образ копіюються `src`, `config`,
`alembic`, `fixtures`). Збирання образу й запуск усіх чотирьох сервісів однією командою в цій хвилі **не перевірялися**
(перевірено: `db`, API та воркери з хоста; `docker compose exec` без `.env` — працює).

## 4. Тести

```bash
uv run pytest -q                                               # unit + property + e2e (офлайн, без мережі й sleep)
docker compose -f docker-compose.test.yml up -d --wait         # тестова БД 5443
FUZZHELM_TEST_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5443/fuzzhelm_test \
  uv run pytest -q -m integration -p no:randomly
docker compose -f docker-compose.test.yml down
```
Інтеграційні тести самі мігрують тестову БД і перед кожним тестом очищають таблиці; без БД — пропускаються (skip).

## 5. Зупинка

`docker compose stop` зупиняє сервіси, дані лишаються у volume `pgdata`; `docker compose down -v` **знищує** volume
(перед цим — резервна копія, `docs/manuals/backup_runbook.md`).
