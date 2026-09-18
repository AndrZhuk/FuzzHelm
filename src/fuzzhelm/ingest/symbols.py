"""Мапінг символів бірж на канонічні символи FuzzHelm.

Найменування: ingest/symbols.py
Призначення: єдине місце, де venue-символ (`BTCUSDT`, `XBTUSD`, `XXBTZUSD`) стає канонічним
(`BTC-USDT-PERP`, `BTC-USD-SPOT`); канонічний символ входить у event_uid, тому мапінг фіксований.
Автор: Андрій Жук, 2026.

Формат канонічного символу: `<BASE>-<QUOTE>-<SPOT|PERP>`. Kraken історично називає біткоїн `XBT`
(актив `XXBT`, котирувальний долар `ZUSD`), тому для крос-звірки потрібні аліаси.
"""

from __future__ import annotations

from dataclasses import dataclass

from fuzzhelm.core.enums import ContractType, Venue
from fuzzhelm.core.errors import NormalizationError


@dataclass(frozen=True, slots=True)
class SymbolRef:
    """Мінімальна ідентичність інструмента, достатня для нормалізації подій (без tick/step)."""

    venue: Venue
    symbol_venue: str
    symbol_canon: str
    base_asset: str
    quote_asset: str
    contract_type: ContractType


BTC_USDT_PERP = SymbolRef(Venue.BINANCE_USDM, "BTCUSDT", "BTC-USDT-PERP", "BTC", "USDT", ContractType.PERP)
ETH_USDT_PERP = SymbolRef(Venue.BINANCE_USDM, "ETHUSDT", "ETH-USDT-PERP", "ETH", "USDT", ContractType.PERP)
BTC_USD_SPOT = SymbolRef(Venue.KRAKEN, "XBTUSD", "BTC-USD-SPOT", "BTC", "USD", ContractType.SPOT)

REGISTRY: tuple[SymbolRef, ...] = (BTC_USDT_PERP, ETH_USDT_PERP, BTC_USD_SPOT)

_BY_VENUE: dict[tuple[Venue, str], SymbolRef] = {(r.venue, r.symbol_venue): r for r in REGISTRY}
_BY_CANON: dict[tuple[Venue, str], SymbolRef] = {(r.venue, r.symbol_canon): r for r in REGISTRY}

# Kraken повертає OHLC під «повною» назвою пари (XXBTZUSD), а приймає коротку (XBTUSD).
KRAKEN_RESULT_KEYS: dict[str, str] = {"XBTUSD": "XXBTZUSD"}
KRAKEN_ASSET_ALIASES: dict[str, str] = {"XBT": "BTC", "XXBT": "BTC", "ZUSD": "USD", "XETH": "ETH"}

# Пари для крос-звірки цін двох незалежних джерел (перп Binance ↔ спот Kraken).
CROSSCHECK_PAIRS: dict[str, str] = {"BTC-USDT-PERP": "BTC-USD-SPOT"}


def make_canonical(base: str, quote: str, contract_type: ContractType) -> str:
    return f"{base.upper()}-{quote.upper()}-{contract_type.value}"


def parse_canonical(symbol_canon: str) -> tuple[str, str, ContractType]:
    parts = symbol_canon.split("-")
    if len(parts) != 3 or not all(parts):
        raise NormalizationError(f"malformed canonical symbol {symbol_canon!r}", field="symbol_canon")
    try:
        ct = ContractType(parts[2])
    except ValueError as e:
        raise NormalizationError(f"unknown contract type in {symbol_canon!r}", field="symbol_canon") from e
    return parts[0], parts[1], ct


def symbol_ref(venue: Venue, symbol_venue: str) -> SymbolRef:
    """Venue-символ → SymbolRef. Для Kraken приймається і ключ результату (`XXBTZUSD`)."""
    key = symbol_venue.upper()
    if venue == Venue.KRAKEN:
        key = kraken_pair_from_result_key(key)
    ref = _BY_VENUE.get((venue, key))
    if ref is None:
        raise NormalizationError(f"unknown symbol {symbol_venue!r} for {venue}", field="symbol",
                                 venue=venue.value)
    return ref


def canonical_symbol(venue: Venue, symbol_venue: str) -> str:
    return symbol_ref(venue, symbol_venue).symbol_canon


def venue_symbol(venue: Venue, symbol_canon: str) -> str:
    ref = _BY_CANON.get((venue, symbol_canon))
    if ref is None:
        raise NormalizationError(f"no {venue} symbol for {symbol_canon!r}", field="symbol_canon",
                                 venue=venue.value)
    return ref.symbol_venue


def kraken_pair_from_result_key(key: str) -> str:
    """`XXBTZUSD` → `XBTUSD`; коротку назву повертає як є."""
    for short, long_key in KRAKEN_RESULT_KEYS.items():
        if key in (short, long_key):
            return short
    return key


def kraken_result_key(pair: str) -> str:
    return KRAKEN_RESULT_KEYS.get(pair.upper(), pair.upper())


def kraken_asset(asset: str) -> str:
    return KRAKEN_ASSET_ALIASES.get(asset, asset)
