"""CI (брифінг §9), fly.toml і цілі Makefile: синтаксис і зміст перевіряються офлайн, нічого не запускається.

Найменування: tests/unit/test_wiring_ci_deploy.py
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import shutil
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def test_ci_workflow_runs_lint_tests_integration_and_audit() -> None:
    doc = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    jobs = doc["jobs"]
    assert set(jobs) == {"lint", "test", "integration", "audit"}

    def runs(job: str) -> str:
        return "\n".join(str(s.get("run", "")) for s in jobs[job]["steps"])

    for job in jobs.values():
        assert any(str(s.get("uses", "")).startswith("astral-sh/setup-uv@") for s in job["steps"])
        assert "uv sync --frozen" in "\n".join(str(s.get("run", "")) for s in job["steps"])
    lint = runs("lint")
    mypy = "mypy src/fuzzhelm/core src/fuzzhelm/fuzzy src/fuzzhelm/risk"
    assert lint.index("ruff check") < lint.index(mypy)
    # поріг покриття — у pyproject ([tool.coverage.report] fail_under), не в командному рядку
    test_runs = runs("test")
    assert "pytest -q" in test_runs and "--cov" in test_runs and "--cov-fail-under" not in test_runs
    # поріг ≥ 90 % на fuzzy/risk/decision/sizing (§10) перевіряється окремим кроком
    assert "tests.helpers.cov_packages" in test_runs
    pg = jobs["integration"]["services"]["postgres"]
    assert pg["image"] == "postgres:16" and pg["ports"] == ["5432:5432"]
    url = jobs["integration"]["env"]["FUZZHELM_TEST_DATABASE_URL"]
    assert url.startswith("postgresql+asyncpg://") and "@localhost:5432/" in url
    assert "-m integration" in runs("integration") and "unreachable" in runs("integration")
    assert "pip-audit" in runs("audit")
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["coverage"]["report"]["fail_under"] == 80


def test_fly_toml_serves_the_api_on_8000_with_the_liveness_probe() -> None:
    fly = tomllib.loads((ROOT / "fly.toml").read_text(encoding="utf-8"))
    http = fly["http_service"]
    assert http["internal_port"] == 8000 and http["checks"][0]["path"] == "/healthz"
    cmd = fly["processes"]["app"]
    assert "uvicorn fuzzhelm.api.main:app" in cmd and "--port 8000" in cmd
    assert "change-me" in fly["app"]                                   # плейсхолдер, деплой — крок людини
    text = (ROOT / "fly.toml").read_text(encoding="utf-8").lower()
    assert "secret =" not in text and "password" not in text           # секрети лише через fly secrets
    from fuzzhelm.api.main import create_app  # noqa: PLC0415

    assert "/healthz" in {getattr(r, "path", None) for r in create_app().routes}


@pytest.mark.skipif(shutil.which("make") is None, reason="make is not installed")
def test_makefile_targets_expand() -> None:
    targets = ("up", "down", "migrate", "ingest", "record", "replay", "backtest", "grid", "walkforward",
               "experiments", "verify", "test", "test-int", "cov", "lint", "audit", "report", "anomaly",
               "users", "backup")
    for t in targets:
        out = subprocess.run(["make", "-n", t], cwd=ROOT, capture_output=True, text=True, check=True).stdout
        assert out.strip(), t
    grid = subprocess.run(["make", "-n", "grid", "SYMBOL=ETHUSDT"], cwd=ROOT, capture_output=True, text=True,
                          check=True).stdout
    assert "scripts/run_grid.py --db --symbol ETHUSDT" in grid
    exp = subprocess.run(["make", "-n", "experiments"], cwd=ROOT, capture_output=True, text=True,
                         check=True).stdout
    assert "scripts/run_all_experiments.sh" in exp
