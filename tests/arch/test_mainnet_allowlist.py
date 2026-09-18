"""A3. Mainnet недосяжний за побудовою: конфігурація відкидає будь-який не-testnet хост виконання."""

from __future__ import annotations

import pytest

from fuzzhelm.config import ALLOWED_TESTNET_HOSTS, Settings, assert_testnet_url, host_of
from fuzzhelm.core.errors import MainnetHostRejected


@pytest.mark.parametrize("url", [
    "https://fapi.binance.com",
    "https://api.binance.com",
    "https://fapi.binance.com.evil.example",
    "https://testnet.binancefuture.com.evil.example",
])
def test_mainnet_host_is_rejected_by_config(url: str) -> None:
    with pytest.raises(MainnetHostRejected):
        Settings(venue_base_url=url)
    with pytest.raises(MainnetHostRejected):
        assert_testnet_url(url)


def test_default_settings_point_to_testnet() -> None:
    s = Settings()
    assert host_of(s.venue_base_url) in ALLOWED_TESTNET_HOSTS
