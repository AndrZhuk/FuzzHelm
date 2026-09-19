# Runbook: резервне копіювання і відновлення PostgreSQL FuzzHelm

Автор: Андрій Жук, 2026. Формат — `pg_dump -Fc` (custom: стиснений, вибіркове відновлення, `pg_restore --list`).
Утиліти беруться з самого контейнера `db` (PostgreSQL 16) — на хості клієнт PostgreSQL не потрібен. Файл `.env`
для цих команд не потрібен.

## 1. Резервна копія (робоча БД не зупиняється)

```bash
mkdir -p backups
docker exec fuzzhelm-db-1 pg_dump -U fuzzhelm -Fc fuzzhelm > backups/fuzzhelm_$(date +%Y%m%d_%H%M%S).dump
# те саме через compose (make backup):
docker compose exec -T db pg_dump -U fuzzhelm -Fc fuzzhelm > backups/fuzzhelm_$(date +%Y%m%d_%H%M%S).dump
docker exec -i fuzzhelm-db-1 pg_restore --list < backups/<файл>.dump | grep -c "TABLE DATA"   # 16 = 15 таблиць + alembic_version
```
`pg_dump` бере узгоджений знімок (одна транзакція), тож воркери й API можуть працювати під час копіювання. `-T` у
`compose exec` обов'язковий: без нього TTY зіпсує бінарний потік.

## 2. Перевірка копії відновленням у тимчасову БД (не чіпаючи робочу)

```bash
docker exec fuzzhelm-db-1 createdb -U fuzzhelm fuzzhelm_restore_check
docker exec -i fuzzhelm-db-1 pg_restore -U fuzzhelm -d fuzzhelm_restore_check --exit-on-error < backups/<файл>.dump
Q="select 'candle',count(*) from candle union all select 'event_journal',count(*) from event_journal union all
   select 'run',count(*) from run union all select 'equity_point',count(*) from equity_point"   # … усі 15 таблиць
docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -Atc "$Q" | sort > /tmp/a
docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm_restore_check -Atc "$Q" | sort > /tmp/b && diff /tmp/a /tmp/b
FUZZHELM_DATABASE_URL=postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm_restore_check \
  uv run fuzzhelm verify-journal                                  # хеш-ланцюги і звірка з паспортами прогонів
docker exec fuzzhelm-db-1 dropdb -U fuzzhelm fuzzhelm_restore_check
```
Гранти ролі `fuzzhelm_app` (зокрема REVOKE UPDATE/DELETE на `event_journal`/`audit_log`) відновлюються з копії: роль
кластерна і вже існує. Відновлення в інший кластер — спершу `uv run alembic upgrade 0003_auth_audit` на порожній БД
(створює роль) або `pg_restore --no-privileges`.

## 3. Відновлення робочої БД (руйнівне — лише за рішенням адміністратора)

```bash
docker compose stop api worker                                  # ніхто не пише
docker exec -i fuzzhelm-db-1 pg_restore -U fuzzhelm -d fuzzhelm --clean --if-exists --exit-on-error < backups/<файл>.dump
# або make restore FILE=backups/<файл>.dump
docker exec fuzzhelm-db-1 psql -U fuzzhelm -d fuzzhelm -Atc "select version_num from alembic_version"
uv run fuzzhelm verify-journal && uv run fuzzhelm db-stats
docker compose start api worker
```

## 4. Перевірка цього runbook (виконано 2026-09-19 на робочій БД, порт 5442)

| крок | результат |
|---|---|
| `docker exec fuzzhelm-db-1 pg_dump -U fuzzhelm -Fc fuzzhelm > …` | 0.68 с, 9 206 764 байти |
| `docker compose exec -T db pg_dump … > …` (без `.env`) | 0.89 с, 9 206 764 байти |
| `pg_restore --list … \| grep -c "TABLE DATA"` | 16 |
| `createdb fuzzhelm_restore_check` + `pg_restore --exit-on-error` | 1.1 с, код 0 |
| кількість рядків 15 таблиць + `alembic_version` (відсортований `diff`) | **ідентичні**: candle 130 694, event_journal 37 520, equity_point 64 917, risk_event 27 001, decision 297, sim_order 300, position 115, run 5, run_metric 49, instrument 2, ingest_gap 3, audit_log 1, app_user 0, strategy 0, dq_score 0; ревізія `0004_decision_trace_extras` |
| права у відновленій БД | `fuzzhelm_app`: DELETE на `event_journal` — ні, INSERT — так |
| `verify-journal` у відновленій БД | 6 ланцюгів «chain OK» (4 прогони DONE — ще й «anchor OK», 2 журнали інжесту без рядка `run`); 1 «chain BROKEN at seq 26» — FAILED-прогін `fcf44bb4…`, записаний до виправлення W-10 (`docs/deviations.d/workers.md`) — такий самий і в робочій БД |
| `dropdb fuzzhelm_restore_check` | тимчасову БД видалено, у кластері лишились `fuzzhelm`, `postgres`, `template0/1` |

**Повторна перевірка рецензентом (2026-09-19, та сама робоча БД):** `pg_dump -Fc` — 0.69 с, 9 332 499 байтів; `pg_restore
--list | grep -c "TABLE DATA"` — 16; `createdb fuzzhelm_restore_check` + `pg_restore --exit-on-error` — 1.13 с, код 0;
відсортований `diff` кількостей 15 таблиць + `alembic_version` — порожній (candle 130 695, event_journal 38 610,
equity_point 64 917, risk_event 27 001, decision 297, sim_order 300, position 115, run 5, run_metric 49, instrument 2,
ingest_gap 3, audit_log 1, app_user/strategy/dq_score 0); у відновленій БД `fuzzhelm_app` не має DELETE на `event_journal`
і UPDATE на `audit_log`, має INSERT на `event_journal`; `verify-journal` — 7 ланцюгів OK (4 з «anchor OK»), 1 BROKEN —
`fcf44bb4…` (як і в робочій); `dropdb fuzzhelm_restore_check` — у кластері лишились `fuzzhelm`, `postgres`, `template0/1`.

Команди перевірки й числа — з цієї сесії (журнал `docs/journal.d/workers.md`).
