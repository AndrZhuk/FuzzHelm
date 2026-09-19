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
  * git_sha      — `git rev-parse HEAD` (модуль поза межею детермінізму, subprocess дозволений);
  * git_dirty    — ОДНЕ визначення для всіх паспортів (XS-11, XA-19, W-11 → WIRE-03): «брудний код» =
                   незакомічені зміни ПОЗА `artifacts/` і `docs/`
                   (`git status --porcelain -- . ':(exclude)artifacts' ':(exclude)docs'`).
                   Виводи скриптів і документація на числа прогону не впливають, тож їх поява (паралельне
                   редагування docs/, виводи попереднього кроку оркестратора) не робить прогін «брудним».
                   `read_git_state()` повертає ще й сирий прапорець (`dirty_any`) і шляхи змін
                   (`dirty_paths`).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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


# Каталоги, зміни в яких НЕ роблять код «брудним»: виводи скриптів (artifacts/) і документація (docs/).
# Код, конфіги, дані, фікстури, pyproject/uv.lock, скрипти рахуються завжди.
GIT_DIRTY_IGNORED: tuple[str, ...] = ("artifacts", "docs")
GIT_DIRTY_PATHS_MAX = 20


@dataclass(frozen=True, slots=True)
class GitState:
    """Стан дерева для паспорта прогону.

    sha         — HEAD (40 hex) або None; у контейнері без .git — з FUZZHELM_GIT_SHA (source = "env");
    dirty       — незакомічені зміни КОДУ (поза GIT_DIRTY_IGNORED); None, якщо дерева не видно;
    dirty_any   — сирий `git status --porcelain` (будь-які зміни, зокрема docs/ і artifacts/);
    dirty_paths — перші GIT_DIRTY_PATHS_MAX рядків porcelain, що зробили dirty = True (провенанс).
    """

    sha: str | None
    dirty: bool | None
    dirty_any: bool | None
    dirty_paths: tuple[str, ...] = ()
    ignored: tuple[str, ...] = GIT_DIRTY_IGNORED
    source: str = "git"                 # git | env | none

    def as_dict(self) -> dict[str, Any]:
        return {"sha": self.sha, "dirty": self.dirty, "dirty_any": self.dirty_any,
                "dirty_paths": list(self.dirty_paths), "dirty_ignored": list(self.ignored),
                "source": self.source}


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, timeout=10,
                          check=True).stdout


def read_git_state(repo: Path = ROOT, *, ignored: Sequence[str] = GIT_DIRTY_IGNORED) -> GitState:
    """Єдине визначення «брудного» коду для всіх паспортів (див. докстрінг модуля)."""
    ign = tuple(ignored)
    try:
        sha = _git(repo, "rev-parse", "HEAD").strip()
        raw = _git(repo, "status", "--porcelain")
        code = _git(repo, "status", "--porcelain", "--", ".", *(f":(exclude){p}" for p in ign))
    except (OSError, subprocess.SubprocessError):
        # у Docker-образі немає .git: SHA передається під час збирання (ARG GIT_SHA → FUZZHELM_GIT_SHA);
        # стан дерева тоді невідомий (dirty = None), а не «чистий»
        env_sha = os.environ.get("FUZZHELM_GIT_SHA", "").strip().lower()
        if len(env_sha) == 40 and set(env_sha) <= _HEX40:
            return GitState(env_sha, None, None, ignored=ign, source="env")
        return GitState(None, None, None, ignored=ign, source="none")
    if len(sha) != 40 or not set(sha) <= _HEX40:
        return GitState(None, None, None, ignored=ign, source="none")
    lines = [ln for ln in code.splitlines() if ln.strip()]
    return GitState(sha, bool(lines), bool(raw.strip()), tuple(lines[:GIT_DIRTY_PATHS_MAX]), ign, "git")


def read_git_sha(repo: Path = ROOT) -> tuple[str | None, bool | None]:
    """(sha, dirty_any) — СИРИЙ прапорець `git status --porcelain` (зворотна сумісність: на ньому стоять
    experiments_search.git_state / experiments_analysis.git_state, що звужують його самі й перевіряють, що
    «сирий» відрізняється від «коду»). Для паспортів прогонів — `read_git_state().dirty` (зміни коду)."""
    st = read_git_state(repo)
    return st.sha, st.dirty_any


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
    gs = read_git_state(repo) if git else None
    sha, dirty = (gs.sha, gs.dirty) if gs is not None else (None, None)
    m = RunManifest(kind=RunKind(kind), engine=EngineKind(engine), seed=seed,
                    config_hash=config_hash(config), dataset_hash=ds, git_sha=sha, git_dirty=dirty)
    return m.with_results(journal_head=journal_head, equity=equity, equity_ts_ns=equity_ts_ns)
