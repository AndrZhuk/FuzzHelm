"""Асинхронний запуск бектестів з API: черга задач у процесі, рушій — через ін'єктований порт.

Найменування: api/backtests.py
Призначення: POST /backtests має одразу повернути run_id, а сам прогін (хвилини CPU) — іти у фоні,
не блокуючи event loop. Рушій бектесту (`fuzzhelm.backtest.engine.run_backtest`, хвиля 2) пишеться
паралельно, тому API залежить лише від протоколу `BacktestService`; типова реалізація імпортує рушій
ліниво в момент виконання і запускає його в окремому потоці.
Автор: Андрій Жук, 2026.

Контракт рушія (docs/api/api.md, «Бектести»): модуль `fuzzhelm.backtest.engine` з `BacktestConfig` і
синхронною `run_backtest(dataset, cfg, seed, *, run_id, git, kind) -> BacktestResult`; рушій чистий, а
читання свічок і запис результатів робить `api.backtest_runner.DbBacktestRunner`. Якщо рушія немає
(модуль не імпортується), задача переходить у FAILED з поясненням — GET /runs/{id} показує це чесно,
а не «вічний RUNNING».
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from types import ModuleType
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy.exc import DBAPIError

from fuzzhelm.core.errors import FuzzHelmError
from fuzzhelm.storage.repositories.common import sqlstate

log = logging.getLogger(__name__)

ERROR_MAX_CHARS = 500
ENGINE_MODULE = "fuzzhelm.backtest.engine"
ENGINE_FUNCTION = "run_backtest"
ENGINE_API: tuple[str, ...] = (ENGINE_FUNCTION, "BacktestConfig")


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class BacktestQueueFull(FuzzHelmError):
    """Забагато незавершених задач (захист від DoS) → HTTP 429."""


def public_error(exc: BaseException, limit: int = ERROR_MAX_CHARS) -> str:
    """Текст помилки прогону для користувача (GET /runs/{id} → job.error, run.error).

    str(DBAPIError) містить SQL і параметри запиту, тож для помилок СУБД віддаємо лише клас і SQLSTATE —
    як і обробники помилок API (main._install_error_handlers); повне трасування — лише в журнал сервера.
    """
    if isinstance(exc, DBAPIError):
        return f"{type(exc).__name__}: database error (sqlstate {sqlstate(exc)})"[:limit]
    return f"{type(exc).__name__}: {exc}"[:limit]


@dataclass
class BacktestJob:
    run_id: UUID
    spec: dict[str, Any]
    actor: str
    status: JobStatus = JobStatus.PENDING
    error: str | None = None
    result: Any = None
    task: asyncio.Task[None] | None = field(default=None, repr=False)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": str(self.run_id),
            "status": self.status.value,
            "error": self.error,
            "actor": self.actor,
            "spec": self.spec,
        }


class BacktestService(Protocol):
    async def submit(self, run_id: UUID, spec: Mapping[str, Any], *, actor: str) -> BacktestJob: ...

    def job(self, run_id: UUID) -> BacktestJob | None: ...

    async def aclose(self) -> None: ...


Runner = Callable[[UUID, Mapping[str, Any]], Awaitable[Any]]


def load_engine_module() -> ModuleType:
    """Лінивий імпорт рушію; відсутність модуля чи його API → FuzzHelmError із зрозумілим текстом."""
    try:
        module = importlib.import_module(ENGINE_MODULE)
    except ImportError as e:
        raise FuzzHelmError(f"backtest engine is not available: {ENGINE_MODULE} cannot be imported") from e
    missing = [name for name in ENGINE_API if not hasattr(module, name)]
    if missing or not callable(getattr(module, ENGINE_FUNCTION)):
        raise FuzzHelmError(f"backtest engine is not available: {ENGINE_MODULE} lacks {missing}")
    return module


def default_runner() -> Runner:
    """Типовий виконавець: PostgreSQL процесу (роль fuzzhelm_app) + рушій (api.backtest_runner)."""
    from fuzzhelm.api.backtest_runner import DbBacktestRunner  # noqa: PLC0415 — уникнути циклу імпорту
    from fuzzhelm.config import get_settings  # noqa: PLC0415
    from fuzzhelm.storage.session import get_default_factory  # noqa: PLC0415

    settings = get_settings()
    return DbBacktestRunner(get_default_factory(), data_dir=settings.config_dir.parent / "data")


class EngineBacktestService:
    """Черга у процесі API: не більше `max_concurrent` прогонів одночасно і `max_pending` у черзі.

    Реєстр задач обмежений (`max_jobs`, найстаріші завершені витісняються), бо довготривалий стан
    прогонів живе в БД (`run`), а тут — лише ще не записане рушієм.
    """

    def __init__(
        self,
        runner: Runner | None = None,
        *,
        max_concurrent: int = 1,
        max_pending: int = 8,
        max_jobs: int = 256,
    ) -> None:
        self._runner: Runner = runner or default_runner()
        self._sem = asyncio.Semaphore(max_concurrent)
        self.max_pending = max_pending
        self.max_jobs = max_jobs
        self._jobs: OrderedDict[UUID, BacktestJob] = OrderedDict()

    def _unfinished(self) -> int:
        return sum(1 for j in self._jobs.values() if j.status in (JobStatus.PENDING, JobStatus.RUNNING))

    async def submit(self, run_id: UUID, spec: Mapping[str, Any], *, actor: str) -> BacktestJob:
        if self._unfinished() >= self.max_pending:
            raise BacktestQueueFull(f"{self.max_pending} backtests already queued or running")
        job = BacktestJob(run_id=run_id, spec=dict(spec), actor=actor)
        self._jobs[run_id] = job
        self._evict()
        job.task = asyncio.get_running_loop().create_task(self._execute(job), name=f"backtest-{run_id}")
        return job

    def _evict(self) -> None:
        while len(self._jobs) > self.max_jobs:
            victim = next(
                (k for k, j in self._jobs.items() if j.status in (JobStatus.DONE, JobStatus.FAILED)), None
            )
            if victim is None:
                return
            del self._jobs[victim]

    async def _execute(self, job: BacktestJob) -> None:
        async with self._sem:
            job.status = JobStatus.RUNNING
            try:
                job.result = await self._runner(job.run_id, job.spec)
            except asyncio.CancelledError:
                job.status, job.error = JobStatus.FAILED, "cancelled on shutdown"
                raise
            except Exception as e:
                # текст помилки рушія — для користувача (без деталей драйвера); трасування — лише в журнал
                job.status, job.error = JobStatus.FAILED, public_error(e)
                log.exception("backtest %s failed", job.run_id)
            else:
                job.status = JobStatus.DONE

    def job(self, run_id: UUID) -> BacktestJob | None:
        return self._jobs.get(run_id)

    async def wait(self, run_id: UUID) -> BacktestJob | None:
        """Дочекатися завершення задачі (для тестів і скриптів)."""
        job = self._jobs.get(run_id)
        if job is not None and job.task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await job.task
        return job

    async def aclose(self) -> None:
        tasks = [j.task for j in self._jobs.values() if j.task is not None and not j.task.done()]
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t
