"""Налаштування сервісу (лише з оточення/.env) і завантаження YAML-конфігурацій.

Найменування: config.py
Автор: Андрій Жук, 2026.

ТВЕРДА ЗАБОРОНА MAINNET: хост виконання мусить входити до ALLOWED_TESTNET_HOSTS, а джерела
ринкових даних — до ALLOWED_READONLY_HOSTS. Порушення → MainnetHostRejected ще на етапі
побудови Settings (тест tests/arch/test_mainnet_allowlist.py).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from fuzzhelm.core.errors import ConfigValidationError, MainnetHostRejected

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
FIXTURES_DIR = ROOT / "fixtures"

# Єдині хости, куди дозволено надсилати ордери (Binance Futures testnet / demo).
ALLOWED_TESTNET_HOSTS: frozenset[str] = frozenset({
    "testnet.binancefuture.com",
    "demo-fapi.binance.com",
})
# Публічні read-only джерела ринкових даних (ордерів туди не шлемо за побудовою).
ALLOWED_READONLY_HOSTS: frozenset[str] = frozenset({
    "fapi.binance.com",
    "fstream.binance.com",
}) | ALLOWED_TESTNET_HOSTS | frozenset({"stream.binancefuture.com", "fstream.binancefuture.com"})


def host_of(url: str) -> str:
    host = urlparse(url).hostname
    if not host:
        raise MainnetHostRejected(f"cannot parse host from {url!r}")
    return host.lower()


def assert_testnet_url(url: str) -> str:
    host = host_of(url)
    if host not in ALLOWED_TESTNET_HOSTS:
        raise MainnetHostRejected(f"execution host {host!r} is not an allowed testnet host")
    return url


def assert_readonly_url(url: str) -> str:
    host = host_of(url)
    if host not in ALLOWED_READONLY_HOSTS:
        raise MainnetHostRejected(f"market-data host {host!r} is not allow-listed")
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="FUZZHELM_", env_file=".env", extra="ignore")

    # --- сховище
    database_url: str = "postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm"

    # --- виконання: лише симуляція (PaperBroker). Хост нижче — захисна межа: навіть у конфігурації
    # неможливо вказати основну мережу біржі (перевіряє assert_testnet_url).
    venue_base_url: str = "https://testnet.binancefuture.com"

    # --- публічні ринкові дані (read-only)
    binance_rest_base: str = "https://fapi.binance.com"
    binance_ws_market: str = "wss://fstream.binance.com/market/stream"   # kline/aggTrade/markPrice
    binance_ws_public: str = "wss://fstream.binance.com/public/stream"   # depth

    # --- API/безпека
    jwt_secret: SecretStr = SecretStr("dev-only-change-me")
    jwt_ttl_hours: int = 8

    # --- відтворюваність
    seed: int = 20260918
    config_dir: Path = CONFIG_DIR
    fixtures_dir: Path = FIXTURES_DIR
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT")

    @field_validator("venue_base_url")
    @classmethod
    def _testnet_only(cls, v: str) -> str:
        return assert_testnet_url(v)

    @field_validator("binance_rest_base", "binance_ws_market", "binance_ws_public")
    @classmethod
    def _readonly_only(cls, v: str) -> str:
        return assert_readonly_url(v)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_yaml(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    """Прочитати config/<name> (з розширенням або без). Порожній файл → ConfigValidationError."""
    base = config_dir or CONFIG_DIR
    path = base / name
    if not path.suffix:
        path = path.with_suffix(".yaml")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ConfigValidationError(f"YAML parse error: {e}", path=str(path.name)) from e
    if not isinstance(data, dict):
        raise ConfigValidationError("top-level YAML must be a mapping", path=str(path.name))
    return data


def parse_yaml_text(text: str, name: str = "<text>") -> dict[str, Any]:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigValidationError(f"YAML parse error: {e}", path=name) from e
    if not isinstance(data, dict):
        raise ConfigValidationError("top-level YAML must be a mapping", path=name)
    return data
