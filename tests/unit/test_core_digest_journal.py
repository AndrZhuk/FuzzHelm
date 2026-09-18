"""Група B брифінгу (§10): канонічна серіалізація, дайджести, журнал із ланцюгом хешів, event_uid.

Найменування: tests/unit/test_core_digest_journal.py
Призначення: перевірити фундамент core (digest/money/journal) незалежними від реалізації засобами —
ручними золотими байтами, прямим hashlib, окремими процесами з іншим PYTHONHASHSEED.
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

import dataclasses
import hashlib
import itertools
import json
import os
import subprocess
import sys
import textwrap
from decimal import Decimal
from uuid import UUID

import numpy as np
import pytest

from fuzzhelm.core.digest import canonical_json, event_uid, hex_digest
from fuzzhelm.core.errors import JournalIntegrityError
from fuzzhelm.core.journal import (
    GENESIS,
    EventJournal,
    JournalEntry,
    assert_chain,
    entry_hash,
    verify_chain,
)
from fuzzhelm.core.money import dec_str, quantize_money
from fuzzhelm.ingest.normalize import kline_uid
from fuzzhelm.ingest.symbols import BTC_USDT_PERP

RUN_ID = UUID("00000000-0000-4000-8000-000000000001")

# Стан, який будується ОДНАКОВО в цьому процесі й у дочірніх (текст виконується exec-ом в обох).
# frozenset рядків — навмисно: порядок його обходу залежить від PYTHONHASHSEED, тож тест має «зуби».
STATE_SRC = textwrap.dedent("""
    from decimal import Decimal
    from uuid import UUID
    from fuzzhelm.core.enums import Side, Src, Venue
    tags = frozenset(f"tag-{i:02d}-{chr(0x0430 + i)}" for i in range(16))
    state = {
        "run": UUID("00000000-0000-4000-8000-000000000001"),
        "equity": Decimal("100000.00"), "cash": Decimal("-12.345000"),
        "positions": [{"inst": "BTC-USDT-PERP", "qty": Decimal("0.123"), "side": Side.LONG}],
        "venue": Venue.BINANCE_USDM, "src": Src.REST,
        "zeta": {"b": 2, "a": 1, "ціна": Decimal("81015.40")},
        "tags": tags,
    }
""")

CHILD = STATE_SRC + textwrap.dedent("""
    import json
    from fuzzhelm.core.digest import hex_digest
    print(json.dumps({"digest": hex_digest(state), "raw_order": list(tags)}))
