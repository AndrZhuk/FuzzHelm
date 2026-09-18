"""Канонічна серіалізація без float і BLAKE2b-дайджести.

Найменування: core/digest.py
Призначення: однаковий об'єкт → однакові байти → однаковий хеш у будь-якому процесі
(незалежно від PYTHONHASHSEED, порядку ключів, представлення Decimal).
Автор: Андрій Жук, 2026.

Чому предвалідатор: orjson серіалізує float нативно і `default` для нього не викликається,
тому заборону float неможливо реалізувати через `default` — лише рекурсивним обходом до dumps.
"""

from __future__ import annotations

import dataclasses
import hashlib
from collections.abc import Mapping
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

import orjson
from pydantic import BaseModel

from fuzzhelm.core.money import dec_str


def assert_no_float(obj: Any, path: str = "$") -> None:
    """Рекурсивно відкинути будь-який float (включно з numpy.float64 — підклас float)."""
    if isinstance(obj, bool) or obj is None:
        return
    if isinstance(obj, float):
        raise TypeError(f"float is forbidden in canonical data at {path}: {obj!r}")
    if isinstance(obj, BaseModel):
        assert_no_float(obj.model_dump(mode="python"), path)
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        assert_no_float(dataclasses.asdict(obj), path)
    elif isinstance(obj, Mapping):
        for k, v in obj.items():
            if not isinstance(k, str):
                raise TypeError(f"non-str key {k!r} at {path}")
            assert_no_float(v, f"{path}.{k}")
    elif isinstance(obj, list | tuple | set | frozenset):
        for i, v in enumerate(obj):
            assert_no_float(v, f"{path}[{i}]")
    elif type(obj).__module__ == "numpy":
        # numpy int/bool скаляри та масиви не є канонічними: конвертуйте явно
        raise TypeError(f"numpy value is forbidden in canonical data at {path}: {type(obj).__name__}")


def to_canonical(obj: Any) -> Any:
    """Перетворити на дерево з dict/list/str/int/bool/None; Decimal → рядок без експоненти."""
    if obj is None or isinstance(obj, bool | int | str):
        return obj
    if isinstance(obj, Decimal):
        return dec_str(obj)
    if isinstance(obj, Enum):
        return to_canonical(obj.value)
    if isinstance(obj, UUID):
        return str(obj)
    if isinstance(obj, bytes):
        return obj.hex()
    if isinstance(obj, BaseModel):
        return to_canonical(obj.model_dump(mode="python"))
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return to_canonical(dataclasses.asdict(obj))
    if isinstance(obj, Mapping):
        return {str(k): to_canonical(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [to_canonical(v) for v in obj]
    if isinstance(obj, set | frozenset):
        return sorted(to_canonical(v) for v in obj)
    raise TypeError(f"type {type(obj).__name__} is not canonical-serializable")


def canonical_json(obj: Any) -> bytes:
    assert_no_float(obj)
    return orjson.dumps(to_canonical(obj), option=orjson.OPT_SORT_KEYS)


def digest(obj: Any, size: int = 32) -> bytes:
    return hashlib.blake2b(canonical_json(obj), digest_size=size).digest()


def hex_digest(obj: Any, size: int = 32) -> str:
    return digest(obj, size).hex()


def event_uid(*natural_key: Any) -> str:
    """BLAKE2b-128 від природного ключа події, напр. (venue, stream, symbol, open_time_ns)."""
    return hashlib.blake2b(canonical_json(list(natural_key)), digest_size=16).hexdigest()
