"""Україномовне трасування рішення для /explain, ExplainView і звіту.

Найменування: decision/narrative_uk.py
Призначення: перетворити `DecisionTrace` на читабельний текст у стилі демо-фрази (брифінг §15):
«Спрацювало правило R07 з α = 0.62: ЯКЩО тренд СИЛЬНЕ_ЗРОСТАННЯ І реверсія НЕМА_ТИСКУ
І волатильність ПОМІРНА ТО сигнал СИЛЬНИЙ_ЛОНГ.»
Автор: Андрій Жук, 2026.

Формат чисел: α, T/R/V, p, H, A_g, κ, u, κ_mode — 2 знаки після десяткової крапки '.'
(незалежно від локалі); кількості та спостереження ризик-правил — без втрати точності
(Decimal — канонічний рядок без експоненти, float — до 8 знаків без хвостових нулів).
"""

from __future__ import annotations

import numbers
from collections.abc import Mapping, Sequence
from enum import Enum
from typing import Any

from fuzzhelm.core.money import dec_str
from fuzzhelm.decision.trace import DecisionTrace
from fuzzhelm.fuzzy.base import FiredRule

VAR_NAMES: dict[str, str] = {"T": "тренд", "R": "реверсія", "V": "волатильність", "U": "сигнал"}
ANTECEDENT_ORDER: tuple[str, ...] = ("T", "R", "V")
ANY_TERM = "any"
ANY_UK = "БУДЬ-ЯКА"

TERMS_UK: dict[str, dict[str, str]] = {
    "T": {
        "STRONG_DOWN": "СИЛЬНЕ_ПАДІННЯ",
        "WEAK_DOWN": "ПОМІРНЕ_ПАДІННЯ",
        "NEUTRAL": "БЕЗ_ТРЕНДУ",
        "WEAK_UP": "ПОМІРНЕ_ЗРОСТАННЯ",
        "STRONG_UP": "СИЛЬНЕ_ЗРОСТАННЯ",
    },
    "R": {
        "SELL_PRESSURE": "ТИСК_ПРОДАВЦІВ",
        "NO_PRESSURE": "НЕМА_ТИСКУ",
        "BUY_PRESSURE": "ТИСК_ПОКУПЦІВ",
    },
    "V": {"LO": "НИЗЬКА", "MID": "ПОМІРНА", "HI": "ВИСОКА"},
    "U": {
        "STRONG_SHORT": "СИЛЬНИЙ_ШОРТ",
        "SHORT": "ШОРТ",
        "HOLD": "УТРИМАННЯ",
        "LONG": "ЛОНГ",
        "STRONG_LONG": "СИЛЬНИЙ_ЛОНГ",
    },
}

BINDING_UK: dict[str, str] = {
    "ATR_RISK": "ризик на ATR",
    "VOL_TARGET": "таргет волатильності",
    "LEVERAGE": "ліміт плеча",
}
RISK_STATE_UK: dict[str, str] = {
    "NORMAL": "НОРМА",
    "WARNING": "ПОПЕРЕДЖЕННЯ",
    "COOLDOWN": "ОХОЛОДЖЕННЯ",
    "HALTED": "ЗУПИНЕНО",
}
VERDICT_UK: dict[str, str] = {"ALLOW": "дозволено", "SHRINK": "зменшено", "VETO": "заборонено"}
OTHER_RULES_LIMIT = 3


# --- форматування -------------------------------------------------------------------------------

def f2(x: float) -> str:
    """2 знаки, крапка; «-0.00» нормалізується до «0.00»."""
    s = f"{x:.2f}"
    return "0.00" if s == "-0.00" else s


def fnum(x: Any) -> str:
    """Кількість/спостереження без втрати точності і без експоненти."""
    if isinstance(x, Enum):
        x = x.value
    if isinstance(x, bool) or x is None:
        return str(x)
    if isinstance(x, int):
        return str(x)
    if isinstance(x, float):
        s = f"{x:.8f}".rstrip("0").rstrip(".")
        return "0" if s in ("-0", "") else s
    if isinstance(x, numbers.Number) and not isinstance(x, numbers.Complex):
        return dec_str(x)  # type: ignore[arg-type]  # Decimal — структурно, без імпорту decimal
    if isinstance(x, numbers.Real):
        return fnum(float(x))
    return str(x)


