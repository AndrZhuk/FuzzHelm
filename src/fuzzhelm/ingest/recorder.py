"""Запис сирої WS-сесії у gzip JSON Lines (формат docs/contracts.md §7).

Найменування: ingest/recorder.py
Призначення: той самий формат файлу, що пише scripts/record_ws_session.py, але як бібліотечний клас
з ін'єктованим годинником: BinanceWsClient може «дзеркалити» live-потік у файл, а
scripts/make_pathological.py — писати похідні сесії. Тут же — типи записів (кадр/керування), спільні
для ws_client, replay і pipeline.
Автор: Андрій Жук, 2026.

Формат (рядок = JSON-об'єкт, компактні роздільники `,` і `:` як у скрипті запису):
  {"v":1,"kind":"header","venue":..,"symbol":..,"urls":{..},"started_ns":int,"minutes":float}
  {"v":1,"kind":"frame","conn":"market"|"public","ts_ingest_ns":int,"stream":str,"data":{..}}
  {"v":1,"kind":"control","conn":..,"ts_ingest_ns":int,"event":"connected"|"disconnected","detail":str}
  {"v":1,"kind":"footer","ended_ns":int,"frames":int}
Файл спершу пишеться як `<ім'я>.part` і атомарно перейменовується при закритті — незавершений запис
ніколи не виглядає як готова фікстура.
"""

from __future__ import annotations

import gzip
import io
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal

from fuzzhelm.core.ports import Clock

FORMAT_VERSION: Final = 1
Conn = Literal["market", "public"]
ControlEvent = Literal["connected", "disconnected"]


@dataclass(frozen=True, slots=True)
class RawFrame:
    """Сирий кадр комбінованого потоку Binance: `stream` і `data` без змін + локальний час отримання."""

    conn: str
    ts_ingest_ns: int
    stream: str
    data: Mapping[str, Any]

    def to_record(self) -> dict[str, Any]:
        return {"v": FORMAT_VERSION, "kind": "frame", "conn": self.conn, "ts_ingest_ns": self.ts_ingest_ns,
                "stream": self.stream, "data": self.data}


@dataclass(frozen=True, slots=True)
class ControlRecord:
    conn: str
    ts_ingest_ns: int
    event: str
    detail: str = ""

    def to_record(self) -> dict[str, Any]:
        return {"v": FORMAT_VERSION, "kind": "control", "conn": self.conn, "ts_ingest_ns": self.ts_ingest_ns,
                "event": self.event, "detail": self.detail}


SessionItem = RawFrame | ControlRecord


def header_record(*, venue: str, symbol: str, urls: Mapping[str, str], started_ns: int,
                  minutes: float | None) -> dict[str, Any]:
    return {"v": FORMAT_VERSION, "kind": "header", "venue": venue, "symbol": symbol, "urls": dict(urls),
            "started_ns": started_ns, "minutes": minutes}


def footer_record(*, ended_ns: int, frames: int) -> dict[str, Any]:
    return {"v": FORMAT_VERSION, "kind": "footer", "ended_ns": ended_ns, "frames": frames}


def dumps_record(rec: Mapping[str, Any]) -> str:
    """Рядок у точності як у scripts/record_ws_session.py (json.dumps, separators=(",", ":"))."""
    return json.dumps(rec, separators=(",", ":"))


def _open_gzip_text(path: Path, compresslevel: int, deterministic: bool) -> io.TextIOWrapper:
    raw = open(path, "wb")  # noqa: SIM115 — закривається разом з обгорткою
    # mtime=0 + порожнє ім'я в заголовку gzip → побайтово відтворюваний файл (похідні фікстури)
    gz = gzip.GzipFile(filename="" if deterministic else path.name, mode="wb", fileobj=raw,
                       compresslevel=compresslevel, mtime=0 if deterministic else None)
    return _ClosingText(gz, raw)


class _ClosingText(io.TextIOWrapper):
    def __init__(self, gz: gzip.GzipFile, raw: io.BufferedWriter) -> None:
        super().__init__(gz, encoding="utf-8", newline="\n")
        self._raw_file = raw

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._raw_file.close()


