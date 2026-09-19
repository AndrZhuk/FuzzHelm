"""Спільні фікстури тестів FuzzHelm.

НЕ редагувати з паралельних задач — допоміжне кладіть у tests/helpers/.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from hypothesis import HealthCheck, settings

from fuzzhelm.core.money import setup_decimal_context

# Однопотокові BLAS/OpenMP у тестах (QP-03): задачі тут малі (KMeans на ~1.5 тис. точок, MLP 8-3-8), і потоки
# лише конкурують за ядра — із пулами процесів тестів сітки (кожен воркер успадкував би 8 потоків OpenMP).
# Виміряно на повному типовому наборі: CPU user 37 → 30 с, sys 7 → 1 с, стіна −0.3 с; жоден тест не змінив
# результату (KMeans і так відтворюється лише до ~1e−15, DATA-08). setdefault: явне значення середовища
# головніше. Стоїть до першого імпорту numpy/sklearn: імпорти вище їх не тягнуть, тестові модулі — пізніше.
for _var in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS"):
    os.environ.setdefault(_var, "1")

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
CONFIG = ROOT / "config"

settings.register_profile("default", deadline=None, suppress_health_check=[HealthCheck.too_slow])
settings.register_profile("ci", deadline=None, max_examples=200, suppress_health_check=[HealthCheck.too_slow])
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "default"))


@pytest.fixture(autouse=True)
def _decimal_ctx() -> None:
    setup_decimal_context()


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES


@pytest.fixture
def config_dir() -> Path:
    return CONFIG
