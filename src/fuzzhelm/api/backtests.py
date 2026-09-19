"""Асинхронний запуск бектестів з API: черга задач у процесі, рушій — через ін'єктований порт.

Найменування: api/backtests.py
Призначення: POST /backtests має одразу повернути run_id, а сам прогін (хвилини CPU) — іти у фоні,
не блокуючи event loop. Рушій бектесту (`fuzzhelm.backtest.engine.run_backtest`, хвиля 2) пишеться
паралельно, тому API залежить лише від протоколу `BacktestService`; типова реалізація імпортує рушій
ліниво в момент виконання і запускає його в окремому потоці.
Автор: Андрій Жук, 2026.

Очікуваний контракт рушія (docs/api/api.md, «Бектести»): синхронна функція
`run_backtest(*, run_id: UUID, symbol, tf, ts_from_ns, ts_to_ns, engine, seed, strategy_id, params)`,
яка сама пише паспорт `run` (RunRepo), рішення, капітал і метрики. Поки рушія немає, задача переходить у
FAILED з поясненням — GET /runs/{id} показує це чесно, а не «вічний RUNNING».
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
from typing import Any, Protocol
from uuid import UUID

from fuzzhelm.core.errors import FuzzHelmError

log = logging.getLogger(__name__)

ENGINE_MODULE = "fuzzhelm.backtest.engine"
ENGINE_FUNCTION = "run_backtest"


class JobStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"


class BacktestQueueFull(FuzzHelmError):
    """Забагато незавершених задач (захист від DoS) → HTTP 429."""


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


def load_engine_function() -> Callable[..., Any]:
    """Лінивий імпорт рушію; відсутність модуля/функції → FuzzHelmError із зрозумілим текстом."""
    try:
        module = importlib.import_module(ENGINE_MODULE)
    except ImportError as e:
        raise FuzzHelmError(f"backtest engine is not available: {ENGINE_MODULE} cannot be imported") from e
    fn = getattr(module, ENGINE_FUNCTION, None)
    if not callable(fn):
        raise FuzzHelmError(f"backtest engine is not available: {ENGINE_MODULE}.{ENGINE_FUNCTION} missing")
    return fn  # type: ignore[no-any-return]


async def engine_runner(run_id: UUID, spec: Mapping[str, Any]) -> Any:
    """Типовий виконавець: рушій — CPU-робота, тож окремий потік, event loop API не блокується."""
    fn = load_engine_function()
    return await asyncio.to_thread(fn, run_id=run_id, **dict(spec))


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
        self._runner: Runner = runner or engine_runner
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
                # текст помилки рушія — для користувача; трасування — лише в журнал сервера
                job.status, job.error = JobStatus.FAILED, f"{type(e).__name__}: {e}"[:500]
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