class SessionRecorder:
    """Пише сесію у `path` (через `path.part`). Використання — як контекстний менеджер або open/close."""

    def __init__(self, path: str | Path, *, symbol: str, urls: Mapping[str, str], clock: Clock,
                 venue: str = "BINANCE_USDM", minutes: float | None = None, compresslevel: int = 6,
                 deterministic: bool = False) -> None:
        self.path = Path(path)
        self.part_path = self.path.with_name(self.path.name + ".part")
        self.symbol = symbol
        self.urls = dict(urls)
        self.venue = venue
        self.minutes = minutes
        self._clock = clock
        self._compresslevel = compresslevel
        self._deterministic = deterministic
        self._f: io.TextIOWrapper | None = None
        self.frames = 0
        self.records = 0
        self.started_ns: int | None = None

    # ------------------------------------------------------------ життєвий цикл

    def open(self, header: Mapping[str, Any] | None = None) -> SessionRecorder:
        if self._f is not None:
            raise RuntimeError("recorder already open")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = _open_gzip_text(self.part_path, self._compresslevel, self._deterministic)
        if header is None:
            self.started_ns = self._clock.now_ns()
            header = header_record(venue=self.venue, symbol=self.symbol, urls=self.urls,
                                   started_ns=self.started_ns, minutes=self.minutes)
        else:
            self.started_ns = int(header["started_ns"])
        self._write_line(header)
        return self

    def close(self, *, ended_ns: int | None = None) -> Path:
        """Дописати footer і атомарно перейменувати `.part` → кінцевий файл."""
        if self._f is None:
            raise RuntimeError("recorder is not open")
        self._write_line(footer_record(ended_ns=self._clock.now_ns() if ended_ns is None else ended_ns,
                                       frames=self.frames))
        self._f.close()
        self._f = None
        os.replace(self.part_path, self.path)
        return self.path

    def abort(self) -> None:
        """Закрити без footer і прибрати `.part` (запис не вдався)."""
        if self._f is not None:
            self._f.close()
            self._f = None
        self.part_path.unlink(missing_ok=True)

    def __enter__(self) -> SessionRecorder:
        return self.open()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                 tb: TracebackType | None) -> None:
        if exc_type is None:
            self.close()
        else:
            self.abort()

    # ------------------------------------------------------------ запис

    def _write_line(self, rec: Mapping[str, Any]) -> None:
        if self._f is None:
            raise RuntimeError("recorder is not open")
        self._f.write(dumps_record(rec) + "\n")
        self.records += 1

    def write(self, item: SessionItem) -> None:
        self._write_line(item.to_record())
        if isinstance(item, RawFrame):
            self.frames += 1

    def write_frame(self, conn: str, stream: str, data: Mapping[str, Any],
                    ts_ingest_ns: int | None = None) -> None:
        ts = self._clock.now_ns() if ts_ingest_ns is None else ts_ingest_ns
        self.write(RawFrame(conn, ts, stream, data))

    def write_control(self, conn: str, event: str, detail: str = "", ts_ingest_ns: int | None = None) -> None:
        self.write(ControlRecord(conn, self._clock.now_ns() if ts_ingest_ns is None else ts_ingest_ns, event,
                                 detail))

    def write_all(self, items: Iterable[SessionItem]) -> None:
        for it in items:
            self.write(it)


def write_session(path: str | Path, header: Mapping[str, Any], items: Iterable[SessionItem], *,
                  ended_ns: int, deterministic: bool = True, compresslevel: int = 6) -> Path:
    """Записати готову сесію (заголовок + записи + footer) — для похідних фікстур."""

    class _Frozen:
        def now_ns(self) -> int:
            return int(header["started_ns"])

    rec = SessionRecorder(path, symbol=str(header["symbol"]), urls=header.get("urls", {}), clock=_Frozen(),
                          venue=str(header.get("venue", "BINANCE_USDM")), minutes=header.get("minutes"),
                          compresslevel=compresslevel, deterministic=deterministic)
    rec.open(header)
    try:
        rec.write_all(items)
    except BaseException:
        rec.abort()
        raise
    return rec.close(ended_ns=ended_ns)
