# База правил Мамдані — таблиця для Додатка А

Згенеровано `scripts/plot_control_surface.py` з `config/rules_mamdani.yaml` (45 правил, усі w = 1.0). Рядки — терм R, стовпці — терм T, у клітинці — наслідок U (id).

Політики: **LO** — домінує реверсія: c = clip(2r + trunc(t/2), −2, 2); **MID** — тренд перемагає у конфлікті: c = t (або r при t = 0), c = t + r при згоді і |t| = 1; **HI** — усе до HOLD, c = sign(t) лише при sign(t) = sign(r) ≠ 0.

**V = LO**

| R \ T | STRONG_DOWN | WEAK_DOWN | NEUTRAL | WEAK_UP | STRONG_UP |
|---|---|---|---|---|---|
| SELL_PRESSURE | STRONG_SHORT (R01) | STRONG_SHORT (R02) | STRONG_SHORT (R03) | STRONG_SHORT (R04) | SHORT (R05) |
| NO_PRESSURE | SHORT (R06) | HOLD (R07) | HOLD (R08) | HOLD (R09) | LONG (R10) |
| BUY_PRESSURE | LONG (R11) | STRONG_LONG (R12) | STRONG_LONG (R13) | STRONG_LONG (R14) | STRONG_LONG (R15) |

**V = MID**

| R \ T | STRONG_DOWN | WEAK_DOWN | NEUTRAL | WEAK_UP | STRONG_UP |
|---|---|---|---|---|---|
| SELL_PRESSURE | STRONG_SHORT (R16) | STRONG_SHORT (R17) | SHORT (R18) | LONG (R19) | STRONG_LONG (R20) |
| NO_PRESSURE | STRONG_SHORT (R21) | SHORT (R22) | HOLD (R23) | LONG (R24) | STRONG_LONG (R25) |
| BUY_PRESSURE | STRONG_SHORT (R26) | SHORT (R27) | LONG (R28) | STRONG_LONG (R29) | STRONG_LONG (R30) |

**V = HI**

| R \ T | STRONG_DOWN | WEAK_DOWN | NEUTRAL | WEAK_UP | STRONG_UP |
|---|---|---|---|---|---|
| SELL_PRESSURE | SHORT (R31) | SHORT (R32) | HOLD (R33) | HOLD (R34) | HOLD (R35) |
| NO_PRESSURE | HOLD (R36) | HOLD (R37) | HOLD (R38) | HOLD (R39) | HOLD (R40) |
| BUY_PRESSURE | HOLD (R41) | HOLD (R42) | HOLD (R43) | LONG (R44) | LONG (R45) |

Розподіл наслідків: STRONG_SHORT — 8, SHORT — 7, HOLD — 15, LONG — 7, STRONG_LONG — 8.
