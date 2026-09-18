"""Паспорт прогону: усе, що потрібно, щоб відтворити число з таблиці і довести, що воно те саме.

Найменування: backtest/manifest.py
Призначення: поля таблиці `run` (config_hash, dataset_hash, git_sha, seed, engine,
journal_head_hash, equity_hash) + унікальна ідентичність прогону ux_run_identity.
Автор: Андрій Жук, 2026.

  * config_hash  — BLAKE2b-256(canonical_json(config)); float з YAML → Decimal(repr) перед хешуванням;
  * dataset_hash — BLAKE2b-256 над масивами свічок у канонічній формі: колонки за іменем, кожна —
                   (ім'я, dtype '<i8'|'<f8', довжина) + сирі little-endian байти C-порядку; −0.0 → 0.0,
                   NaN заборонено. Для послідовності DTO Candle — окрема функція над canonical_json рядків
                   (Decimal-рядки); дві форми НЕ взаємозамінні (різні представлення даних);
  * equity_hash  — SHA-256(canonical_json(крива)), кожне значення квантоване до 1e−18 (масштаб
                   NUMERIC(38,18)), щоб Decimal('1.0') і Decimal('1.00') давали той самий хеш;
  * git_sha      — `git rev-parse HEAD` (модуль поза межею детермінізму, subprocess дозволений).
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
from numpy.typing import NDArray
from pydantic import BaseModel, ConfigDict

from fuzzhelm.config import ROOT
from fuzzhelm.core.digest import canonical_json
from fuzzhelm.core.dto import Candle
from fuzzhelm.core.enums import EngineKind, RunKind
from fuzzhelm.core.money import quantize_internal
from fuzzhelm.sizing.convert import float_to_decimal_exact

_HEX40 = frozenset("0123456789abcdef")


def canonicalize_config(obj: Any) -> Any:
    """Рекурсивно замінити float на Decimal(repr(x)) (0.5 → '0.5'), кортежі — на списки."""
    if isinstance(obj, bool) or obj is None:
        return obj
    if isinstance(obj, float):
        return float_to_decimal_exact(obj)
    if isinstance(obj, np.generic):
        return canonicalize_config(obj.item())
    if isinstance(obj, Mapping):
        return {str(k): canonicalize_config(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [canonicalize_config(v) for v in obj]
    return obj


def config_hash(config: Mapping[str, Any]) -> str:
    return hashlib.blake2b(canonical_json(canonicalize_config(config)), digest_size=32).hexdigest()


def dataset_hash(columns: Mapping[str, NDArray[Any]]) -> str:
    """BLAKE2b-256 над колонками свічок (напр. t_ns, o, h, l, c, v) у канонічній формі."""
    h = hashlib.blake2b(digest_size=32)
    for name in sorted(columns):
        arr = np.asarray(columns[name])
        if arr.dtype.kind in "iub":
            canon = np.ascontiguousarray(arr, dtype="<i8")
        elif arr.dtype.kind == "f":
            if np.isnan(arr).any():
                raise ValueError(f"column {name!r} contains NaN")
            canon = np.ascontiguousarray(arr + 0.0, dtype="<f8")   # −0.0 + 0.0 = +0.0
        else:
            raise TypeError(f"column {name!r}: unsupported dtype {arr.dtype}")
        h.update(canonical_json([name, canon.dtype.str, list(canon.shape)]))
        h.update(canon.tobytes(order="C"))
    return h.hexdigest()


def dataset_hash_candles(candles: Sequence[Candle]) -> str:
    """BLAKE2b-256 над канонічними рядками DTO-свічок (instrument, tf, open_time_ns, o, h, l, c, volume)."""
    h = hashlib.blake2b(digest_size=32)
    for c in candles:
        h.update(canonical_json([c.instrument, c.tf, c.open_time_ns, c.o, c.h, c.l, c.c, c.volume]))
        h.update(b"\n")
    return h.hexdigest()


def equity_hash(equity: Sequence[Decimal], ts_ns: Sequence[int] | None = None) -> str:
    """SHA-256 канонічної кривої капіталу (опційно з мітками часу)."""
    values = [quantize_internal(e) for e in equity]
    payload: Any = values if ts_ns is None else [[t, v] for t, v in zip(ts_ns, values, strict=True)]
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def read_git_sha(repo: Path = ROOT) -> tuple[str | None, bool | None]:
    """(sha, dirty) поточного коміту; (None, None), якщо git недоступний або це не репозиторій."""
    try:
        sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True,
                             timeout=10, check=True).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True,
                                text=True, timeout=10, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None, None
    if len(sha) != 40 or not set(sha) <= _HEX40:
        return None, None
    return sha, bool(status.strip())


class RunManifest(BaseModel):
    """Паспорт прогону (рядок таблиці `run` без часу/статусу). Хеші — hex-рядки (BYTEA ← bytes.fromhex)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RunKind
    engine: EngineKind
    seed: int
    config_hash: str
    dataset_hash: str
    git_sha: str | None = None
    git_dirty: bool | None = None
    journal_head_hash: str | None = None
    equity_hash: str | None = None

    def identity(self) -> tuple[str, str, int, str, str | None]:
        """Ключ унікальності ux_run_identity (config_hash, dataset_hash, seed, engine, git_sha)."""
        return (self.config_hash, self.dataset_hash, self.seed, self.engine.value, self.git_sha)

    def with_results(self, *, journal_head: bytes | None = None,
                     equity: Sequence[Decimal] | None = None,
                     equity_ts_ns: Sequence[int] | None = None) -> RunManifest:
        """Дописати хеші результатів після завершення прогону."""
        upd: dict[str, Any] = {}
        if journal_head is not None:
            upd["journal_head_hash"] = journal_head.hex()
        if equity is not None:
            upd["equity_hash"] = equity_hash(equity, equity_ts_ns)
        return self.model_copy(update=upd)


def build_manifest(
    *,
    kind: RunKind | str,
    engine: EngineKind | str,
    seed: int,
    config: Mapping[str, Any],
    dataset: Mapping[str, NDArray[Any]] | Sequence[Candle],
    journal_head: bytes | None = None,
    equity: Sequence[Decimal] | None = None,
    equity_ts_ns: Sequence[int] | None = None,
    git: bool = True,
    repo: Path = ROOT,
) -> RunManifest:
    ds = dataset_hash(dataset) if isinstance(dataset, Mapping) else dataset_hash_candles(dataset)
    sha, dirty = read_git_sha(repo) if git else (None, None)
    m = RunManifest(kind=RunKind(kind), engine=EngineKind(engine), seed=seed,
                    config_hash=config_hash(config), dataset_hash=ds, git_sha=sha, git_dirty=dirty)
    return m.with_results(journal_head=journal_head, equity=equity, equity_ts_ns=equity_ts_ns)
