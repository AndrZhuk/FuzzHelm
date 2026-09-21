"""Одне визначення «брудного» коду для всіх паспортів прогонів (WIRE-03; XS-11, XA-19, W-11).

Найменування: tests/unit/test_wiring_git_state.py
Автор: Андрій Жук, 2026.

«Код брудний» = незакомічені зміни поза `artifacts/` і `docs/` (backtest.manifest.read_git_state). Перевірка —
у тимчасовому git-репозиторії (без мережі); плюс контейнерний шлях FUZZHELM_GIT_SHA і те, що паспорти API,
торгового воркера і scripts/run_backtest.py беруть саме це визначення.
"""

from __future__ import annotations

import ast
import subprocess
from pathlib import Path

import pytest

from fuzzhelm.backtest import manifest as manifest_mod
from fuzzhelm.backtest.manifest import read_git_sha, read_git_state

ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *a: str) -> None:
    subprocess.run(["git", "-c", "user.email=t@example.com", "-c", "user.name=t",
                    "-c", "commit.gpgsign=false", *a], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_container_without_git_takes_sha_from_env_and_leaves_dirty_unknown(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def no_git(*_: object, **__: object) -> None:
        raise FileNotFoundError("git")

    monkeypatch.setattr(manifest_mod.subprocess, "run", no_git)
    monkeypatch.setenv("FUZZHELM_GIT_SHA", "A" * 40)
    st = read_git_state(tmp_path)
    assert (st.sha, st.dirty, st.dirty_any, st.source) == ("a" * 40, None, None, "env")
    monkeypatch.setenv("FUZZHELM_GIT_SHA", "not-a-sha")
    assert read_git_state(tmp_path).sha is None and read_git_sha(tmp_path) == (None, None)


async def test_api_runner_passport_uses_code_dirty_and_keeps_paths(repo: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    from fuzzhelm.api.backtest_runner import DbBacktestRunner  # noqa: PLC0415

    real = read_git_state
    monkeypatch.setattr(manifest_mod, "read_git_state", lambda: real(repo))
    runner = DbBacktestRunner(None, data_dir=repo)  # type: ignore[arg-type]  # _git БД не чіпає
    (repo / "docs").mkdir()
    (repo / "docs" / "x.md").write_text("x", encoding="utf-8")
    sha, dirty = await runner._git()
    assert len(sha or "") == 40 and dirty is False and runner.git_dirty_paths == ()
    (repo / "src" / "m.py").write_text("x = 3\n", encoding="utf-8")
    assert (await runner._git())[1] is True and any("src/m.py" in p for p in runner.git_dirty_paths)
    off = DbBacktestRunner(None, data_dir=repo, git=False)  # type: ignore[arg-type]
    assert await off._git() == (None, None)


def test_run_passports_take_the_single_code_dirty_definition() -> None:
    """Паспорти прогонів (POST /backtests, торговий воркер, scripts/run_backtest.py) читають
    manifest.read_git_state, а не сирий read_git_sha."""
    for rel in ("src/fuzzhelm/api/backtest_runner.py", "src/fuzzhelm/workers/trading_worker.py",
                "scripts/run_backtest.py"):
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        names = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
                 and n.module == "fuzzhelm.backtest.manifest" for a in n.names}
        assert "read_git_state" in names and "read_git_sha" not in names, rel
