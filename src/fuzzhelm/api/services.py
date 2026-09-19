"""Сервісний шар API: порти сховища (Protocol), одиниця роботи (транзакція) і збирання залежностей.

Найменування: api/services.py
Призначення: маршрути працюють не з AsyncSession, а з набором репозиторіїв `Repos`, який видає одиниця
роботи `uow()` — одна транзакція, COMMIT при виході без винятку. Робоча реалізація — репозиторії
storage поверх PostgreSQL; тести підміняють `ApiServices` (FastAPI dependency_overrides) на реалізацію в
пам'яті з тими самими методами. Так код маршрутів однаковий у e2e-тестах і на справжній БД.
Автор: Андрій Жук, 2026.

Чому транзакцію відкриває сам маршрут, а не залежність із yield: вихідний код yield-залежності FastAPI
виконується після формування відповіді; якби COMMIT упав там, клієнт уже отримав би «200 OK» за зміну,
якої немає в БД (і без запису аудиту). `async with services.uow()` комітить ДО відповіді.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from fuzzhelm.api.auth import LoginRateLimiter
from fuzzhelm.api.backtests import BacktestService, EngineBacktestService
from fuzzhelm.api.limits import FileLimitsStore, LimitsStore
from fuzzhelm.api.live import (
    CONTROL_CHANNEL,
    LIVE_CHANNEL,
    LiveHub,
    PgLiveSource,
    asyncpg_dsn,
    encode_notify_payload,
)
from fuzzhelm.config import Settings, get_settings
from fuzzhelm.core.enums import Role, RunKind, RunStatus
from fuzzhelm.core.ports import Clock, IdGenerator
from fuzzhelm.infra.wallclock import RandomIdGenerator, SystemClock
from fuzzhelm.storage.repositories import (
    AuditRepo,
    AuditRow,
    CandlePage,
    CandleRepo,
    DecisionRepo,
    DecisionRow,
    DqRepo,
    DqRow,
    EquityRepo,
    EquityRow,
    GapRepo,
    GapRow,
    InstrumentRepo,
    InstrumentRow,
    RiskEventRepo,
    RiskEventRow,
    RunRepo,
    RunRow,
    StrategyRepo,
    StrategyRow,
    UserRepo,
    UserRow,
)
from fuzzhelm.storage.session import APP_ROLE, get_default_engine, session_factory

# ------------------------------------------------------------------ порти (підмножина методів storage)


class UserStore(Protocol):
    async def authenticate(self, login: str, password: str) -> UserRow | None: ...

    async def get(self, user_id: int) -> UserRow | None: ...


class AuditStore(Protocol):
    async def append(
        self,
        action: str,
        target: str,
        *,
        before: Mapping[str, Any] | None = None,
        after: Mapping[str, Any] | None = None,
        user_id: int | None = None,
        ip: str | None = None,
        ts_ns: int | None = None,
    ) -> int: ...

    async def list(
        self,
        *,
        target: str | None = None,
        action: str | None = None,
        user_id: int | None = None,
        limit: int = 200,
    ) -> list[AuditRow]: ...


class StrategyStore(Protocol):
    async def create_version(
        self,
        name: str,
        rules_yaml: str,
        membership_yaml: str,
        *,
        created_by: str | None = None,
        activate: bool = False,
    ) -> StrategyRow: ...

    async def activate(self, strategy_id: int) -> StrategyRow: ...

    async def get(self, strategy_id: int) -> StrategyRow | None: ...

    async def get_version(self, name: str, version: int) -> StrategyRow | None: ...

    async def latest(self, name: str) -> StrategyRow | None: ...

    async def get_active(self, name: str | None = None) -> StrategyRow | None: ...

    async def list_versions(self, name: str) -> list[StrategyRow]: ...

    async def list_names(self) -> list[str]: ...


class RunStore(Protocol):
    async def get(self, run_id: UUID) -> RunRow | None: ...

    async def list(
        self, *, kind: RunKind | str | None = None, status: RunStatus | str | None = None, limit: int = 100
    ) -> list[RunRow]: ...

    async def get_metrics(self, run_id: UUID) -> dict[str, float | None]: ...


class EquityStore(Protocol):
    async def curve(
        self, run_id: UUID, ts_from_ns: int | None = None, ts_to_ns: int | None = None
    ) -> list[EquityRow]: ...


class DecisionStore(Protocol):
    async def get(self, decision_id: int) -> DecisionRow | None: ...


class CandleStore(Protocol):
    async def range(
        self,
        instrument_id: int,
        tf: str,
        ts_from_ns: int | None = None,
        ts_to_ns: int | None = None,
        *,
        after_ns: int | None = None,
        limit: int = 1000,
        closed_only: bool = False,
        descending: bool = False,
    ) -> CandlePage: ...

    async def latest_open_time_ns(
        self, instrument_id: int, tf: str, *, closed_only: bool = True
    ) -> int | None: ...


class InstrumentStore(Protocol):
    async def get(self, instrument_id: int) -> InstrumentRow | None: ...

    async def get_by_canon(self, symbol_canon: str) -> InstrumentRow | None: ...

    async def list(self, *, active_only: bool = False) -> list[InstrumentRow]: ...


class DqStore(Protocol):
    async def range(
        self, instrument_id: int, ts_from_ns: int | None = None, ts_to_ns: int | None = None
    ) -> list[DqRow]: ...

    async def latest(self, instrument_id: int) -> DqRow | None: ...


class GapStore(Protocol):
    async def list_open(self, instrument_id: int | None = None, *, limit: int = 1_000) -> list[GapRow]: ...

    async def stats(self) -> dict[str, int]: ...


class RiskStore(Protocol):
    async def list_for_run(
        self, run_id: UUID, *, since_ns: int | None = None, limit: int = 500, rule: str | None = None
    ) -> list[RiskEventRow]: ...

    async def vetoes(self, run_id: UUID, *, limit: int = 500) -> list[RiskEventRow]: ...

    async def transitions(self, run_id: UUID) -> list[RiskEventRow]: ...


class Repos(Protocol):
    users: UserStore
    audit: AuditStore
    strategies: StrategyStore
    runs: RunStore
    equity: EquityStore
    decisions: DecisionStore
    candles: CandleStore
    instruments: InstrumentStore
    dq: DqStore
    gaps: GapStore
    risk: RiskStore

    async def notify(self, channel: str, kind: str, payload: Mapping[str, Any]) -> None:
        """Подія, що буде доставлена лише після COMMIT цієї одиниці роботи."""
        ...


UnitOfWork = Callable[[], contextlib.AbstractAsyncContextManager[Repos]]

# ------------------------------------------------------------------ PostgreSQL


class DbRepos:
    """Репозиторії storage поверх однієї AsyncSession (межа транзакції — у DbUnitOfWork)."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.users = UserRepo(session)
        self.audit = AuditRepo(session)
        self.strategies = StrategyRepo(session)
        self.runs = RunRepo(session)
        self.equity = EquityRepo(session)
        self.decisions = DecisionRepo(session)
        self.candles = CandleRepo(session)
        self.instruments = InstrumentRepo(session)
        self.dq = DqRepo(session)
        self.gaps = GapRepo(session)
        self.risk = RiskEventRepo(session)

    async def notify(self, channel: str, kind: str, payload: Mapping[str, Any]) -> None:
        # pg_notify у тій самій транзакції: PostgreSQL доставить подію лише після COMMIT
        await self.session.execute(select(func.pg_notify(channel, encode_notify_payload(kind, payload))))


