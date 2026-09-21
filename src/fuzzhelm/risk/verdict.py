"""Алгебра вердиктів ризик-контуру: ALLOW / SHRINK(f) / VETO з монотонною композицією.

Найменування: risk/verdict.py
Призначення: інваріант «жодне ризик-правило не може збільшити експозицію»
як властивість алгебри, а не як умова в коді.
Автор: Андрій Жук, 2026.

    Verdict ∈ {ALLOW, SHRINK(f), VETO},  f ∈ (0; 1) строго
    compose(v₁..vₙ) = VETO,          якщо ∃i: vᵢ = VETO
                    = SHRINK(Π fᵢ),  інакше, якщо є хоч один SHRINK
                    = ALLOW,         інакше
    exposure(v, q)  = q (ALLOW) | ⌊f·q⌋ (SHRINK) | 0 (VETO)

Інваріант: exposure(compose(V), q) ≤ minᵢ exposure(vᵢ, q) ≤ q.

Чому множник зберігається як Fraction, а не Decimal: добуток Decimal із prec=38 округлюється на
кожному кроці, тож (a·b)·c і a·(b·c) можуть розійтися в 38-му знаку — композиція формально
втратила б асоціативність і комутативність. Раціональний добуток точний, тому (Verdict, compose) —
точний комутативний моноїд з одиницею ALLOW і поглинаючим елементом VETO. У Decimal множник
переводиться лише на виході і лише з округленням ВНИЗ (floor), тож округлення ніколи не збільшує
експозицію.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from fuzzhelm.core.enums import VerdictKind
from fuzzhelm.core.money import D0, dec

if TYPE_CHECKING:
    from fuzzhelm.risk.context import RiskContext

FRACTION_SCALE = 18                        # масштаб NUMERIC(38,18)
_ONE = Fraction(1)
_ZERO = Fraction(0)


def floor_fraction(x: Fraction, scale: int = FRACTION_SCALE) -> Decimal:
    """⌊x·10^scale⌋·10^(−scale) як точний Decimal (без контексту округлення, для x ≥ 0 і x < 0)."""
    if x.denominator == 1:
        return dec(x.numerator)                          # цілі (зокрема 0 і 1) — без форматування
    n = (x.numerator * 10**scale) // x.denominator      # // — floor і для від'ємних
    return dec(f"{n}E-{scale}") if scale > 0 else dec(n)


def to_fraction(x: Decimal | Fraction | int | str) -> Fraction:
    """Точне раціональне значення; float відкидається (float → Decimal лише через sizing.convert)."""
    if isinstance(x, bool | float):
        raise TypeError(f"verdict factor refuses {type(x).__name__}; pass Decimal, Fraction, int or str")
    if isinstance(x, Fraction):
        return x
    if isinstance(x, Decimal):
        if not x.is_finite():
            raise ValueError(f"non-finite factor {x}")
        return Fraction(x)
    if isinstance(x, int | str):
        return Fraction(x)
    raise TypeError(f"cannot use {type(x).__name__} as a verdict factor")


@dataclass(frozen=True, slots=True)
class Verdict:
    """Вердикт ризик-правила. `factor` — точний множник експозиції: ALLOW → 1, VETO → 0, SHRINK → (0; 1)."""

    kind: VerdictKind
    factor: Fraction

    def __post_init__(self) -> None:
        if not isinstance(self.factor, Fraction):
            raise TypeError("Verdict.factor must be a Fraction; use shrink(f) / ALLOW / VETO")
        if self.kind is VerdictKind.ALLOW and self.factor != _ONE:
            raise ValueError(f"ALLOW must have factor 1, got {self.factor}")
        if self.kind is VerdictKind.VETO and self.factor != _ZERO:
            raise ValueError(f"VETO must have factor 0, got {self.factor}")
        if self.kind is VerdictKind.SHRINK and not (_ZERO < self.factor < _ONE):
            raise ValueError(f"SHRINK factor must lie strictly in (0; 1), got {self.factor}")

    @property
    def factor_dec(self) -> Decimal:
        """Множник як Decimal, округлений ВНИЗ до 1e-18 (для журналу risk_event.factor)."""
        return floor_fraction(self.factor)

    def __str__(self) -> str:
        if self.kind is VerdictKind.SHRINK:
            return f"SHRINK({self.factor_dec.normalize()})"
        return self.kind.value


ALLOW = Verdict(VerdictKind.ALLOW, _ONE)
VETO = Verdict(VerdictKind.VETO, _ZERO)


def shrink(f: Decimal | Fraction | int | str) -> Verdict:
    """SHRINK(f), f ∈ (0; 1) строго — інакше ValueError (f=1 — це ALLOW, f=0 — це VETO)."""
    return Verdict(VerdictKind.SHRINK, to_fraction(f))


def compose(verdicts: Iterable[Verdict]) -> Verdict:
    """Композиція ланцюга правил. Порожній ланцюг → ALLOW (нейтральний елемент)."""
    product = _ONE
    shrunk = False
    for v in verdicts:
        if v.kind is VerdictKind.VETO:
            return VETO                              # поглинаючий елемент
        if v.kind is VerdictKind.SHRINK:
            product *= v.factor                      # точний добуток: f∈(0;1) ⇒ Π f∈(0;1)
            shrunk = True
    return Verdict(VerdictKind.SHRINK, product) if shrunk else ALLOW


def exposure(verdict: Verdict, requested: Decimal) -> Decimal:
    """Дозволена експозиція (кількість або номінал, ≥ 0) після вердикту; округлення лише вниз."""
    if requested < 0:
        raise ValueError(f"requested exposure must be >= 0 (use absolute size), got {requested}")
    if verdict.kind is VerdictKind.ALLOW:
        return requested
    if verdict.kind is VerdictKind.VETO:
        return D0
    return floor_fraction(Fraction(requested) * verdict.factor)


# ---------------------------------------------------------------- вердикт конкретного правила


@dataclass(frozen=True, slots=True)
class RuleVerdict:
    """Результат однієї перевірки: що вирішило правило, що побачило (observed) і з чим порівняло (limit).

    payload — канонічно-серіалізовні значення (Decimal/int/str/bool/None), бо йде в журнал і хеш-ланцюг.
    halt — сигнал «перевести автомат у HALTED і закрити все» (лише MaxDrawdownHalt).
    """

    rule: str
    verdict: Verdict
    observed: Decimal | None
    limit: Decimal | None
    payload: Mapping[str, Any] = field(default_factory=dict)
    halt: bool = False


@runtime_checkable
class RiskRule(Protocol):
    """Правило ризик-ланцюга: чиста функція від контексту (без годинника, без побічних ефектів)."""

    name: str

    def check(self, ctx: RiskContext) -> RuleVerdict: ...
