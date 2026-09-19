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

**Гаряча заміна лімітів у контейнерах.** PUT /risk/limits атомарно переписує `config/risk_limits.yaml` (tmp →
`os.replace` у тій самій теці) і будить воркер каналом `fuzzhelm_control`; воркер перечитує `config/risk_limits.yaml`.
З хвилі 3 обидва контейнери бачать **один** файл — bind-mount теки `./config` хоста (PLAT-02):
```yaml
  api:    {volumes: ["./config:/app/config", "./data:/app/data:ro"]}   # api пише ліміти; data — фандинг для POST /backtests
  worker: {volumes: ["./config:/app/config:ro"]}                        # воркер лише читає
```
Наслідок: PUT /risk/limits у контейнері змінює `config/risk_limits.yaml` робочого дерева хоста (це той самий файл, що
в git) — так і задумано: зміна видна воркеру, `git diff` і `audit_log`.

## 3a. Збирання образів і запуск у контейнерах (перевірено 2026-09-19)

Образи `api`/`worker` будуються з `Dockerfile` (`uv sync --frozen --no-dev`; у образ копіюються `src`, `config`,
`alembic`, `fixtures`); `.dockerignore` не пускає в контекст `.env`, `.venv`, `.git`, тести й документацію.
```bash
docker compose build api worker                 # 85 с з порожнім кешем; контекст 15.57 МБ; образи по 852 МБ
docker compose up -d --build db api worker      # db не перестворюється: хеш конфігурації db не змінився
curl -s -o /dev/null -w '%{http_code}\n' localhost:8000/docs      # 200
docker compose logs worker                      # JSON-підсумок реплею
docker compose stop api worker                  # db лишається запущеною
```
Фактичний результат (2026-09-19, 12:45 UTC): `db` — той самий контейнер `fuzzhelm-db-1` («Up 17 hours», volume
`fuzzhelm_pgdata` не чіпався; `docker compose config --hash db` = мітка контейнера `db19a781…`). `api`: `/healthz` 200,
`/docs` 200, `/openapi.json` 200 (OpenAPI 3.1.0, 22 шляхи, у `/risk/events` — `until_ns`, `cursor`, відповідь
`RiskEventPageOut`), захищений маршрут без токена — 401; у журналі старту — попередження про типовий
`FUZZHELM_JWT_SECRET` (`.env` на стенді немає). `worker` (профіль replay, ×30): прогрів 523 барами з БД, 9 111 кадрів,
45 барів, 2 виконання, 1 закрита угода, режим NORMAL, капітал 10000 → 9992.72088640, `status: DONE`, вихід з кодом 0
за 93 с (12:45:47 → 12:47:20 UTC); `equity_hash d0f45aa3…` збігся з реплеєм `c1dbbdf6…`, запущеним раніше з хоста
(відтворюваність хост ↔ контейнер). Спільний `config/`: sha256 `risk_limits.yaml` однаковий на хості й в обох
контейнерах (`a5d212b57856aec9…`); в `api` тека `config` rw, `data` ro, у `worker` `config` ro (`touch` → «Read-only
file system»). Після перевірки `docker compose stop api worker`; `db` працює далі.

Обмеження перевірки: (1) наскрізно «PUT /risk/limits у контейнері → воркер перечитав» не проганялось — у робочій БД
немає адміністратора (створює людина: `docker compose run --rm -it api fuzzhelm user add --login <логін> --role admin`
або `uv run fuzzhelm user add …` з хоста). (2) Прогони з контейнера мають `run.git_sha = NULL`: у образі немає `.git`,
а `backtest.manifest` бере SHA лише з `git rev-parse` (відкрите питання власнику `backtest`: змінна оточення / build-arg).
(3) Якщо `docker compose build` зависає на `load metadata for docker.io/library/python:3.12-slim`, винен
credential-helper Docker Desktop у неінтерактивній сесії (`docker-credential-desktop get` не повертається). Обхід для
публічних образів — тимчасовий `DOCKER_CONFIG` без `credsStore`:
```bash
mkdir -p /tmp/dcfg && echo '{"auths": {}}' > /tmp/dcfg/config.json && ln -sfn ~/.docker/cli-plugins /tmp/dcfg/cli-plugins
DOCKER_CONFIG=/tmp/dcfg DOCKER_HOST=unix://$HOME/.docker/run/docker.sock docker compose build api worker
```

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