class DbUnitOfWork:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self.factory = factory

    @contextlib.asynccontextmanager
    async def __call__(self) -> AsyncIterator[Repos]:
        async with self.factory() as session, session.begin():
            yield DbRepos(session)


# ------------------------------------------------------------------ збирання


@dataclass
class ApiServices:
    """Усе, що потрібно маршрутам; один екземпляр на застосунок (app.state.services)."""

    uow: UnitOfWork
    limits: LimitsStore
    backtests: BacktestService
    live: LiveHub
    settings: Settings
    clock: Clock
    ids: IdGenerator
    login_limiter: LoginRateLimiter
    detectors_cfg: Mapping[str, Any] | None = None
    live_source: PgLiveSource | None = None
    engine: AsyncEngine | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def ensure_live(self) -> None:
        """Лінивий старт LISTEN (перший SSE-клієнт / health), щоб API піднімався і без БД."""
        if self.live_source is not None:
            self.live_source.ensure_started()

    async def aclose(self) -> None:
        self.live.close()
        if self.live_source is not None:
            await self.live_source.stop()
        await self.backtests.aclose()
        if self.engine is not None:
            await self.engine.dispose()


def build_db_services(
    settings: Settings | None = None,
    *,
    engine: AsyncEngine | None = None,
    limits_path: Path | None = None,
    backtests: BacktestService | None = None,
    clock: Clock | None = None,
    ids: IdGenerator | None = None,
    listen: bool = True,
    own_engine: bool = False,
) -> ApiServices:
    """Робоча збірка: PostgreSQL (роль fuzzhelm_app), файл лімітів, черга бектестів, LISTEN для SSE.

    Нічого не підключається до БД під час збирання — перше з'єднання відкриває перший запит.
    """
    s = settings or get_settings()
    eng = engine or get_default_engine()
    hub = LiveHub()
    source = (
        PgLiveSource(asyncpg_dsn(eng.url.render_as_string(hide_password=False)), hub, role=APP_ROLE)
        if listen
        else None
    )
    return ApiServices(
        uow=DbUnitOfWork(session_factory(eng)),
        limits=FileLimitsStore(limits_path or (s.config_dir / "risk_limits.yaml")),
        backtests=backtests or EngineBacktestService(),
        live=hub,
        settings=s,
        clock=clock or SystemClock(),
        ids=ids or RandomIdGenerator(),
        login_limiter=LoginRateLimiter(),
        live_source=source,
        engine=eng if own_engine else None,
    )


