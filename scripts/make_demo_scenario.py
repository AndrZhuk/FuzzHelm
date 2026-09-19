"""Стрес-сценарій flash_crash для демо ризик-контуру (§15, акт 2:30–3:20; чек-лист §17).

Найменування: make_demo_scenario.py
Призначення: на основі записаної 45-хвилинної сесії fixtures/ws/btcusdt_2026-09-18.jsonl.gz побудувати
    fixtures/ws/scenarios/flash_crash.jsonl.gz (той самий формат сесії, docs/contracts.md §7) і поруч
    flash_crash.meta.json (профіль обвалу, момент ін'єкції, хеші джерел, обґрунтування).
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/make_demo_scenario.py [--check]

Дизайн (чесно, це стрес-тест, а не відтворення історичної події):
  1. Спершу чиста сесія проганяється тим самим торговим шляхом, що й демо (профіль replay: прогрів з
     fixtures/rest, IngestPipeline + TradingLoop). Момент ін'єкції t₀ — межа хвилини, на яку стратегія
     ВХОДИТЬ, тримаючи довгу позицію (рішення попереднього бару — hold): стрес-тест ризик-контуру має
     сенс лише тоді, коли є що втрачати. Кадри до t₀ не змінюються, тож рішення до t₀ побітово ті самі
     (у рушії немає зазирання вперед — тест test_shuffling_future_bars_does_not_change_past_decisions).
  2. Від t₀ усі ціни (kline OHLC, aggTrade, markPrice, стакан) множаться на кусково-лінійний профіль
     f(t): розрив −6.5 % саме на межі хвилини (перша угода хвилини вже на 0.935·P — каскад ліквідацій),
     далі до −11 % за 20 с, відскок до −7.5 % до кінця хвилини, відновлення до −3 % за 10 хв і далі
     рівень −3 %. Розрив на межі хвилини — суть тесту: у барній моделі брокера стоп усередині бару
     виконується за рівнем (оптимістично), тож лише розрив крізь стоп перевіряє припущення «про виконання
     стопа», на яке спирається оцінка гіршого випадку §5.12.
  3. OHLC кожного кадру kline узгоджений (h ≥ max(o,c), l ≤ min(o,c), quote volume ∈ [v·l, v·h]), ціни на
     сітці tick 0.10 — сесія проходить ті самі інваріанти якості, що й справжня.
Файли побайтово відтворювані (gzip mtime = 0); --check будує сценарій у тимчасовій теці й перевіряє, що
закомічені файли (сценарій і .meta.json) збігаються побайтово, нічого не перезаписуючи.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

from fuzzhelm.backtest.dataset import load_exchange_instrument
from fuzzhelm.core.enums import RunKind
from fuzzhelm.core.money import dec, dec_str, quantize_price
from fuzzhelm.ingest.recorder import RawFrame, SessionItem, write_session
from fuzzhelm.ingest.replay import read_session
from fuzzhelm.workers.trading_worker import (
    MemorySink,
    TradingSession,
    WorkerProfile,
    first_kline_open_ns,
    offline_warmup_history,
)

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "fixtures" / "ws" / "btcusdt_2026-09-18.jsonl.gz"
OUT_DIR = ROOT / "fixtures" / "ws" / "scenarios"
NAME = "flash_crash"
TICK = Decimal("0.10")
S_MS = 1_000
MIN_MS = 60_000
NS_PER_MS = 1_000_000

# профіль множника цін: (мс від t₀, множник); до t₀ — 1.0, у t₀ — розрив (f права-неперервна)
PROFILE: tuple[tuple[int, str], ...] = (
    (0, "0.935"),              # розрив −6.5 % на межі хвилини
    (20 * S_MS, "0.890"),      # каскад до −11 %
    (60 * S_MS, "0.925"),      # відскок до −7.5 % до кінця першої хвилини
    (10 * MIN_MS, "0.970"),    # відновлення до −3 % за 10 хв
)


class Crash:
    def __init__(self, t0_ms: int, profile: tuple[tuple[int, str], ...] = PROFILE) -> None:
        self.t0 = t0_ms
        self.pts = [(t0_ms + dt, dec(v)) for dt, v in profile]

    def f(self, t_ms: int) -> Decimal:
        if t_ms < self.t0:
            return Decimal(1)
        pts = self.pts
        if t_ms >= pts[-1][0]:
            return pts[-1][1]
        for (ta, va), (tb, vb) in pairwise(pts):
            if ta <= t_ms < tb:
                # лінійна інтерполяція в Decimal (без float у цінах), 9 знаків множника
                x = va + (vb - va) * Decimal(t_ms - ta) / Decimal(tb - ta)
                return x.quantize(Decimal("1E-9"), rounding=ROUND_HALF_EVEN)
        return pts[-1][1]  # pragma: no cover

    def range(self, a_ms: int, b_ms: int) -> tuple[Decimal, Decimal]:
        """(min f, max f) на [a, b]: кусково-лінійна f — екстремуми в кінцях і вузлах усередині."""
        vals = [self.f(a_ms), self.f(b_ms)] + [v for t, v in self.pts if a_ms < t < b_ms]
        if a_ms < self.t0 <= b_ms:
            vals.append(Decimal(1))
        return min(vals), max(vals)


def _q(x: Decimal, like: str) -> str:
    exp = Decimal(1).scaleb(-len(like.split(".")[1])) if "." in like else Decimal(1)
    return dec_str(x.quantize(exp, rounding=ROUND_HALF_EVEN))


def crash_kline(k: dict[str, Any], e_ms: int, cr: Crash) -> dict[str, Any]:
    t_open = int(k["t"])
    # свічка покриває угоди [open, min(E, close)]: фінальне оновлення хвилини біржа шле вже ПІСЛЯ її межі
    # (E > close), але угод з наступної хвилини в ньому немає — множник береться за часом угод
    e_ms = min(e_ms, t_open + MIN_MS - 1)
    f_lo, f_hi = cr.range(t_open, e_ms)
    o = quantize_price(dec(k["o"]) * cr.f(t_open), TICK)
    c = quantize_price(dec(k["c"]) * cr.f(e_ms), TICK)
    h = quantize_price(max(dec(k["h"]) * f_hi, o, c), TICK)
    lo = quantize_price(min(dec(k["l"]) * f_lo, o, c), TICK)
    v, bv = dec(k["v"]), dec(k["V"])
    mid = (cr.f(t_open) + cr.f(e_ms)) / 2
    q = min(max(dec(k["q"]) * mid, v * lo), v * h)
    bq = min(max(dec(k["Q"]) * mid, bv * lo), bv * h, q)
    return k | {"o": dec_str(o), "h": dec_str(h), "l": dec_str(lo), "c": dec_str(c), "q": _q(q, k["q"]),
                "Q": _q(bq, k["Q"])}


def crash_frame(fr: RawFrame, cr: Crash) -> RawFrame:
    d = dict(fr.data)
    if "@kline_" in fr.stream:
        if int(d["k"]["t"]) < cr.t0:
            return fr                       # хвилини до t₀ — побайтово як у записі
        d["k"] = crash_kline(dict(d["k"]), int(d["E"]), cr)
    elif fr.stream.endswith("@aggTrade"):
        f = cr.f(int(d["T"]))
        if f == 1:
            return fr
        d["p"] = dec_str(quantize_price(dec(d["p"]) * f, TICK))
    elif "@markPrice" in fr.stream:
        f = cr.f(int(d["E"]))
        if f == 1:
            return fr
        for key in ("p", "ap", "P", "i"):
            if key in d:
                d[key] = _q(dec(d[key]) * f, d[key])
    elif "@depth" in fr.stream:
        f = cr.f(int(d["T"]))
        bids, asks = d["b"], d["a"]
        if f == 1 or not bids or not asks:
            return fr
        mid = (dec(bids[0][0]) + dec(asks[0][0])) / 2
        # адитивний зсув зберігає крок між рівнями (множення стиснуло б їх і злило після квантування)
        shift = quantize_price(mid * (f - 1), TICK)
        d["b"] = [[dec_str(dec(p) + shift), q] for p, q in bids]
        d["a"] = [[dec_str(dec(p) + shift), q] for p, q in asks]
    return replace(fr, data=d)


async def find_injection_point() -> dict[str, Any]:
    """Прогнати ЧИСТУ сесію торговим шляхом демо і знайти першу хвилину, яку стратегія починає в лонгу."""
    prof = WorkerProfile.load("replay")
    cfg = prof.backtest_config(check_invariants=True)
    inst = load_exchange_instrument("BTCUSDT")
    n = cfg.resolved_warmup()
    first_open = first_kline_open_ns(SRC)
    warm = await offline_warmup_history(prof.history or ROOT / "fixtures/rest/binance_klines.json.gz", inst,
                                          first_open, n)
    from uuid import UUID  # noqa: PLC0415

    sink = MemorySink()
    sess = TradingSession(inst, cfg, seed=prof.seed, run_id=UUID(int=0), kind=RunKind.REPLAY, feed="REPLAY",
                          warmup_bars=n, sink=sink, streams=prof.streams,
                          heartbeat_timeout_s=prof.heartbeat_timeout_s)
    await sess.begin({"purpose": "find flash_crash injection point"})
    sess.warm_up(warm)
    from fuzzhelm.ingest.replay import iter_items  # noqa: PLC0415

    await sess.run(iter_items(SRC, prof.streams))
    for out in sink.steps:
        sr = out.step
        d = sr.decision
        if sr.position_qty > 0 and d is not None and d.action == "hold":
            entry = next(o.step for o in sink.steps if o.step.opened_positions)
            return {"t0_ns": sr.close_time_ns + NS_PER_MS, "long_qty": str(sr.position_qty),
                    "entry_price": str(entry.opened_positions[0].avg_entry),
                    "stop_price": str(entry.opened_positions[0].stop_price),
                    "equity_before": str(sr.equity), "config_hash": cfg.config_hash}
    raise SystemExit("clean session never holds a long position into a new minute — nothing to stress")


def build(point: dict[str, Any], *, name: str = NAME, out_dir: Path = OUT_DIR,
          profile: tuple[tuple[int, str], ...] = PROFILE) -> tuple[Path, dict[str, Any]]:
    t0_ms = point["t0_ns"] // NS_PER_MS
    cr = Crash(t0_ms, profile)
    sess = read_session(SRC)
    items: list[SessionItem] = [crash_frame(i, cr) if isinstance(i, RawFrame) else i for i in sess.items]
    header = dict(sess.header) | {"scenario": name, "derived_from": SRC.name}
    footer = sess.footer or {}
    out_dir.mkdir(parents=True, exist_ok=True)
    out = write_session(out_dir / f"{name}.jsonl.gz", header, items,
                        ended_ns=int(footer.get("ended_ns", header["started_ns"])), deterministic=True)
    meta = {
        "scenario": name, "derived_from": f"fixtures/ws/{SRC.name}",
        "derived_from_sha256": hashlib.sha256(SRC.read_bytes()).hexdigest(),
        "sha256": hashlib.sha256(out.read_bytes()).hexdigest(),
        "injection": point, "profile_ms_from_t0": [[dt, v] for dt, v in profile],
        "design": ("stress test, not a historical replay: at the minute boundary where the strategy enters "
                   "holding a long position, prices gap −6.5 %, cascade to −11 % in 20 s, rebound to −7.5 %, "
                   "recover to −3 % over 10 min; frames before t0 are byte-identical to the recording"),
        "generator": "scripts/make_demo_scenario.py",
    }
    if profile != PROFILE:
        meta["design"] = f"variant of {NAME} with a custom price profile (see profile_ms_from_t0)"
    (out_dir / f"{name}.meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                                               encoding="utf-8")
    return out, meta


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="regenerate and compare with the previous file")
    ap.add_argument("--name", default=NAME, help="scenario name (variants: write them outside fixtures)")
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    ap.add_argument("--profile", default=None,
                    help="price profile 'ms:factor,...' from t0, e.g. 0:0.955,20000:0.93,600000:0.975")
    args = ap.parse_args(argv)
    profile = PROFILE if args.profile is None else tuple(
        (int(a), b) for a, b in (x.split(":", 1) for x in args.profile.split(",")))
    target = args.out_dir / f"{args.name}.jsonl.gz"
    point = asyncio.run(find_injection_point())
    print(f"injection at t0 = {point['t0_ns']} ns (long {point['long_qty']} BTC, stop {point['stop_price']})")
    if args.check:
        # перевірка нічого не перезаписує: сценарій будується в тимчасовій теці й порівнюється із закоміченим
        with tempfile.TemporaryDirectory() as tmp:
            out, meta = build(point, name=args.name, out_dir=Path(tmp), profile=profile)
            same = target.is_file() and target.read_bytes() == out.read_bytes()
            meta_same = (target.with_name(f"{args.name}.meta.json").is_file()
                         and target.with_name(f"{args.name}.meta.json").read_text(encoding="utf-8")
                         == (Path(tmp) / f"{args.name}.meta.json").read_text(encoding="utf-8"))
        print(f"rebuilt sha256 {meta['sha256'][:16]}… vs {target}")
        print("byte-identical to the committed file:", same, "| meta identical:", meta_same)
        return 0 if same and meta_same else 1
    out, meta = build(point, name=args.name, out_dir=args.out_dir, profile=profile)
    print(f"wrote {out} sha256 {meta['sha256'][:16]}…")
    return 0


if __name__ == "__main__":
    sys.exit(main())
