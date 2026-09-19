"""RAM-замір: 45-денний бектест BTCUSDT у пам'яті (профіль backtest, без запису в БД)."""
import asyncio, resource, sys, time
from fuzzhelm.backtest.runner import load_db_window
from fuzzhelm.backtest.engine import BacktestConfig, run_backtest
from fuzzhelm.config import load_yaml

ds = asyncio.run(load_db_window("BTCUSDT"))
r0 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
cfg = BacktestConfig.from_profile("backtest")
seed = int(load_yaml("profiles/backtest")["seed"])
t = time.perf_counter()
res = run_backtest(ds, cfg, seed, git=False)
dt = time.perf_counter() - t
r1 = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(f"bars={len(ds)} engine_s={dt:.2f} equity_hash={res.manifest.equity_hash}")
print(f"maxrss_after_load_bytes={r0} maxrss_after_engine_bytes={r1}")
