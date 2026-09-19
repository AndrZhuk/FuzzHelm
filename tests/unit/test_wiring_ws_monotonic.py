"""WS-07: сторож тиші живого BinanceWsClient міряє інтервали монотонним годинником, мітки кадрів — час події.

Найменування: tests/unit/test_wiring_ws_monotonic.py
Автор: Андрій Жук, 2026.

Без мережі й без sleep: з'єднання — фіктивне, «очікування» recv рухає обидва фіктивні годинники на тривалість
тиші, а крок NTP — лише настінний. Стрибок настінного годинника вставляється в мить між міткою кадру і
наступним розрахунком залишку таймауту (через стік запису сесії, який клієнт кличе для кожного кадру).
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import orjson
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.frames import Close as WsClose

from fuzzhelm.config import Settings
from fuzzhelm.core.clock import ManualClock
from fuzzhelm.ingest.normalize import normalize_exchange_info
from fuzzhelm.ingest.reconnect import Backoff
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionItem
from fuzzhelm.ingest.replay import read_session
from fuzzhelm.ingest.ws_client import BinanceWsClient

ROOT = Path(__file__).resolve().parents[2]
REST = ROOT / "fixtures" / "rest"
INSTR = normalize_exchange_info(json.loads((REST / "exchange_info.json").read_text()),
                                ["BTCUSDT"])["BTCUSDT"]
FRAMES = read_session(ROOT / "fixtures" / "ws" / "sample_btcusdt_4m.jsonl.gz").frames
WIRE = [orjson.dumps({"stream": f.stream, "data": f.data}).decode()
        for f in FRAMES if f.conn == "market"][:12]
S = 1_000_000_000


class WallClock:
    """Настінний годинник, який (на відміну від ManualClock) можна крокувати і назад — як NTP."""

    def __init__(self, t: int) -> None:
        self.t = t

    def now_ns(self) -> int:
        return self.t

    def jump(self, dt_ns: int) -> None:
        self.t += dt_ns


class Silence:
    def __init__(self, seconds: float, *, wall_jump_s: float = 0.0) -> None:
        self.seconds = seconds
        self.wall_jump_s = wall_jump_s          # крок NTP посеред цієї тиші


class Close:
    pass


class FakeWs:
    def __init__(self, script: list[Any]) -> None:
        self.script: deque[Any] = deque(script)


def connector(script: list[Any]) -> Any:
    sessions = {"market": deque([script]), "public": deque()}

    def connect(url: str) -> Any:
        conn = "market" if "/market/" in url else "public"
        entry = sessions[conn].popleft() if sessions[conn] else [Close()]

        @asynccontextmanager
        async def ctx() -> AsyncIterator[FakeWs]:
            yield FakeWs(entry)

        return ctx()

    return connect


def fake_recv(wall: WallClock, mono: ManualClock) -> Any:
    """asyncio.wait_for над фіктивним з'єднанням: тиша довша за таймаут → обидва годинники + таймаут,
    TimeoutError; інакше тиша минає і приходить наступний кадр (+1 мс)."""

    def advance(seconds: float) -> None:
        dt = round(seconds * S)
        wall.jump(dt)
        mono.advance(dt)

    async def recv(ws: FakeWs, timeout: float) -> str:
        while True:
            head = ws.script[0] if ws.script else Close()
            if isinstance(head, Silence):
                if head.wall_jump_s:
                    wall.jump(round(head.wall_jump_s * S))
                    head.wall_jump_s = 0.0
                if head.seconds > timeout:
                    head.seconds -= timeout
                    advance(timeout)
                    raise TimeoutError
                ws.script.popleft()
                advance(head.seconds)
                continue
            if isinstance(head, Close):
                raise ConnectionClosedError(WsClose(1000, "bye"), None)
            ws.script.popleft()
            advance(0.001)
            return str(head)

    return recv


class JumpOnFrame:
    """Стік запису сесії (клієнт кличе write для кожного елемента ДО наступного recv): на k-му кадрі —
    крок настінного годинника, тобто рівно між міткою кадру і розрахунком залишку таймауту сторожа."""

    def __init__(self, wall: WallClock, at_frame: int, jump_s: float) -> None:
        self.wall, self.at, self.jump_ns, self.frames = wall, at_frame, round(jump_s * S), 0

    def write(self, item: SessionItem) -> None:
        if isinstance(item, RawFrame):
            self.frames += 1
            if self.frames == self.at:
                self.wall.jump(self.jump_ns)


def client(script: list[Any], wall: WallClock, mono: ManualClock, *, monotonic: bool,
           recorder: Any = None) -> BinanceWsClient:
    async def no_sleep(_: float) -> None:
        return None

    return BinanceWsClient(Settings(symbols=("BTCUSDT",)), wall, instruments=[INSTR],
                           connect=connector(script), recv=fake_recv(wall, mono), sleep=no_sleep,
                           heartbeat_timeout_s=10.0,
                           backoff=lambda c: Backoff(base_s=1.0, cap_s=8.0, seed=5), max_connects=1,
                           recorder=recorder, monotonic=mono if monotonic else None)


def _disconnects(items: list[SessionItem]) -> list[str]:
    return [i.detail for i in items if isinstance(i, ControlRecord) and i.event == "disconnected"
            and i.conn == "market"]


@pytest.mark.parametrize("monotonic", [True, False])
async def test_forward_wall_clock_jump_does_not_force_a_reconnect(monotonic: bool) -> None:
    """Крок NTP +40 с між кадрами: сторож на монотонному годиннику тиші не бачить (0 спрацювань, 12 кадрів),
    а на настінному (як було до WS-07) — бачить «40 с тиші» і рве здорове з'єднання."""
    wall, mono = WallClock(1_000 * S), ManualClock(5 * S)
    script = [*WIRE[:6], Silence(0.5), *WIRE[6:], Close()]
    c = client(script, wall, mono, monotonic=monotonic, recorder=JumpOnFrame(wall, at_frame=6, jump_s=40.0))
    items = [it async for it in c.items()]
    frames = [i for i in items if isinstance(i, RawFrame) and i.conn == "market"]
    if monotonic:
        assert c.watchdog_fires["market"] == 0 and len(frames) == len(WIRE)
        assert [d.split(":")[0] for d in _disconnects(items)] == ["NORMAL"]
        # мітки кадрів — час події (настінний), зі стрибком: наступний кадр після кроку — на +40 с
        assert frames[6].ts_ingest_ns - frames[5].ts_ingest_ns >= 40 * S
    else:
        assert c.watchdog_fires["market"] == 1 and len(frames) == 6
        assert _disconnects(items)[0].startswith("HEARTBEAT_TIMEOUT")