def _plain(x: Any) -> str:
    """Значення Enum або рядок."""
    return str(x.value) if isinstance(x, Enum) else str(x)


def term_uk(var: str, term: str) -> str:
    if term == ANY_TERM:
        return ANY_UK
    return TERMS_UK.get(var, {}).get(term, term)


def _plural_rules(n: int) -> str:
    """Узгодження іменника «правило» з числівником."""
    if n % 10 == 1 and n % 100 != 11:
        return "правило"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "правила"
    return "правил"


# --- речення ------------------------------------------------------------------------------------

def rule_sentence(rule: FiredRule) -> str:
    """Головне речення в стилі демо: «Спрацювало правило R07 з α = 0.62: ЯКЩО … ТО сигнал …»."""
    ante = rule.antecedent
    keys = [k for k in ANTECEDENT_ORDER if k in ante] + sorted(k for k in ante if k not in ANTECEDENT_ORDER)
    clauses = " І ".join(f"{VAR_NAMES.get(k, k)} {term_uk(k, ante[k])}" for k in keys)
    return (
        f"Спрацювало правило {rule.rule_id} з α = {f2(rule.alpha)}: "
        f"ЯКЩО {clauses} ТО {VAR_NAMES['U']} {term_uk('U', rule.consequent)}."
    )


def _brief_rule(rule: FiredRule) -> str:
    return f"{rule.rule_id} (α = {f2(rule.alpha)}, {VAR_NAMES['U']} {term_uk('U', rule.consequent)})"


def _other_rules_sentence(others: Sequence[FiredRule]) -> str | None:
    if not others:
        return None
    shown = others[:OTHER_RULES_LIMIT]
    rest = len(others) - len(shown)
    if len(shown) == 1:
        text = f"Також спрацювало правило {_brief_rule(shown[0])}"
    else:
        text = "Також спрацювали правила " + ", ".join(_brief_rule(r) for r in shown)
    if rest > 0:
        text += f"; ще {rest} {_plural_rules(rest)} з меншою активацією"
    return text + "."


def _consensus_sentence(trace: DecisionTrace) -> str:
    c = trace.consensus
    text = (
        f"Консенсус детекторів: {VAR_NAMES['T']} T = {f2(c.T)}, {VAR_NAMES['R']} R = {f2(c.R)}, "
        f"{VAR_NAMES['V']} V = {f2(c.V)}"
    )
    if c.v_source == "default":
        text += " (V — апріорне значення: детектор режиму волатильності ще не дав рангу)"
    return text + "."


def _agreement_sentence(trace: DecisionTrace) -> str:
    a = trace.agreement
    if not a.has_evidence:
        return (
            "Трендові й реверсійні детектори не дали свідчень (сумарна довіра практично нульова), "
            f"тому κ = κ_min = {f2(a.kappa_min)} — максимальне гасіння сигналу."
        )
    text = (
        f"Узгодженість детекторів: p₊ = {f2(a.p_plus)}, p₋ = {f2(a.p_minus)}, p₀ = {f2(a.p_zero)}; "
        f"ентропія H = {f2(a.H)}, A_g = {f2(a.A_g)}, отже коефіцієнт довіри κ = {f2(a.kappa)}"
    )
    if a.kappa >= 1.0:
        text += " (повна згода, без приглушення)"
    elif f2(a.kappa) == "0.00":
        text += " (сигнал практично погашено)"      # κ_min = 0 і повний розкол
    else:
        text += f" (сигнал приглушено у {f2(1.0 / a.kappa)} раза)"
    return text + "."


def _direction(u: float) -> str:
    # напрям узгоджено з надрукованим числом: чисельний шум центроїда (~1e−19) при «u_final = 0.00»
    # не повинен звучати як «у бік лонгу»
    if f2(u) == "0.00":
        return "нейтральний"
    return "у бік лонгу" if u > 0.0 else "у бік шорту"


def _output_sentence(trace: DecisionTrace) -> str:
    return (
        f"Вихід нечіткого ядра u_raw = {f2(trace.u_raw)}; після множення на κ = {f2(trace.kappa)} "
        f"намір u_final = {f2(trace.u_final)} ({_direction(trace.u_final)})."
    )


