"""Історія ставок фінансування перпетуала: добір через REST, збереження у data/, колонки для dataset_hash.

Найменування: ingest/funding.py
Призначення: «REST … funding» (брифінг §1 п.1, §4.3): `GET /fapi/v1/fundingRate` за вікном датасету →
    `data/funding_<SYMBOL>.json` (невеликий файл у репозиторії, офлайн-джерело для бектесту) →
    `funding_columns(...)` — колонки, які рушій бектесту додає до `backtest.manifest.dataset_hash`
    разом зі свічками (інша ставка фінансування = інший датасет = інший паспорт прогону).
Автор: Андрій Жук, 2026.

Формат файлу (v1): рядки зберігаються ДОСЛІВНО як рядки біржі (`"0.00004188"`, масштаб не змінюється),
час — мілісекунди біржі (з мілісекундним «хвостом», напр. …600004). `rows_digest` — BLAKE2b-256 від
canonical_json(rows): завантаження перевіряє, що файл не змінено після добору.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import orjson

from fuzzhelm.core.digest import canonical_json
from fuzzhelm.core.money import dec_str
from fuzzhelm.features.convert import to_float
from fuzzhelm.ingest.normalize import (
    NS_PER_MS,
    FundingRate,
    InstrumentLike,
    normalize_funding_rate,
    normalize_funding_rates,
)
from fuzzhelm.ingest.rest_client import FUNDING_RATE, MAX_FUNDING_LIMIT, BinanceRestClient

FORMAT_VERSION: Final = 1
COLUMNS: Final = ("funding_time_ms", "funding_rate", "mark_price", "rate_type")


async def fetch_funding_history(client: BinanceRestClient, instrument: InstrumentLike, start_ms: int,
                                end_ms: int, *,
                                limit: int = MAX_FUNDING_LIMIT) -> tuple[list[FundingRate], int]:
    """Усі ставки з fundingTime ∈ [start_ms, end_ms] (пагінація: наступна сторінка з останнього часу + 1 мс).

    Повертає (ставки за зростанням часу, кількість запитів).
    """
    if end_ms < start_ms:
        raise ValueError("end_ms < start_ms")
    rows: list[Mapping[str, Any]] = []
    requests = 0
    cursor = start_ms
    while cursor <= end_ms:
        page = await client.funding_rate_history(instrument.symbol_venue, start_ms=cursor, end_ms=end_ms,
                                                 limit=limit)
        requests += 1
        rows.extend(page)
        if len(page) < limit:
            break
        last = normalize_funding_rate(page[-1], instrument).funding_time_ms
        if last < cursor:
            break                                   # біржа не просунулась — захист від зациклення
        cursor = last + 1
    rates = [r for r in normalize_funding_rates(rows, instrument)
             if start_ms <= r.funding_time_ms <= end_ms]
    return rates, requests


def _row(r: FundingRate) -> list[Any]:
    return [r.funding_time_ms, dec_str(r.funding_rate),
            None if r.mark_price is None else dec_str(r.mark_price), r.rate_type]


def rows_digest(rows: Sequence[Sequence[Any]]) -> str:
    return hashlib.blake2b(canonical_json([list(x) for x in rows]), digest_size=32).hexdigest()


def funding_document(rates: Sequence[FundingRate], instrument: InstrumentLike, *, base_url: str,
                     window: Mapping[str, Any], fetched_at_utc: str, requests: int) -> dict[str, Any]:
    """Словник файлу `data/funding_<SYMBOL>.json` (порядок ключів стабільний)."""
    rows = [_row(r) for r in rates]
    return {
        "v": FORMAT_VERSION,
        "source": f"GET {base_url.rstrip('/')}{FUNDING_RATE}",
        "venue": instrument.venue.value,
        "symbol": instrument.symbol_venue,
        "symbol_canon": instrument.symbol_canon,
        "window": dict(window),
        "fetched_at_utc": fetched_at_utc,
        "requests": requests,
        "count": len(rows),
        "columns": list(COLUMNS),
        "rows_digest": rows_digest(rows),
        "rows": rows,
    }


def write_funding_json(path: str | Path, doc: Mapping[str, Any]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_bytes(orjson.dumps(doc, option=orjson.OPT_INDENT_2) + b"\n")
    tmp.replace(p)
    return p


@dataclass(frozen=True, slots=True)
class FundingSeries:
    meta: dict[str, Any]
    rates: tuple[FundingRate, ...]


def load_funding_json(path: str | Path, instrument: InstrumentLike) -> FundingSeries:
    """Прочитати файл, перевірити дайджест і кожен рядок тим самим нормалізатором, що й відповідь біржі."""
    doc = orjson.loads(Path(path).read_bytes())
    if not isinstance(doc, dict) or doc.get("v") != FORMAT_VERSION or doc.get("columns") != list(COLUMNS):
        raise ValueError(f"{path}: not a funding file v{FORMAT_VERSION}")
    rows = doc["rows"]
    if rows_digest(rows) != doc.get("rows_digest"):
        raise ValueError(f"{path}: rows_digest mismatch (file was modified after fetch)")
    if doc.get("symbol") != instrument.symbol_venue:
        raise ValueError(f"{path}: symbol {doc.get('symbol')!r} != {instrument.symbol_venue!r}")
    raw = []
    for t_ms, rate, mark, rate_type in rows:
        r: dict[str, Any] = {"symbol": instrument.symbol_venue, "fundingTime": t_ms, "fundingRate": rate,
                             "markPrice": "" if mark is None else mark}
        if rate_type is not None:
            r["rateType"] = rate_type
        raw.append(r)
    rates = tuple(normalize_funding_rates(raw, instrument))
    if len(rates) != len(rows):
        raise ValueError(f"{path}: duplicate funding times")
    meta = {k: v for k, v in doc.items() if k != "rows"}
    return FundingSeries(meta, rates)


def funding_columns(rates: Sequence[FundingRate], prefix: str = "funding_"
                    ) -> dict[str, npt.NDArray[Any]]:
    """Колонки для `backtest.manifest.dataset_hash`: `<prefix>t_ns` (int64, нс) і `<prefix>rate` (float64,
    `features.convert.to_float` — коректно округлений double рядка біржі). Рушій хешує їх разом зі
    свічками і специфікацією інструмента (RF-02): `dataset_hash({**candle_arrays.columns(),
    **funding_columns(rates), ...instrument_spec})`."""
    t = np.array([r.funding_time_ms * NS_PER_MS for r in rates], dtype=np.int64)
    rate = np.array([to_float(r.funding_rate) for r in rates], dtype=np.float64)
    return {f"{prefix}t_ns": t, f"{prefix}rate": rate}