async def test_backward_wall_clock_jump_does_not_hide_a_real_stall() -> None:
    """Крок NTP −40 с посеред справжньої тиші 15 с: монотонний сторож рве з'єднання рівно через 10 с тиші;
    настінний порахував би дедлайн від «повернутого» часу і справжній розрив проґавив би."""
    for monotonic, fires in ((True, 1), (False, 0)):
        wall, mono = WallClock(1_000 * S), ManualClock(5 * S)
        script = [*WIRE[:3], Silence(15.0, wall_jump_s=-40.0), *WIRE[3:5], Close()]
        c = client(script, wall, mono, monotonic=monotonic)
        items = [it async for it in c.items()]
        assert c.watchdog_fires["market"] == fires, monotonic
        if monotonic:
            assert _disconnects(items)[0].startswith("HEARTBEAT_TIMEOUT")


def test_workers_inject_a_monotonic_watchdog_clock_and_keep_event_time_stamps() -> None:
    from fuzzhelm.infra.wallclock import MonotonicClock, SystemClock  # noqa: PLC0415
    from fuzzhelm.workers import ingest_worker, trading_worker  # noqa: PLC0415

    settings = Settings(symbols=("BTCUSDT",), _env_file=None)  # type: ignore[call-arg]
    for make in (ingest_worker.make_ws_client, trading_worker.make_ws_client):
        wall = SystemClock()
        ws = make(settings, wall, [INSTR])
        assert isinstance(ws, BinanceWsClient)
        assert ws.clock is wall and isinstance(ws.monotonic, MonotonicClock)
    # без явного монотонного годинника (реплей, тести) сторож іде за годинником події — як раніше
    plain = BinanceWsClient(settings, ManualClock(), instruments=[INSTR])
    assert plain.monotonic is plain.clock
