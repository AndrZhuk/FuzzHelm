"""Сховище лімітів ризику як даних: атомарний запис config/risk_limits.yaml з оптимістичним блокуванням.

Найменування: api/limits.py
Призначення: PUT /risk/limits змінює файл конфігурації (правила/ліміти — дані, брифінг §7), а не код.
Запис атомарний (тимчасовий файл у тій самій теці → fsync → os.replace), конкурентні записи
серіалізуються блокуванням файлу, а «втрачене оновлення» відсікає порівняння SHA-256 (If-Match).
Автор: Андрій Жук, 2026.

Узгодження з аудитом (api/routers/risk.py): спершу запис audit_log у транзакції, потім заміна файлу,
потім COMMIT; якщо COMMIT впав — файл повертається до попереднього тексту (компенсація), тож стану
«ліміт змінено, а в аудиті сліду немає» не виникає.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import tempfile
import threading
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, Protocol, cast

import yaml

from fuzzhelm.core.errors import FuzzHelmError
from fuzzhelm.risk.config import RiskConfig, load_risk_config


class LimitsConflictError(FuzzHelmError):
    """Файл змінився після того, як клієнт його прочитав (expected_sha256 ≠ поточний) → HTTP 409."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"risk limits changed concurrently (expected {expected[:12]}…, got {actual[:12]}…)")
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class LimitsSnapshot:
    text: str
    sha256: str
    config: RiskConfig


class LimitsStore(Protocol):
    def read(self) -> LimitsSnapshot: ...

    def replace(self, new_text: str, *, expected_sha256: str | None) -> LimitsSnapshot: ...

    def restore(self, old_text: str) -> None: ...


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _plain(obj: Any) -> Any:
    """Дерево для YAML: Decimal → float, якщо перетворення точне (0.3 ↔ '0.3'), інакше рядок; Enum → value."""
    if isinstance(obj, Mapping):
        return {str(_plain(k)): _plain(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_plain(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        # load_risk_config перетворює float назад через str(float) → Decimal, тож round-trip точний,
        # коли найкоротше repr float дорівнює десятковому значенню
        f = float(obj)
        return f if Decimal(repr(f)) == obj else format(obj, "f")
    return obj


def config_json(cfg: RiskConfig) -> dict[str, Any]:
    """JSON-подання конфігурації для відповіді й audit_log (Decimal → рядок без експоненти)."""
    return cast("dict[str, Any]", _jsonable(cfg.model_dump(mode="python")))


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        return {str(_jsonable(k)): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return format(obj, "f")
    return obj


def diff_paths(before: Any, after: Any, prefix: str = "") -> list[str]:
    """Крапкові шляхи значень, що відрізняються (для відповіді й журналу)."""
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        out: list[str] = []
        for k in sorted(set(before) | set(after), key=str):
            p = f"{prefix}.{k}" if prefix else str(k)
            if k not in before or k not in after:
                out.append(p)
            else:
                out.extend(diff_paths(before[k], after[k], p))
        return out
    return [] if before == after else [prefix or "$"]


def render_limits_yaml(cfg: RiskConfig, header: str = "") -> str:
    body = yaml.safe_dump(
        _plain(cfg.model_dump(mode="python")), sort_keys=False, allow_unicode=True, default_flow_style=False
    )
    return (header if header.endswith("\n") or not header else header + "\n") + body


def leading_comments(text: str) -> str:
    """Початковий блок коментарів файлу (зберігається при перезаписі, решта коментарів губиться)."""
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "".join(lines)


class FileLimitsStore:
    """config/risk_limits.yaml на диску; потокобезпечний і міжпроцесно серіалізований (flock)."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._lock, open(lock_path, "a+") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def read(self) -> LimitsSnapshot:
        text = self.path.read_text(encoding="utf-8")
        return LimitsSnapshot(text=text, sha256=sha256_text(text), config=load_risk_config(text))

    def _atomic_write(self, text: str) -> None:
        directory = self.path.parent
        fd, tmp = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o644)  # mkstemp дає 0600 — воркер під іншим користувачем не прочитав би ліміти
            os.replace(tmp, self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise
        # fsync теки: після збою живлення перейменування не «відкотиться»
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def replace(self, new_text: str, *, expected_sha256: str | None) -> LimitsSnapshot:
        cfg = load_risk_config(new_text)  # ніколи не пишемо текст, який сам не прочитаємо
        with self._locked():
            current = sha256_text(self.path.read_text(encoding="utf-8"))
            if expected_sha256 is not None and expected_sha256 != current:
                raise LimitsConflictError(expected_sha256, current)
            self._atomic_write(new_text)
        return LimitsSnapshot(text=new_text, sha256=sha256_text(new_text), config=cfg)

    def restore(self, old_text: str) -> None:
        with self._locked():
            self._atomic_write(old_text)