def _sizing_sentence(sizing: Mapping[str, Any]) -> str:
    parts: list[str] = []
    binding = sizing.get("binding_constraint")
    if binding is not None:
        code = _plain(binding)
        parts.append(f"обмежувальний чинник — {BINDING_UK.get(code, code)} ({code})")
    qs = [f"{k} = {fnum(sizing[k])}" for k in ("q_atr", "q_vt", "q_lev") if sizing.get(k) is not None]
    if qs:
        parts.append(", ".join(qs))
    if sizing.get("kappa_mode") is not None:
        parts.append(f"κ_mode = {f2(float(sizing['kappa_mode']))}")
    if sizing.get("qty") is not None:
        parts.append(f"кількість — {fnum(sizing['qty'])}")
    text = "Сайзер: " + ("; ".join(parts) if parts else "дані розрахунку відсутні")
    reject = sizing.get("reject_code")
    if reject is not None:
        text += f"; ордер відхилено з кодом {_plain(reject)}"
    return text + "."


def _verdict_text(verdict: Any) -> str:
    if isinstance(verdict, Mapping):
        kind, factor = verdict.get("kind"), verdict.get("factor")
    elif isinstance(verdict, str | Enum):
        kind, factor = verdict, None
    else:
        kind, factor = getattr(verdict, "kind", verdict), getattr(verdict, "factor", None)
    code = _plain(kind)
    text = f"вердикт — {VERDICT_UK.get(code, code)} ({code})"
    if code == "SHRINK" and factor is not None:
        text += f" ×{fnum(factor)}"
    return text


def _veto_text(v: Any) -> str:
    if not isinstance(v, Mapping):
        if not hasattr(v, "rule"):
            return _plain(v)
        # об'єкт-запис ризик-правила (risk.verdict.RuleVerdict: rule, observed, limit)
        v = {k: getattr(v, k, None) for k in ("rule", "observed", "limit")}
    name = _plain(v.get("rule", v.get("name", "?")))
    details = []
    if v.get("observed") is not None:
        details.append(f"спостережено {fnum(v['observed'])}")
    if v.get("limit") is not None:
        details.append(f"ліміт {fnum(v['limit'])}")
    return f"{name} ({', '.join(details)})" if details else name


def _risk_sentence(risk: Mapping[str, Any]) -> str:
    parts: list[str] = []
    state = risk.get("state")
    if state is not None:
        code = _plain(state)
        parts.append(f"стан {RISK_STATE_UK.get(code, code)} ({code})")
    if risk.get("kappa_mode") is not None:
        parts.append(f"κ_mode = {f2(float(risk['kappa_mode']))}")
    if risk.get("verdict") is not None:
        parts.append(_verdict_text(risk["verdict"]))
    if "vetoes" in risk:
        vetoes = risk.get("vetoes") or ()
        parts.append("вето: " + ", ".join(_veto_text(v) for v in vetoes) if vetoes else "вето немає")
    if risk.get("approved_qty") is not None:
        parts.append(f"дозволена кількість — {fnum(risk['approved_qty'])}")
    return "Ризик-контур: " + ("; ".join(parts) if parts else "дані перевірки відсутні") + "."


def narrate(trace: DecisionTrace) -> str:
    """Українське трасування: головне правило з α, інші правила, консенсус, κ, u, сайзер, ризик."""
    sentences: list[str] = []
    fired = trace.fired_rules
    # рушій без бази правил (LinearVoteEngine: memberships = {}, fired = ()) — порожній fired у нього
    # не означає «жодне правило не спрацювало», навіть якщо u_raw = 0
    rule_free = not fired and (trace.u_raw != 0.0 or not trace.fuzzy.memberships)
    if fired:
        sentences.append(rule_sentence(fired[0]))
        other = _other_rules_sentence(fired[1:])
        if other is not None:
            sentences.append(other)
    elif rule_free:
        sentences.append(
            f"Рушій «{trace.engine}» не використовує бази правил: його вихід u_raw = {f2(trace.u_raw)}."
        )
    else:
        sentences.append(
            "Жодне правило не спрацювало (усі ступені активації α = 0), тому u_raw = 0.00 "
            f"і рішення — {term_uk('U', 'HOLD')}."
        )
    sentences.append(_consensus_sentence(trace))
    sentences.append(_agreement_sentence(trace))
    if fired or rule_free:
        sentences.append(_output_sentence(trace))
    if trace.sizing is not None:
        sentences.append(_sizing_sentence(trace.sizing))
    if trace.risk is not None:
        sentences.append(_risk_sentence(trace.risk))
    return " ".join(sentences)
