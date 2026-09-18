"""Генерація шести патологічних WS-сесій з 4-хвилинного зразка + REST-відповідей для офлайн-добору.

Найменування: make_pathological.py
Призначення: фаза 0/2 брифінгу —
fixtures/ws/pathological/{gap,dup,reorder,clock_jump,stall,flash_crash}.jsonl.gz (той самий формат, що й
записана сесія, docs/contracts.md §7) і поруч <назва>.rest.json — сирі рядки /fapi/v1/klines і
/fapi/v1/aggTrades, які імітація REST (ingest.replay.FixtureRestHandler) віддає під час добору прогалин.
З ключем --report додатково відтворює всі сесії через IngestPipeline з офлайн-добором і пише таблицю
docs/figures/ingest_pathological.md.
Автор: Андрій Жук, 2026.

Запуск:  uv run python scripts/make_pathological.py [--report]

Похідні файли побайтово відтворювані (фіксований seed, gzip mtime=0). Часові вікна задано від
хвилинних меж M0..M3 зразка (M0 — open_time першої свічки); для кожного сценарію межі збою пишуться
в <назва>.rest.json ("fault"), щоб звіт рахував час відновлення від фактичних моментів.

Джерела REST-відповідей (чесно):
  * klines: для gap/dup/reorder/clock_jump/stall — СПРАВЖНІ записані рядки REST
    (fixtures/rest/binance_klines.json.gz покриває хвилини зразка; побайтово збігаються з WS-закриттями
    x=true);
    для flash_crash — похідні зі спотворених кадрів x=true (обвал синтетичний, REST такого не бачив);
  * aggTrades: REST-запис не робився, тож рядки {a,p,q,f,l,T,m} взято з WS-кадрів aggTrade тієї самої
    сесії — це ті самі поля й значення, що повертає /fapi/v1/aggTrades для тих самих id.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
from collections.abc import Callable, Sequence
from dataclasses import replace
from decimal import ROUND_HALF_EVEN, Decimal
from itertools import pairwise
from pathlib import Path
from typing import Any

import numpy as np
import orjson

from fuzzhelm.core.money import dec, dec_str, quantize_price
from fuzzhelm.ingest.recorder import ControlRecord, RawFrame, SessionItem, write_session
from fuzzhelm.ingest.replay import FrameClock, RestFixture, agg_trade_rest_row, read_session

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "fixtures" / "ws" / "sample_btcusdt_4m.jsonl.gz"
OUT = ROOT / "fixtures" / "ws" / "pathological"
REST_KLINES = ROOT / "fixtures" / "rest" / "binance_klines.json.gz"
REPORT = ROOT / "docs" / "figures" / "ingest_pathological.md"
SCENARIOS = ("gap", "dup", "reorder", "clock_jump", "stall", "flash_crash")
TICK = Decimal("0.10")
SEED = 20260918
S = 1_000_000_000
NS_PER_MS = 1_000_000

# глибина і форма синтетичного обвалу: −45 % за 45 с, утримання, відновлення за ~2 хв
CRASH_DEPTH = 0.45


def _is_kline(fr: RawFrame) -> bool:
    return "@kline_" in fr.stream


def _minutes(frames: Sequence[RawFrame]) -> list[int]:
    return sorted({int(f.data["k"]["t"]) * NS_PER_MS for f in frames if _is_kline(f)})


def _first_after(items: Sequence[SessionItem], t: int, pred: Callable[[RawFrame], bool]) -> int | None:
    return next((i.ts_ingest_ns for i in items
                 if isinstance(i, RawFrame) and pred(i) and i.ts_ingest_ns >= t), None)


# ---------------------------------------------------------------- сценарії


def make_gap(items: list[SessionItem], m: list[int]) -> tuple[list[SessionItem], dict[str, Any]]:
    """Втрата кадрів kline+aggTrade на ~90 с (з'єднання не мовчить: markPrice і depth ідуть далі) —
    ловиться лише детекторами послідовності/часу, не сторожем тиші."""
    lo, hi = m[0] + 45 * S, m[2] + 15 * S

    def lost(fr: RawFrame) -> bool:
        return fr.conn == "market" and ("@kline_" in fr.stream or fr.stream.endswith("@aggTrade")) \
            and lo <= fr.ts_ingest_ns < hi

    out = [i for i in items if not (isinstance(i, RawFrame) and lost(i))]
    dropped = len(items) - len(out)
    return out, {"kind": "drop kline+aggTrade frames", "start_ns": lo, "end_ns": hi,
                 "dropped_frames": dropped,
                 "resume_ns": _first_after(out, hi, lambda f: f.stream.endswith("@aggTrade"))}


def make_dup(items: list[SessionItem], m: list[int]) -> tuple[list[SessionItem], dict[str, Any]]:
    """Дублікати: кожен 5-й кадр market, кожен 10-й public, кожне закриття x=true, і «повторна доставка»
    пачки кадрів [M2−3 с, M2+5 с) о M2+5 с (як сервер після пересубскрипції)."""
    out: list[SessionItem] = []
    n_market = n_public = dups = 0
    burst_lo, burst_hi = m[2] - 3 * S, m[2] + 5 * S
    burst = [i for i in items if isinstance(i, RawFrame) and i.conn == "market"
             and burst_lo <= i.ts_ingest_ns < burst_hi]
    burst_done = False
    for k, it in enumerate(items):
        if not burst_done and it.ts_ingest_ns >= burst_hi:
            out.extend(replace(b, ts_ingest_ns=it.ts_ingest_ns) for b in burst)
            dups += len(burst)
            burst_done = True
        out.append(it)
        if not isinstance(it, RawFrame):
            continue
        again = False
        if it.conn == "market":
            n_market += 1
            again = n_market % 5 == 0 or (_is_kline(it) and bool(it.data["k"]["x"]))
        else:
            n_public += 1
            again = n_public % 10 == 0
        if again:
            # повтор надходить на 1 мкс пізніше (але не пізніше за наступний кадр — порядок файлу неспадний)
            nxt = items[k + 1].ts_ingest_ns if k + 1 < len(items) else it.ts_ingest_ns + 1_000
            out.append(replace(it, ts_ingest_ns=min(it.ts_ingest_ns + 1_000, nxt)))
            dups += 1
    return out, {"kind": "duplicate frames + redelivery burst", "start_ns": burst_lo, "end_ns": burst_hi,
                 "duplicated_frames": dups, "burst_frames": len(burst)}


def make_reorder(items: list[SessionItem], m: list[int]) -> tuple[list[SessionItem], dict[str, Any]]:
    """Локальні перестановки: вміст (stream+data) сусідніх кадрів одного з'єднання міняється місцями,
    мітки ts_ingest лишаються на місцях (порядок надходження — інший за порядок подій біржі).
    Кожне закриття x=true обов'язково переставлено з наступним кадром market."""
    rng = np.random.default_rng(SEED)
    out = list(items)
    idx = {c: [k for k, i in enumerate(out) if isinstance(i, RawFrame) and i.conn == c]
           for c in ("market", "public")}
    swaps = forced = 0
    for conn, prob in (("market", 0.2), ("public", 0.1)):
        pos = idx[conn]
        j = 0
        while j + 1 < len(pos):
            a, b = pos[j], pos[j + 1]
            fa, fb = out[a], out[b]
            assert isinstance(fa, RawFrame) and isinstance(fb, RawFrame)
            must = conn == "market" and _is_kline(fa) and bool(fa.data["k"]["x"])
            if must or rng.random() < prob:
                out[a] = replace(fa, stream=fb.stream, data=fb.data)
                out[b] = replace(fb, stream=fa.stream, data=fa.data)
                swaps += 1
                forced += int(must)
                j += 2
            else:
                j += 1
    return out, {"kind": "adjacent payload swaps", "start_ns": m[0], "end_ns": m[-1] + 60 * S, "swaps": swaps,
                 "forced_close_swaps": forced}


def make_clock_jump(items: list[SessionItem], m: list[int]) -> tuple[list[SessionItem], dict[str, Any]]:
    """Локальний годинник стрибає на +40 с о M1+10 с і назад о M2+10 с: ts_ingest усіх кадрів у вікні
    зсунуто, порядок надходження не змінено. Біржові часи (E, T) не зачеплено."""
    lo, hi, jump = m[1] + 10 * S, m[2] + 10 * S, 40 * S
    out: list[SessionItem] = [
        replace(i, ts_ingest_ns=i.ts_ingest_ns + jump) if lo <= i.ts_ingest_ns < hi else i for i in items]
    return out, {"kind": "wall-clock jump +40s / -40s", "start_ns": lo, "end_ns": hi, "jump_ns": jump}


def make_stall(items: list[SessionItem], m: list[int]) -> tuple[list[SessionItem], dict[str, Any]]:
    """«Напівмертве» з'єднання market: 55 с жодного кадру (без close-кадру і без записів керування),
    public (depth) працює — розрив має помітити окремий сторож тиші саме цього з'єднання."""
    lo, hi = m[1] + 30 * S, m[2] + 25 * S
    out = [i for i in items
           if not (isinstance(i, RawFrame) and i.conn == "market" and lo <= i.ts_ingest_ns < hi)]
    return out, {"kind": "silent market connection", "start_ns": lo, "end_ns": hi,
                 "dropped_frames": len(items) - len(out),
                 "resume_ns": _first_after(out, hi, lambda f: f.conn == "market")}


class Crash:
    """Кусково-лінійний множник ціни f(t): 1 → 1−depth (падіння) → утримання → 1 (відновлення)."""

    def __init__(self, m: list[int], depth: float = CRASH_DEPTH) -> None:
        ms = [x // NS_PER_MS for x in m]
        self.pts = [(ms[1] + 5_000, 1.0), (ms[1] + 50_000, 1.0 - depth), (ms[2] + 15_000, 1.0 - depth),
                    (ms[3] + 15_000, 1.0)]

    def f(self, t_ms: int) -> Decimal:
        (t0, _), (t1, low), (t2, _), (t3, _) = self.pts
        if t_ms <= t0 or t_ms >= t3:
            x = 1.0
        elif t_ms < t1:
            x = 1.0 - (1.0 - low) * (t_ms - t0) / (t1 - t0)
        elif t_ms < t2:
            x = low
        else:
            x = low + (1.0 - low) * (t_ms - t2) / (t3 - t2)
        # множник як Decimal з рядка: жодного float у цінах
        return dec(f"{x:.9f}")

    def range(self, a_ms: int, b_ms: int) -> tuple[Decimal, Decimal]:
        """(min f, max f) на [a, b]: для кусково-лінійної f — у кінцях або вузлах усередині."""
        vals = [self.f(a_ms), self.f(b_ms)] + [self.f(t) for t, _ in self.pts if a_ms < t < b_ms]
        return min(vals), max(vals)


def _q(x: Decimal, like: str) -> str:
    """Квантувати до масштабу вихідного рядка біржі (напр. '23575.49940' → 5 знаків)."""
    exp = Decimal(1).scaleb(-len(like.split(".")[1])) if "." in like else Decimal(1)
    return dec_str(x.quantize(exp, rounding=ROUND_HALF_EVEN))


def _crash_kline(k: dict[str, Any], e_ms: int, cr: Crash) -> dict[str, Any]:
    t_open = int(k["t"])
    f_lo, f_hi = cr.range(t_open, e_ms)
    o = quantize_price(dec(k["o"]) * cr.f(t_open), TICK)
    c = quantize_price(dec(k["c"]) * cr.f(e_ms), TICK)
    # межі через екстремуми множника на інтервалі хвилини; квантування монотонне ⇒ h ≥ max(o,c), l ≤ min(o,c)
    h = quantize_price(max(dec(k["h"]) * f_hi, o, c), TICK)
    lo = quantize_price(min(dec(k["l"]) * f_lo, o, c), TICK)
    v, bv = dec(k["v"]), dec(k["V"])
    mid = (cr.f(t_open) + cr.f(e_ms)) / 2
    q = min(max(dec(k["q"]) * mid, v * lo), v * h)
    bq = min(max(dec(k["Q"]) * mid, bv * lo), bv * h, q)
    return k | {"o": dec_str(o), "h": dec_str(h), "l": dec_str(lo), "c": dec_str(c), "q": _q(q, k["q"]),
                "Q": _q(bq, k["Q"])}


def _crash_frame(fr: RawFrame, cr: Crash) -> RawFrame:
    d = dict(fr.data)
    if _is_kline(fr):
        d["k"] = _crash_kline(dict(d["k"]), int(d["E"]), cr)
    elif fr.stream.endswith("@aggTrade"):
        d["p"] = dec_str(quantize_price(dec(d["p"]) * cr.f(int(d["T"])), TICK))
    elif "@markPrice" in fr.stream:
        f = cr.f(int(d["E"]))
        for key in ("p", "ap", "P", "i"):
            if key in d:
                d[key] = _q(dec(d[key]) * f, d[key])
    elif "@depth" in fr.stream:
        f = cr.f(int(d["T"]))
        bids, asks = d["b"], d["a"]
        if f != 1 and bids and asks:
            mid = (dec(bids[0][0]) + dec(asks[0][0])) / 2
            # адитивний зсув зберігає крок між рівнями (множення стиснуло б їх і злило після квантування)
            shift = quantize_price(mid * (f - 1), TICK)
            d["b"] = [[dec_str(dec(p) + shift), q] for p, q in bids]
            d["a"] = [[dec_str(dec(p) + shift), q] for p, q in asks]
    return replace(fr, data=d)


def make_flash_crash(items: list[SessionItem], m: list[int]) -> tuple[list[SessionItem], dict[str, Any]]:
    cr = Crash(m)
    out = [_crash_frame(i, cr) if isinstance(i, RawFrame) else i for i in items]
    return out, {"kind": f"synthetic flash crash -{int(CRASH_DEPTH * 100)}% and recovery",
                 "start_ns": cr.pts[0][0] * NS_PER_MS, "end_ns": cr.pts[-1][0] * NS_PER_MS,
                 "profile_ms_factor": [[t, v] for t, v in cr.pts]}


MAKERS: dict[str, Callable[[list[SessionItem], list[int]], tuple[list[SessionItem], dict[str, Any]]]] = {
    "gap": make_gap, "dup": make_dup, "reorder": make_reorder, "clock_jump": make_clock_jump,
    "stall": make_stall, "flash_crash": make_flash_crash,
}


# ---------------------------------------------------------------- REST-відповіді


def _closed_rows_from_frames(items: Sequence[SessionItem]) -> list[list[Any]]:
    rows: dict[int, list[Any]] = {}
    for i in items:
        if isinstance(i, RawFrame) and _is_kline(i) and i.data["k"]["x"]:
            k = i.data["k"]
            rows[int(k["t"])] = [k["t"], k["o"], k["h"], k["l"], k["c"], k["v"], k["T"], k["q"], k["n"],
                                 k["V"], k["Q"], "0"]
    return [rows[t] for t in sorted(rows)]


def _agg_rows(items: Sequence[SessionItem]) -> list[dict[str, Any]]:
    seen: dict[int, dict[str, Any]] = {}
    for i in items:
        if isinstance(i, RawFrame) and i.stream.endswith("@aggTrade"):
            seen.setdefault(int(i.data["a"]), agg_trade_rest_row(i.data))
    return [seen[a] for a in sorted(seen)]


def rest_payload(name: str, clean: list[SessionItem], derived: list[SessionItem], m: list[int],
                 meta: dict[str, Any]) -> dict[str, Any]:
    if name == "flash_crash":
        klines = _closed_rows_from_frames(derived)
        k_src = "похідні з кадрів x=true цієї сесії (синтетичний обвал; справжній REST такого не бачив)"
        trades = _agg_rows(derived)
    else:
        rest = orjson.loads(gzip.decompress(REST_KLINES.read_bytes()))
        lo_ms, hi_ms = m[0] // NS_PER_MS, m[-1] // NS_PER_MS
        klines = [r for r in rest if lo_ms <= r[0] <= hi_ms]
        k_src = "fixtures/rest/binance_klines.json.gz (записані рядки /fapi/v1/klines)"
        trades = _agg_rows(clean)
    return {"scenario": name, "symbol": "BTCUSDT", "interval": "1m",
            "derived_from": "fixtures/ws/sample_btcusdt_4m.jsonl.gz", "fault": meta,
            "klines_source": k_src,
            "aggTrades_source": "поля {a,p,q,f,l,T,m} WS-кадрів aggTrade (REST aggTrades не записувався)",
            "klines": klines, "aggTrades": trades}


def generate() -> dict[str, dict[str, Any]]:
    sess = read_session(SRC)
    clean = list(sess.items)
    m = _minutes(sess.frames)
    assert len(m) >= 4, "sample must span >= 4 kline minutes"
    OUT.mkdir(parents=True, exist_ok=True)
    metas: dict[str, dict[str, Any]] = {}
    for name in SCENARIOS:
        derived, meta = MAKERS[name](clean, m)
        ts = [i.ts_ingest_ns for i in derived]
        meta["frames"] = sum(1 for i in derived if isinstance(i, RawFrame))
        meta["controls"] = sum(1 for i in derived if isinstance(i, ControlRecord))
        meta["ingest_ts_regressions"] = sum(1 for a, b in pairwise(ts) if b < a)
        header = dict(sess.header)
        ended = sess.footer["ended_ns"] if sess.footer else ts[-1]
        write_session(OUT / f"{name}.jsonl.gz", header, derived, ended_ns=ended)
        payload = rest_payload(name, clean, derived, m, meta)
        (OUT / f"{name}.rest.json").write_bytes(orjson.dumps(payload) + b"\n")
        metas[name] = meta
        print(f"[pathological] {name}: {meta['frames']} frames, fault={meta['kind']}")
    return metas


# ---------------------------------------------------------------- звіт (реплей з офлайн-добором)


async def _run(name: str) -> tuple[Any, Any, dict[str, Any]]:
    from fuzzhelm.ingest.normalize import normalize_exchange_info  # noqa: PLC0415
    from fuzzhelm.ingest.pipeline import (  # noqa: PLC0415
        RestBackfiller,
        SessionCapture,
        offline_rest_client,
        recovery_stats,
        replay_session,
        session_reference,
    )

    inst = normalize_exchange_info(json.loads((ROOT / "fixtures/rest/exchange_info.json").read_text()),
                                   ["BTCUSDT"])["BTCUSDT"]
    ws = OUT / f"{name}.jsonl.gz"
    fx = RestFixture.load(OUT / f"{name}.rest.json")
    clock = FrameClock()
    cap = SessionCapture()
    async with offline_rest_client(fx, clock) as client:
        rep = await replay_session(ws, inst, backfill=RestBackfiller(client, inst), sinks=cap.sinks(),
                                   clock=clock)
    ref = session_reference(ws if name == "flash_crash" else SRC)
    return recovery_stats(ref, cap, rep), rep, fx.meta["fault"]


def _s(ns: int | None) -> str:
    return "—" if ns is None else f"{ns / S:.3f}"


def write_report() -> str:
    lines = [
        "# Патологічні WS-сесії: відновлення з нульовою втратою подій",
        "",
        "Згенеровано `scripts/make_pathological.py --report` (реплей кожної сесії через `IngestPipeline`,",
        "сторож тиші 10 с, grace 2 с, офлайн-REST з `<назва>.rest.json`). Автор: Андрій Жук, 2026.",
        "",
        "«Втрачено» — закриті свічки / aggTrade id еталона (сирі кадри чистого зразка; для flash_crash —",
        "самої сесії), яких немає на виході конвеєра; «дублів» — повторні випуски тієї самої свічки / угоди.",
        "Час — ВІДТВОРЕНИЙ (ts_ingest кадрів), від початку вікна збою. REST у реплеї відповідає миттєво",
        "(віртуальний час не рухається під час запиту), тож «усе добрано» = момент виявлення останньої",
        "прогалини; у live до нього додається RTT REST-запиту (тут не вимірювався).",
        "",
        "| сценарій | вікно збою, с | втрачено свічок / угод | дублів свічок / угод | розбіжних OHLCV | "
        "прогалин (статус) | спрацювань сторожа | перше виявлення, с | усе добрано, с | "
        "від відновлення потоку, с |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in SCENARIOS:
        st, _rep, fault = asyncio.run(_run(name))
        lo = int(fault["start_ns"])
        span = (int(fault["end_ns"]) - lo) / S
        det = None if st.first_detect_ns is None else st.first_detect_ns - lo
        done = None if st.last_closed_ns is None else st.last_closed_ns - lo
        resume = fault.get("resume_ns")
        after = None if (st.last_closed_ns is None or resume is None) else st.last_closed_ns - int(resume)
        status = ", ".join(f"{k}×{v}" for k, v in st.gap_status.items()) or "—"
        lines.append(
            f"| {name} | {span:.1f} | {st.lost_candles} / {st.lost_trades} | {st.duplicated_candles} / "
            f"{st.duplicated_trades} | {st.mismatched_candles} | {st.gaps} ({status}) | "
            f"{st.watchdog_fires} | "
            f"{_s(det)} | {_s(done)} | {_s(after)} |")
        print(f"[report] {name}: zero_loss={st.zero_loss} gaps={st.gap_status} wd={st.watchdog_fires} "
              f"expected={st.expected_candles}c/{st.expected_trades}t")
    lines += [
        "",
        "Примітки:",
        "* gap — втрачено лише kline+aggTrade (markPrice і depth ідуть): сторож тиші мовчить, свічки",
        "  виявляються за біржовим часом інших потоків ще ДО відновлення kline, угоди — за стрибком",
        "  aggTrade id.",
        "* stall — з'єднання market мовчить 55 с: сторож спрацьовує через 10 с тиші; свічку, закриття якої",
        "  загубилось, виявлено за часом потоку depth (/public/stream), угоди — після відновлення.",
        "* clock_jump — стрибок локального годинника на +40 с дає хибне спрацювання сторожа на кожному",
        "  з'єднанні (тиша за ts_ingest), але даних не втрачено і хибних прогалин немає: детектори працюють",
        "  за біржовими ідентифікаторами/часом, а не за ts_ingest. У live сторож варто вести за монотонним",
        "  годинником.",
        "* dup / reorder — дублікати відкидає дедуплікатор за event_uid, перестановки «заростають» у межах",
        "  grace; хибних прогалин 0.",
        "* flash_crash — обвал −45 % не є збоєм транспорту: усі свічки валідні (OHLC узгоджений), жодна не",
        "  відкинута як невалідна; еталон — кадри x=true самої сесії.",
        "",
    ]
    text = "\n".join(lines)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="replay all sessions and write the markdown report")
    ap.add_argument("--no-generate", action="store_true",
                    help="only rebuild the report from existing fixtures")
    a = ap.parse_args()
    if not a.no_generate:
        generate()
    if a.report:
        print(write_report())


if __name__ == "__main__":
    main()