# ------------------------------------------------------------------ спільні запити маршрутів

LIVE_RUN_KINDS: tuple[RunKind, ...] = (RunKind.PAPER, RunKind.REPLAY, RunKind.TESTNET)


async def resolve_live_run(repos: Repos, run_id: UUID | None) -> RunRow | None:
    """Прогін для /risk/*: явний run_id; інакше найсвіжіший RUNNING live-прогін; інакше найсвіжіший live."""
    if run_id is not None:
        return await repos.runs.get(run_id)
    for status in (RunStatus.RUNNING, None):
        best: RunRow | None = None
        for kind in LIVE_RUN_KINDS:
            rows = await repos.runs.list(kind=kind, status=status, limit=1)
            if rows and (best is None or (rows[0].started_at_ns or 0) > (best.started_at_ns or 0)):
                best = rows[0]
        if best is not None:
            return best
    return None


async def resolve_instrument(
    repos: Repos, symbol: str | None, instrument_id: int | None
) -> InstrumentRow | None:
    if instrument_id is not None:
        return await repos.instruments.get(instrument_id)
    if symbol is not None:
        return await repos.instruments.get_by_canon(symbol)
    return None


def role_of(user: UserRow | None) -> Role | None:
    if user is None or user.role is None:
        return None
    try:
        return Role(user.role)
    except ValueError:
        return None


__all__ = [
    "CONTROL_CHANNEL",
    "LIVE_CHANNEL",
    "ApiServices",
    "DbRepos",
    "DbUnitOfWork",
    "Repos",
    "UnitOfWork",
    "build_db_services",
    "resolve_instrument",
    "resolve_live_run",
    "role_of",
]
