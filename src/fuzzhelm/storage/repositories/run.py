"""Репозиторій паспортів прогонів (run) і їхніх метрик (run_metric).

Найменування: storage/repositories/run.py
Призначення: відтворюваність — кожен прогін має config/config_hash, dataset_hash, git_sha, seed,
engine (ux_run_identity), а після завершення — journal_head_hash, equity_hash і статус DONE/FAILED.
Автор: Андрій Жук, 2026.

Хеші приймаються як bytes або hex-рядки (backtest.manifest.RunManifest зберігає hex) і пишуться в BYTEA.
Увага: ux_run_identity містить git_sha, а PostgreSQL вважає NULL-и різними (NULLS DISTINCT за
замовчуванням), тож при git_sha IS NULL індекс дублікатів не ловить — find_by_identity порівнює
через IS NOT DISTINCT FROM і дає викликачу змогу перевірити це явно.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from fuzzhelm.core.enums import EngineKind, RunKind, RunStatus
from fuzzhelm.storage.models import RunMetricModel, RunModel, table_of
from fuzzhelm.storage.repositories.common import as_bytes, enum_value, from_mapping, ns_to_dt_opt

_T = table_of(RunModel)
_M = table_of(RunMetricModel)


class ManifestLike(Protocol):
    """Структурний тип backtest.manifest.RunManifest (storage не імпортує backtest)."""

    kind: Any
    engine: Any
    seed: int
    config_hash: str
    dataset_hash: str
    git_sha: str | None
    journal_head_hash: str | None
    equity_hash: str | None


@dataclass(frozen=True, slots=True)
class RunRow:
    id: UUID
    kind: str | None
    strategy_id: int | None
    instrument_id: int | None
    tf: str | None
    ts_from_ns: int | None
    ts_to_ns: int | None
    config: dict[str, Any]
    config_hash: bytes
    dataset_hash: bytes
    git_sha: str | None
    seed: int
    engine: str | None
    journal_head_hash: bytes | None
    equity_hash: bytes | None
    status: str | None
    error: str | None
    started_at_ns: int | None
    finished_at_ns: int | None


class RunRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.s = session

    async def create(self, run_id: UUID, *, kind: RunKind | str, config: Mapping[str, Any],
                     config_hash: bytes | str, dataset_hash: bytes | str, seed: int,
                     engine: EngineKind | str, git_sha: str | None = None, strategy_id: int | None = None,
                     instrument_id: int | None = None, tf: str | None = None, ts_from_ns: int | None = None,
                     ts_to_ns: int | None = None, started_at_ns: int | None = None) -> RunRow:
        """Паспорт зі статусом RUNNING; started_at = started_at_ns або now() СУБД."""
        values: dict[str, Any] = {
            "id": run_id, "kind": RunKind(kind).value, "strategy_id": strategy_id,
            "instrument_id": instrument_id, "tf": tf, "ts_from": ns_to_dt_opt(ts_from_ns),
            "ts_to": ns_to_dt_opt(ts_to_ns), "config": dict(config), "config_hash": as_bytes(config_hash),
            "dataset_hash": as_bytes(dataset_hash), "git_sha": git_sha, "seed": seed,
            "engine": EngineKind(engine).value, "status": RunStatus.RUNNING.value,
            "started_at": ns_to_dt_opt(started_at_ns) if started_at_ns is not None else func.now(),
        }
        res = await self.s.execute(insert(_T).values(**values).returning(_T))
        return from_mapping(RunRow, res.mappings().one())

    async def create_from_manifest(self, run_id: UUID, manifest: ManifestLike, *, config: Mapping[str, Any],
                                   strategy_id: int | None = None, instrument_id: int | None = None,
                                   tf: str | None = None, ts_from_ns: int | None = None,
                                   ts_to_ns: int | None = None, started_at_ns: int | None = None) -> RunRow:
        return await self.create(
            run_id, kind=enum_value(manifest.kind), config=config, config_hash=manifest.config_hash,
            dataset_hash=manifest.dataset_hash, seed=manifest.seed, engine=enum_value(manifest.engine),
            git_sha=manifest.git_sha, strategy_id=strategy_id, instrument_id=instrument_id, tf=tf,
            ts_from_ns=ts_from_ns, ts_to_ns=ts_to_ns, started_at_ns=started_at_ns,
        )

    async def finish(self, run_id: UUID, status: RunStatus | str = RunStatus.DONE, *,
                     journal_head_hash: bytes | str | None = None, equity_hash: bytes | str | None = None,
                     error: str | None = None, finished_at_ns: int | None = None,
                     ts_to_ns: int | None = None) -> RunRow:
        """Завершити прогін: DONE або FAILED (RUNNING тут — помилка викликача). `ts_to_ns` — кінець вікна
        даних, відомий лише наприкінці (live/replay-воркер); None — не змінювати."""
        st = RunStatus(status)
        if st is RunStatus.RUNNING:
            raise ValueError("finish() expects DONE or FAILED")
        values: dict[str, Any] = {
            "status": st.value, "error": error,
            "finished_at": ns_to_dt_opt(finished_at_ns) if finished_at_ns is not None else func.now(),
        }
        if journal_head_hash is not None:
            values["journal_head_hash"] = as_bytes(journal_head_hash)
        if equity_hash is not None:
            values["equity_hash"] = as_bytes(equity_hash)
        if ts_to_ns is not None:
            values["ts_to"] = ns_to_dt_opt(ts_to_ns)
        res = await self.s.execute(update(_T).where(_T.c.id == run_id).values(**values).returning(_T))
        m = res.mappings().one_or_none()
        if m is None:
            raise LookupError(f"run {run_id} not found")
        return from_mapping(RunRow, m)

    async def get(self, run_id: UUID) -> RunRow | None:
        m = (await self.s.execute(select(_T).where(_T.c.id == run_id))).mappings().one_or_none()
        return None if m is None else from_mapping(RunRow, m)

    async def find_by_identity(self, *, config_hash: bytes | str, dataset_hash: bytes | str, seed: int,
                               engine: EngineKind | str, git_sha: str | None) -> RunRow | None:
        """Прогін з тією самою ідентичністю (NULL git_sha порівнюється як значення)."""
        q = select(_T).where(
            _T.c.config_hash == as_bytes(config_hash), _T.c.dataset_hash == as_bytes(dataset_hash),
            _T.c.seed == seed, _T.c.engine == EngineKind(engine).value,
            _T.c.git_sha.is_not_distinct_from(git_sha),
        ).order_by(_T.c.started_at.desc()).limit(1)
        m = (await self.s.execute(q)).mappings().one_or_none()
        return None if m is None else from_mapping(RunRow, m)

    async def list(self, *, kind: RunKind | str | None = None, status: RunStatus | str | None = None,
                   limit: int = 100) -> list[RunRow]:
        q = select(_T)
        if kind is not None:
            q = q.where(_T.c.kind == RunKind(kind).value)
        if status is not None:
            q = q.where(_T.c.status == RunStatus(status).value)
        res = await self.s.execute(q.order_by(_T.c.started_at.desc().nulls_last()).limit(limit))
        return [from_mapping(RunRow, m) for m in res.mappings()]

    # ------------------------------------------------------------------ run_metric

    async def put_metrics(self, run_id: UUID, metrics: Mapping[str, float | int | None]) -> int:
        """Upsert метрик прогону (DOUBLE PRECISION; NaN допустимий — напр. Sharpe нульової кривої)."""
        if not metrics:
            return 0
        rows = [{"run_id": run_id, "name": k, "value": None if v is None else float(v)}
                for k, v in metrics.items()]
        stmt = pg_insert(_M)
        stmt = stmt.on_conflict_do_update(index_elements=["run_id", "name"],
                                          set_={"value": stmt.excluded.value})
        await self.s.execute(stmt, rows)
        return len(rows)

    async def get_metrics(self, run_id: UUID) -> dict[str, float | None]:
        q = select(_M.c.name, _M.c.value).where(_M.c.run_id == run_id).order_by(_M.c.name)
        res = await self.s.execute(q)
        return {str(k): v for k, v in res.all()}