""")


def _run_child(code: str, hashseed: str) -> dict[str, object]:
    env = {**os.environ, "PYTHONHASHSEED": hashseed}
    out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True,
                         timeout=60)
    return json.loads(out.stdout)


# ---------------------------------------------------------------- B1


def test_canonical_json_rejects_float() -> None:
    with pytest.raises(TypeError, match=r"float is forbidden"):
        canonical_json(1.5)
    with pytest.raises(TypeError, match=r"\$\.a\.b\[1\]"):
        canonical_json({"a": {"b": [Decimal("1"), 2.0]}})
    with pytest.raises(TypeError, match="float is forbidden"):
        canonical_json({"x": (1, (2, float("nan")))})
    # numpy.float64 — підклас float, тому теж мусить бути відкинутий
    with pytest.raises(TypeError, match="float is forbidden"):
        canonical_json({"sigma": np.float64(0.25)})

    @dataclasses.dataclass
    class WithFloat:
        price: float

    with pytest.raises(TypeError, match="float is forbidden"):
        canonical_json(WithFloat(price=1.0))
    # те саме число як Decimal-рядок — дозволене; bool/int/None — не float
    assert canonical_json({"p": Decimal("1.5"), "ok": True, "n": 3, "z": None}) == \
        b'{"n":3,"ok":true,"p":"1.5","z":null}'


# ---------------------------------------------------------------- B2


def test_decimal_quantized_not_normalized() -> None:
    hundred = Decimal("1E+2")                       # саме так Decimal('100').normalize() виглядає
    assert Decimal("100").normalize() == hundred
    assert str(hundred) == "1E+2"                   # наївний str() дав би експоненту в хеші
    assert canonical_json({"amount": quantize_money(hundred)}) == b'{"amount":"100.00"}'
    assert canonical_json({"amount": quantize_money(Decimal("100"))}) == b'{"amount":"100.00"}'
    assert dec_str(hundred) == "100"
    for d in (Decimal("1E+2"), Decimal("1.20E-7"), Decimal("-5E+3"), Decimal("123456789E+10")):
        blob = canonical_json({"d": d})
        assert b"E" not in blob and b"e" not in blob.replace(b'"d"', b"")


# ---------------------------------------------------------------- B3


def test_keys_sorted_lexicographically() -> None:
    a = {"b": 1, "a": 2, "A": 3, "aa": 4, "ціна": 5, "Z": 6, "é": 7, "\U0001f600": 8, "～": 9,
         "nested": {"y": 1, "x": {"k2": 0, "k1": 0}}}
    b = dict(reversed(list(a.items())))            # той самий вміст, інший порядок вставки
    blob = canonical_json(a)
    assert blob == canonical_json(b)
    decoded = json.loads(blob)
    assert list(decoded) == sorted(a)               # порядок кодових точок Unicode
    assert list(decoded["nested"]) == ["x", "y"]
    assert list(decoded["nested"]["x"]) == ["k1", "k2"]
    # U+FF5E < U+1F600 за кодовими точками, але в UTF-16 (JS/Java) 😀 (сурогат D83D) йшов би першим
    assert blob.index("～".encode()) < blob.index("\U0001f600".encode())
    assert blob.startswith(b'{"A":3,"Z":6,"a":2,"aa":4,"b":1,')


# ---------------------------------------------------------------- B4


def test_state_digest_stable_across_processes() -> None:
    ns: dict[str, object] = {}
    exec(STATE_SRC, ns)  # фіксований текст із цього ж файлу
    here = hex_digest(ns["state"])
    c1 = _run_child(CHILD, "0")
    c2 = _run_child(CHILD, "4242")
    # контроль «зубів»: сирий порядок обходу frozenset справді різний у двох процесах
    assert c1["raw_order"] != c2["raw_order"]
    assert c1["digest"] == c2["digest"] == here


# ---------------------------------------------------------------- B5


def test_hash_chain_links_prev_hash() -> None:
    j = EventJournal(RUN_ID)
    e0 = j.append("fill", {"px": Decimal("81015.40"), "side": "LONG"}, ts_event_ns=1000, ts_ingest_ns=2000)
    # золоті байти формули з core/journal.py:
    # hash_0 = BLAKE2b-256(GENESIS ‖ canonical([seq, te, ti, kind, payload]))
    golden = b'[0,1000,2000,"fill",{"px":"81015.40","side":"LONG"}]'
    assert e0.prev_hash == GENESIS == bytes(32)
    assert e0.hash == hashlib.blake2b(GENESIS + golden, digest_size=32).digest()
    for i in range(1, 6):
        j.append("tick", {"i": i, "c": Decimal(f"{i}.10")}, ts_event_ns=1000 + i, ts_ingest_ns=2000 + i)
    es = j.entries
    for prev, cur in itertools.pairwise(es):
        assert cur.prev_hash == prev.hash
        assert cur.hash == hashlib.blake2b(
            prev.hash + canonical_json([cur.seq, cur.ts_event_ns, cur.ts_ingest_ns, cur.kind, cur.payload]),
            digest_size=32).digest()
    assert [e.seq for e in es] == list(range(6))
    assert j.head == es[-1].hash
    assert verify_chain(es) is None


# ---------------------------------------------------------------- B6


def _journal(n: int) -> list[JournalEntry]:
    j = EventJournal(RUN_ID)
    for i in range(n):
        j.append("candle", {"i": i, "c": Decimal(f"81000.{i}0")}, ts_event_ns=10 * i, ts_ingest_ns=10 * i + 3)
    return j.entries


def test_tampered_payload_breaks_chain_at_exact_seq() -> None:
    es = _journal(8)
    for k in range(len(es)):
        tampered = list(es)
        tampered[k] = dataclasses.replace(es[k], payload={**es[k].payload, "c": Decimal("1.00")})
        assert verify_chain(tampered) == k
        with pytest.raises(JournalIntegrityError) as ei:
            assert_chain(tampered)
        assert ei.value.seq == k
    # зміна часу чи виду події — теж розрив рівно на своєму seq
    assert verify_chain([*es[:3], dataclasses.replace(es[3], ts_event_ns=999), *es[4:]]) == 3
    assert verify_chain([*es[:5], dataclasses.replace(es[5], kind="fill"), *es[6:]]) == 5


def test_rehashed_tampering_is_caught_at_next_seq_or_by_head_anchor() -> None:
    """Чесна межа гарантії: якщо зловмисник перерахує hash зміненого запису k, розрив видно на k+1;
    переписаний ОСТАННІЙ запис ланцюг сам не виявить — потрібен зовнішній якір (journal_head_hash у run)."""
    es = _journal(6)
    for k in range(len(es) - 1):
        forged = dataclasses.replace(es[k], payload={"i": -1})
        rehash = entry_hash(forged.prev_hash, forged.seq, forged.ts_event_ns, forged.ts_ingest_ns,
                            forged.kind, forged.payload)
        forged = dataclasses.replace(forged, hash=rehash)
        assert verify_chain([*es[:k], forged, *es[k + 1:]]) == k + 1
    last = dataclasses.replace(es[-1], payload={"i": -1})
    last = dataclasses.replace(last, hash=entry_hash(last.prev_hash, last.seq, last.ts_event_ns,
                                                     last.ts_ingest_ns, last.kind, last.payload))
    rewritten = [*es[:-1], last]
    assert verify_chain(rewritten) is None           # ланцюг сам по собі цілий…
    assert rewritten[-1].hash != es[-1].hash          # …але голова не збігається з якорем


# ---------------------------------------------------------------- B7


UID_KEYS = [
    ("BINANCE_USDM", "klines", "BTC-USDT-PERP", "1m", 1789758540000000000),
    ("BINANCE_USDM", "trades", "BTC-USDT-PERP", 3455864431),
    ("BINANCE_USDM", "depth", "BTC-USDT-PERP", 11593007760348),
    ("BINANCE_USDM", "mark", "BTC-USDT-PERP", 1789758541001000000),
]
# Золоті значення, обчислені незалежно: blake2b(json_compact(key), digest_size=16). Зміна формату
# event_uid між версіями коду зламає дедуплікацію вже записаних даних — тому значення зафіксовано літерально.
UID_GOLDEN = {
    "klines": "eef5ed93ca36971e0932ebf4e1109f2c",
}


def test_event_uid_stable_across_restart() -> None:
    here = [event_uid(*k) for k in UID_KEYS]
    for k, uid in zip(UID_KEYS, here, strict=True):
        raw = json.dumps(list(k), separators=(",", ":"), ensure_ascii=False).encode()
        assert uid == hashlib.blake2b(raw, digest_size=16).hexdigest()
    assert here[0] == UID_GOLDEN["klines"]
    # ingest використовує саме цей природний ключ (contracts §1)
    assert kline_uid(BTC_USDT_PERP.venue, "BTC-USDT-PERP", "1m", 1789758540000000000) == here[0]
    code = ("import json; from fuzzhelm.core.digest import event_uid; "
            f"print(json.dumps([event_uid(*k) for k in {UID_KEYS!r}]))")
    for seed in ("0", "31337"):
        env = {**os.environ, "PYTHONHASHSEED": seed}
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True,
                             check=True, timeout=60)
        assert json.loads(out.stdout) == here
