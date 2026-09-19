"""Україномовні шаблони Telegram-повідомлень: сигнали, ризик-події, kill-switch, розриви WS, щоденний звіт.

Найменування: notify/templates.py
Призначення: один текстовий стиль для телефона оператора (демо §15: «Telegram-нотифікація на телефон»).
Назви термів, станів і вердиктів — ті самі словники, що й у decision.narrative_uk (узгодженість з /explain).
Звичайний текст без parse_mode: немає ризику ін'єкції розмітки з даних (символи, причини, логіни).
Автор: Андрій Жук, 2026.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from fuzzhelm.core.money import dec_str
from fuzzhelm.decision.narrative_uk import BINDING_UK, RISK_STATE_UK, VERDICT_UK, f2, term_uk

MAX_MESSAGE_CHARS = 4096  # межа Telegram Bot API для sendMessage
SIDE_UK = {1: "ЛОНГ", -1: "ШОРТ", 0: "БЕЗ ПОЗИЦІЇ"}
DISCONNECT_UK = {
    "network": "мережа",
    "server_close": "закрито сервером",
    "heartbeat": "мовчання (heartbeat)",
    "protocol": "помилка протоколу",
    "rate_limit": "обмеження частоти",
}


def _num(x: Decimal | float | int | None) -> str:
    if x is None:
        return "—"
    if isinstance(x, Decimal):
        return dec_str(x)
    if isinstance(x, int):
        return str(x)
    return f"{x:.8f}".rstrip("0").rstrip(".") or "0"


def clip(text: str) -> str:
    return text if len(text) <= MAX_MESSAGE_CHARS else text[: MAX_MESSAGE_CHARS - 1] + "…"


def signal_text(
    *,
    symbol: str,
    side: int,
    u_final: float,
    top_rule: str | None = None,
    alpha: float | None = None,
    consequent: str | None = None,
    qty: Decimal | None = None,
    price: Decimal | None = None,
    binding_constraint: str | None = None,
) -> str:
    lines = [f"Сигнал {symbol}: {SIDE_UK.get(side, str(side))}, u_final = {f2(u_final)}."]
    if top_rule is not None and alpha is not None:
        cons = f", висновок {term_uk('U', consequent)}" if consequent else ""
        lines.append(f"Головне правило {top_rule} (α = {f2(alpha)}{cons}).")
    if qty is not None:
        at = f" за ціною {_num(price)}" if price is not None else ""
        lines.append(f"Кількість {_num(qty)}{at}.")
    if binding_constraint:
        lines.append(
            f"Обмежувальний чинник сайзера: {BINDING_UK.get(binding_constraint, binding_constraint)}."
        )
    return clip(" ".join(lines))


def risk_event_text(
    *,
    rule: str,
    verdict: str,
    symbol: str | None = None,
    observed: Any = None,
    limit: Any = None,
    factor: Any = None,
) -> str:
    code = str(getattr(verdict, "value", verdict))
    where = f" ({symbol})" if symbol else ""
    text = f"Ризик-правило {rule}{where}: {VERDICT_UK.get(code, code)} ({code})"
    if code == "SHRINK" and factor is not None:
        text += f", множник ×{_num(factor)}"
    details = []
    if observed is not None:
        details.append(f"спостережено {_num(observed)}")
    if limit is not None:
        details.append(f"ліміт {_num(limit)}")
    if details:
        text += "; " + ", ".join(details)
    return clip(text + ".")


def risk_state_text(
    *, state_from: str, state_to: str, drawdown: float | None = None, reason: str | None = None
) -> str:
    a, b = str(state_from), str(state_to)
    text = f"Режим ризику: {RISK_STATE_UK.get(a, a)} → {RISK_STATE_UK.get(b, b)}"
    if drawdown is not None:
        text += f", просадка {drawdown * 100:.2f}%"
    if reason:
        text += f" ({reason})"
    return clip(text + ".")


def killswitch_text(
    *, action: str, reason: str | None = None, actor: str | None = None, run_id: str | None = None
) -> str:
    if action == "tripped":
        text = "УВАГА: спрацював kill-switch, торгівлю зупинено (HALTED), усі позиції закриваються"
    elif action == "release_requested":
        text = "Запит на зняття kill-switch"
    else:
        text = "Kill-switch знято, режим ОХОЛОДЖЕННЯ (лише зменшення позицій)"
    if actor:
        text += f"; адміністратор {actor}"
    if reason:
        text += f"; причина: {reason}"
    if run_id:
        text += f"; прогін {run_id}"
    return clip(text + ".")


def ws_disconnect_text(
    *, conn: str, cls: str, detail: str | None = None, reconnect_in_s: float | None = None
) -> str:
    text = f"Розрив WebSocket-з'єднання «{conn}»: {DISCONNECT_UK.get(cls, cls)}"
    if detail:
        text += f" ({detail[:200]})"
    if reconnect_in_s is not None:
        text += f"; повторне підключення через {reconnect_in_s:.1f} с"
    return clip(text + ".")


def daily_report_text(
    *,
    day: str,
    candles: Mapping[str, int],
    q_min: Mapping[str, float | None],
    gaps: Mapping[str, int],
    vetoes: int,
    transitions: Sequence[str],
    equity: Decimal | None = None,
    drawdown: float | None = None,
) -> str:
    lines = [f"Щоденний звіт FuzzHelm за {day} (UTC)."]
    for sym in sorted(candles):
        q = q_min.get(sym)
        q_txt = "немає даних" if q is None else f"{q:.4f}"
        lines.append(f"{sym}: свічок {candles[sym]}, мінімальний Q {q_txt}.")
    if gaps:
        lines.append("Прогалини: " + ", ".join(f"{k} {v}" for k, v in sorted(gaps.items())) + ".")
    else:
        lines.append("Прогалин немає.")
    lines.append(f"Відхилень ризик-контуром (VETO): {vetoes}.")
    if transitions:
        lines.append("Зміни режиму: " + "; ".join(transitions) + ".")
    if equity is not None:
        dd = "" if drawdown is None else f", просадка {drawdown * 100:.2f}%"
        lines.append(f"Капітал {_num(equity)}{dd}.")
    lines.append("Лише paper/testnet; не є інвестиційною рекомендацією.")
    return clip("\n".join(lines))
