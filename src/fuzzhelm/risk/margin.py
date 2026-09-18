"""Маржа, ціна ліквідації, відстань до ліквідації в ATR і оцінка гіршого випадку швидкості просадки.

Найменування: risk/margin.py
Призначення: виведення P_liq з умови Equity = MM (§5.10), а не з документації біржі; усе в Decimal.
Автор: Андрій Жук, 2026.

Умова ліквідації: Equity = MM, де Equity = W + q·(P − P_e) для позиції зі знаком q (лонг q > 0,
шорт q < 0), MM = |q|·P·mmr − ma. Розв'язок відносно P:

    P_liq = (q·P_e − W − ma) / (q − |q|·mmr)
    лонг  (q > 0):  P_liq = (q·P_e − W − ma) / (q·(1 − mmr))
    шорт  (q = −s): P_liq = (W + s·P_e + ma) / (s·(1 + mmr))

W — власні кошти позиції, P_e — ціна входу, mmr — ставка підтримуваної маржі, ma — maintenance amount.
Контрольний приклад: q=1, P_e=100, W=10 (10×), mmr=0.005, ma=0 ⇒ P_liq = 90/0.995 = 90.4523.

Зауваження про крос-маржу: Equity = W + q(P − P_e) = W_eff + q(P − P) з W_eff = W + q(P − P_e), тобто
ціна ліквідації з (W, P_e) дорівнює ціні з (поточний капітал, поточна ціна). Тому ризик-правила
рахують P_liq пост-трейдової позиції від поточного капіталу і поточної ціни — це точна тотожність.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from fuzzhelm.core.enums import Side
from fuzzhelm.core.money import D0, D1


def liq_price_long(q: Decimal, entry: Decimal, wallet: Decimal, mmr: Decimal,
                   maint_amount: Decimal = D0) -> Decimal:
    """P_liq^long = (q·P_e − W − ma) / (q·(1 − mmr)), q > 0. Значення ≤ 0 означає «ліквідація недосяжна»."""
    if q <= 0:
        raise ValueError(f"long quantity must be > 0, got {q}")
    return (q * entry - wallet - maint_amount) / (q * (D1 - mmr))


def liq_price_short(q: Decimal, entry: Decimal, wallet: Decimal, mmr: Decimal,
                    maint_amount: Decimal = D0) -> Decimal:
    """P_liq^short = (W + q·P_e + ma) / (q·(1 + mmr)), q > 0 — модуль шортової позиції."""
    if q <= 0:
        raise ValueError(f"short quantity (absolute) must be > 0, got {q}")
    return (wallet + q * entry + maint_amount) / (q * (D1 + mmr))


def liq_price(q_signed: Decimal, entry: Decimal, wallet: Decimal, mmr: Decimal,
              maint_amount: Decimal = D0) -> Decimal:
    """Єдина формула для позиції зі знаком: (q·P_e − W − ma) / (q − |q|·mmr)."""
    if q_signed == 0:
        raise ValueError("no position: liquidation price undefined for q = 0")
    return (q_signed * entry - wallet - maint_amount) / (q_signed - abs(q_signed) * mmr)


def position_equity(q_signed: Decimal, entry: Decimal, price: Decimal, wallet: Decimal) -> Decimal:
    return wallet + q_signed * (price - entry)


def maintenance_margin(q_signed: Decimal, price: Decimal, mmr: Decimal,
                       maint_amount: Decimal = D0) -> Decimal:
    return abs(q_signed) * price * mmr - maint_amount


def margin_ratio(q_signed: Decimal, entry: Decimal, price: Decimal, wallet: Decimal, mmr: Decimal, *,
                 maint_amount: Decimal = D0) -> Decimal:
    """MM / Equity. Рівно 1 на ціні ліквідації, < 1 — безпечно, > 1 — за межею ліквідації."""
    eq = position_equity(q_signed, entry, price, wallet)
    if eq <= 0:
        raise ValueError(f"position equity must be > 0 for a margin ratio, got {eq}")
    return maintenance_margin(q_signed, price, mmr, maint_amount) / eq


def side_liq_price(side: Side, q_abs: Decimal, entry: Decimal, wallet: Decimal, mmr: Decimal, *,
                   maint_amount: Decimal = D0) -> Decimal:
    if side is Side.LONG:
        return liq_price_long(q_abs, entry, wallet, mmr, maint_amount)
    if side is Side.SHORT:
        return liq_price_short(q_abs, entry, wallet, mmr, maint_amount)
    raise ValueError("FLAT position has no liquidation price")


def dtl_atr(price: Decimal, liq: Decimal, atr: Decimal) -> Decimal:
    """DTL = |P_t − P_liq| / ATR_t — відстань до ліквідації в одиницях власного шуму ціни."""
    if atr <= 0:
        raise ValueError(f"ATR must be > 0, got {atr}")
    return abs(price - liq) / atr


def signed_dtl_atr(side: Side, price: Decimal, liq: Decimal, atr: Decimal) -> Decimal:
    """Знакова відстань до ліквідації в ATR: лонг (P − P_liq)/ATR, шорт (P_liq − P)/ATR.

    Для позиції «по правильний бік» ліквідації збігається з dtl_atr. Від'ємне значення означає, що ціна
    вже за P_liq (позиція ліквідується одразу, плече > 1/mmr) — модуль |P − P_liq| тут показав би
    фіктивний «запас», тому ризик-правило використовує саме знакову форму.
    """
    if atr <= 0:
        raise ValueError(f"ATR must be > 0, got {atr}")
    if side is Side.LONG:
        return (price - liq) / atr
    if side is Side.SHORT:
        return (liq - price) / atr
    raise ValueError("FLAT position has no liquidation distance")


def reduced_max_leverage(stop_distance: Decimal, entry: Decimal, mmr: Decimal, b: Decimal) -> Decimal:
    """L'_max = 1 / (Δ_stop / (P_e·(1 − b)) + mmr), b — запас (buffer) між стопом і ліквідацією.

    Звідки: для свіжої позиції відстань до ліквідації
    P_e − P_liq = P_e·(1/L − mmr)/(1 − mmr) ≥ P_e·(1/L − mmr);
    вимога P_e·(1/L − mmr) ≥ Δ_stop/(1 − b) (стоп спрацює раніше за ліквідацію із запасом b) ⇔ L ≤ L'_max.
    """
    if not D0 <= b < D1:
        raise ValueError(f"buffer b must be in [0, 1), got {b}")
    if entry <= 0:
        raise ValueError("entry price must be > 0")
    return D1 / (stop_distance / (entry * (D1 - b)) + mmr)


def max_qty_for_dtl(side: Side, *, wallet: Decimal, price: Decimal, atr: Decimal, min_dtl: Decimal,
                    mmr: Decimal, maint_amount: Decimal = D0) -> Decimal:
    """Найбільша |q| свіжої позиції (вхід за P, власні кошти W), для якої DTL ≥ min_dtl.

    Відстань D(q) = (W + ma − q·P·mmr) / (q·(1 ∓ mmr)) спадає за q; D(q) = k·ATR ⇔
        лонг:  q = (W + ma) / (P·mmr + k·ATR·(1 − mmr))
        шорт:  q = (W + ma) / (P·mmr + k·ATR·(1 + mmr))
    """
    if side is Side.FLAT:
        raise ValueError("side must be LONG or SHORT")
    if wallet + maint_amount <= 0:
        return D0
    sgn = D1 - mmr if side is Side.LONG else D1 + mmr
    return (wallet + maint_amount) / (price * mmr + min_dtl * atr * sgn)


def leverage(q_abs: Decimal, price: Decimal, wallet: Decimal) -> Decimal:
    """L = q·P / W (ефективне плече позиції на власних коштах W)."""
    if wallet <= 0:
        raise ValueError("wallet must be > 0")
    return q_abs * price / wallet


# ------------------------------------------------ оцінка гіршого випадку (НЕ доведення стійкості)


@dataclass(frozen=True, slots=True)
class DrawdownSpeedBound:
    """Нижня межа кількості барів до пробиття DD_max. Це оцінка гіршого випадку за припущень:

    1) збиток позиції за бар обмежений її ризик-бюджетом: |ΔE| ≤ κ_mode·ρ_base·E_t·(1 + κ_slip) —
       тобто стоп виконується з ковзанням не більше κ_slip від стоп-відстані (немає гепів за стоп);
    2) ρ_base — сумарний ризик-бюджет усієї книги (а не однієї позиції з кількох);
    3) κ_mode не зростає зі зростанням DD (гарантує автомат станів).
    Тоді DD_{t+1} − DD_t = (E_t − E_{t+1})/peak ≤ |ΔE|/E_t ≤ v_max, і n_min ≥ (DD_max − DD₀)/v_max.
    Порушення будь-якого припущення (геп через стоп, ліквідація, кілька корельованих позицій)
    робить межу недійсною — тому це не доведення стійкості.
    """

    dd0: Decimal
    dd_max: Decimal
    v_max: Decimal
    n_min: Decimal


def per_bar_loss_bound(kappa_mode: Decimal, rho_base: Decimal, kappa_slip: Decimal) -> Decimal:
    """v_max = κ_mode·ρ_base·(1 + κ_slip) — найбільша частка капіталу, втрачена за бар (див. припущення)."""
    if kappa_mode < 0 or rho_base < 0 or kappa_slip < 0:
        raise ValueError("kappa_mode, rho_base and kappa_slip must be >= 0")
    return kappa_mode * rho_base * (D1 + kappa_slip)


def min_bars_to_drawdown(dd0: Decimal, dd_max: Decimal, v_max: Decimal) -> DrawdownSpeedBound:
    """n_min ≥ (DD_max − DD₀) / v_max. v_max = 0 ⇒ DD_max недосяжна (n_min = +∞)."""
    if dd_max < dd0:
        raise ValueError("dd_max must be >= dd0")
    if v_max < 0:
        raise ValueError("v_max must be >= 0")
    n = Decimal("Infinity") if v_max == 0 else (dd_max - dd0) / v_max
    return DrawdownSpeedBound(dd0, dd_max, v_max, n)
