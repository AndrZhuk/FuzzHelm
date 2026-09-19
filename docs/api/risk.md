# API модулів `sizing` і `risk` (фактичний, хвиля 1)

Автор: Андрій Жук, 2026. Джерело вимог — `docs/BRIEF.md` §5.8–§5.13, `docs/contracts.md` §10–§11.
Розходження з брифінгом і контрактом — `docs/deviations.d/risk.md` (R-01…R-13) і `docs/deviations.d/riskfix.md`
(RF-01 політика COOLDOWN, RF-03 ковзні VaR/CVaR кривої, RF-04 припущення оцінки гіршого випадку).

Домени типів: математика сайзера — `float`; усе, що йде в облік/журнал/БД (кількості, ціни, капітал,
множники вердиктів, пороги ризику) — `Decimal` (або точний `Fraction` усередині алгебри вердиктів).
Жоден модуль тут не читає настінний годинник: час — лише `ts_ns` подій або ін'єктований `Clock`.

---

## 1. Конфігурація — `fuzzhelm.risk.config`

```python
load_risk_config(source: str | Path | dict | None = None) -> RiskConfig
#   None → config/risk_limits.yaml; Path → YAML-файл; str → YAML-текст; dict → дерево.
#   Помилка схеми → ConfigValidationError(path="limits.max_daily_loss.value" | "state_machine" | ...)

RiskConfig(limits: LimitsCfg, state_machine: StateMachineCfg, sizing: SizingCfg, hysteresis: HysteresisCfg)
LimitsCfg: max_position_notional(value: Decimal), max_gross_leverage(value), max_daily_loss(value, reset),
           max_drawdown_halt(value), liquidation_buffer(min_dtl_atr, b), stale_data(max_lag_s, min_dq)
StateMachineCfg: warn_enter/warn_exit/cool_enter/cool_exit/halt_enter/cool_daily_loss/halt_daily_loss: Decimal,
                 warn_dwell/cool_dwell: int, warn_vol_ratio: float, kappa_mode: dict[RiskState, Decimal],
                 cooldown_policy: CooldownPolicy = SCALED_ENTRIES
                 # валідація: warn_exit < warn_enter ≤ cool_exit < cool_enter < halt_enter,
                 #            cool_daily < halt_daily, κ ∈ [0,1] незростає, κ(HALTED) = 0
class CooldownPolicy(StrEnum): SCALED_ENTRIES = "scaled_entries" | REDUCE_ONLY = "reduce_only"   # RF-01
    # scaled_entries (типово, рішення автора): у COOLDOWN новий вхід (з пласкої книги / новий бік розвороту)
    #   дозволено, розмір × κ_mode(COOLDOWN) у сайзері; наявну позицію (зокрема відкриту до COOLDOWN) — не збільшувати
    # reduce_only (буквально §5.12): жодного приросту; пласка книга в COOLDOWN при DD > cool_exit — поглинаюча (ENG-14)
SizingCfg (float): ewma_lambda, annualization_bars, sigma_target, scale_min, scale_max, filter_gamma,
                   rho_base, chi_atr, max_leverage
HysteresisCfg (float): enter, exit  (exit ≤ enter)
```
Усі моделі pydantic `frozen=True, extra="forbid"`. YAML-числа → `Decimal` через `str(float)` (0.30 → `Decimal('0.3')`).

---

## 2. Сайзинг — `fuzzhelm.sizing`

### 2.1 `vol_target`
```python
VolTarget(*, lam=0.94, A=525_600, sigma_target=0.20, s_min=0.25, s_max=3.0, gamma=0.2,
          s0: float | None = None, sigma2_0: float | None = None)      # УСІ параметри keyword-only
          # s0 ∉ [s_min, s_max] (або NaN) → ValueError: інакше інваріант меж s_t не тримався б
VolTarget.from_config(cfg: SizingCfg) -> VolTarget
vt.update(r: float) -> tuple[sigma_ann, s_star, s_t]                   # O(1); r — дохідність бару
vt.sigma2: float | None; vt.sigma_ann: float (nan до першого update); vt.s_star; vt.s_t; vt.n
vt.target_scale(sigma_ann) -> float                                    # clip(σ_target/σ_ann, s_min, s_max)
filter_step_response(gamma, n) -> float            # 1 − (1−γ)^n
filter_time_constant_bars(gamma) -> float          # −1/ln(1−γ) ≈ 4.4814 при γ=0.2  (див. R-01)
backward_euler_time_constant_bars(gamma) -> float  # (1−γ)/γ = 4 — звідки «T = 4Δt» у брифінгу
```
Ініціалізація: `σ²₀ = r₁²` (якщо не задано `sigma2_0`), `s₀ = s_min` (консервативний старт);
`σ_ann = 0 ⇒ s* = s_max`. Інваріант: `s_min ≤ s*_t ≤ s_max`, `s_t` — опукла комбінація ⇒ теж у межах.

### 2.2 `atr_risk`
```python
q_atr(u_final, equity, atr, rho_base=0.005, chi=2.0) -> float   # ρ·|u|·E/(χ·ATR); ATR≤0|nan або E≤0 → 0.0
stop_distance(atr, chi=2.0) -> float                             # Δ_stop = χ·ATR (ціновий)
risk_money(qty, atr, chi=2.0) -> float                           # qty·χ·ATR = ρ·|u|·E (const за ATR)
```

### 2.3 `sizer`
```python
class BindingConstraint(StrEnum): ATR_RISK | VOL_TARGET | LEVERAGE
SizingParams(rho_base=0.005, chi=2.0, max_leverage=3.0);  SizingParams.from_config(cfg: SizingCfg)
SizingInput(u_final: float, equity: Decimal, price: Decimal, atr: float, s_t: float, kappa_mode: float,
            step_size: Decimal, min_notional: Decimal, gross_notional: Decimal = 0, sigma_ann: float = nan)
PositionSizer(params: SizingParams | None = None).size(inp) -> SizingResult
SizingResult(qty: Decimal, side: Side, binding_constraint: BindingConstraint, q_atr, q_vt, q_lev,
             s_t, sigma_ann, kappa_mode, reject_code: RejectCode | None, q_raw: float,
             notional: Decimal, stop_distance: float)
   .signed_qty -> Decimal;  .to_dict() -> dict (JSON; для DecisionTrace.sizing; NaN-стоп на прогріві → null)
```
Семантика: `q_vt = s_t·E/p`, `q_lev = max(0, (E·L_max − gross_notional)/p)`, `q_raw = κ_mode·min(...)`,
`qty = to_decimal(q_raw, step_size)` (ROUND_DOWN). `qty` — **цільова позиція за модулем**, не приріст;
`gross_notional` — Σ|номінал| **інших** інструментів. Рівність мінімумів → перший у порядку ATR_RISK,
VOL_TARGET, LEVERAGE.
Відмови: `kappa_mode == 0` → `qty=0, reject_code=HALTED` (за будь-якого u); `u == 0` → `side=FLAT, qty=0,
reject_code=None`; `qty·p < min_notional` → `qty=0, BELOW_MIN_NOTIONAL` (`ZERO_QTY`, якщо min_notional=0).
Інваріант (property): `qty ≤ exact(q_raw)`, `qty % step == 0`, `exact(q_raw) − qty < step`.
Валідація: `u ∈ [−1,1]` скінченне, `kappa_mode ∈ [0,1]`, `price > 0` — інакше `ValueError`.

### 2.4 `hysteresis`
```python
HysteresisGate(enter=0.25, exit=0.12, initial=Side.FLAT)   # 0 ≤ exit ≤ enter ≤ 1; exit == enter — компаратор
gate.update(u_final) -> Side;  gate.side;  gate.flips (лічильник змін стану);  gate.reset(side=FLAT)
   # u_final не скінченне (NaN/inf) → ValueError, стан не змінюється (NaN інакше мовчки тримав би позицію)
churn_cost_per_day(fee_rate, fills_per_bar=2.0, bars_per_day=1440) -> float   # частка номіналу/добу
```
FLAT: `u ≥ enter → LONG`, `u ≤ −enter → SHORT`. LONG: `u ≤ −enter → SHORT`, `u < exit → FLAT`.
SHORT: `u ≥ enter → LONG`, `u > −exit → FLAT`. Вихід — знаковий (R-09).

---

## 3. Ризик — `fuzzhelm.risk`

### 3.1 Алгебра вердиктів — `verdict`
```python
Verdict(kind: VerdictKind, factor: Fraction)        # ALLOW↔1, VETO↔0, SHRINK↔(0;1) строго; інакше ValueError
   .factor_dec -> Decimal                           # ⌊factor⌋ до 1e-18 (для risk_event.factor)
ALLOW, VETO                                         # синглтони
shrink(f: Decimal | Fraction | int | str) -> Verdict   # float → TypeError; f∉(0;1) → ValueError
compose(verdicts: Iterable[Verdict]) -> Verdict     # VETO якщо є; SHRINK(Π f) якщо є SHRINK; інакше ALLOW
exposure(verdict, requested: Decimal) -> Decimal    # requested ≥ 0; ALLOW→requested, VETO→0, SHRINK→⌊f·req⌋₁ₑ₋₁₈
floor_fraction(x: Fraction, scale=18) -> Decimal;  to_fraction(x) -> Fraction
RuleVerdict(rule: str, verdict: Verdict, observed: Decimal | None, limit: Decimal | None,
            payload: Mapping[str, Any] = {}, halt: bool = False)
class RiskRule(Protocol): name: str; def check(self, ctx: RiskContext) -> RuleVerdict
```
Інваріанти (hypothesis, ≤ 20 вердиктів): `exposure(compose(V)) ≤ minᵢ exposure(vᵢ) ≤ requested`;
`compose` точно комутативна й асоціативна (раціональний добуток), `ALLOW` — одиниця, `VETO` — поглинає.

### 3.2 Контекст і трекер капіталу — `context`
```python
EquityTracker().update(ts_ns: int, equity: Decimal) -> EquitySnapshot   # ts неспадні, інакше ValueError
tracker.rebase() -> EquitySnapshot | None    # пік і E_open := поточний капітал (після зняття HALTED)
tracker.snapshot -> EquitySnapshot | None
EquitySnapshot(ts_ns, equity, peak, drawdown, day_start_ns, equity_day_open, pnl_day).day_return -> Decimal
#   DD = 1 − E/peak (running peak); нова UTC-доба (за ts_ns!) ⇒ E_open = останній капітал до північчі

RiskContext(ts_ns, instrument, price, equity, current_qty, target_qty, atr, stop_distance,
            drawdown=0, pnl_day=0, equity_day_open=None, day_start_ns=None, gross_notional_other=0,
            mmr=Decimal("0.005"), maint_amount=0, leverage_setting=Decimal(3), last_data_ns=None,
            dq_score=1, risk_state=RiskState.NORMAL, step_size=None)
RiskContext.from_snapshot(snap, **fields)    # equity/drawdown/pnl_day/E_open/day_start_ns — зі snapshot
# похідні (обчислюються в __post_init__): base_qty, increase_qty, post_qty (=|target|);
# властивості: target_side, is_flip, post_notional, lag_ns, effective_day_open
cap_increase(ctx, q_max_post) -> Verdict     # «post ≤ q_max» → ALLOW / SHRINK((q_max−base)/inc) / VETO
```
Кількості в контексті — **зі знаком**. Приріст: той самий бік — `max(0, |tgt|−|cur|)`, розворот — `|tgt|`,
закриття — 0. Правила гейтять **лише приріст**: зменшення/закриття проходить завжди.
`last_data_ns=None` ⇒ лаг 0 (бар-синхронний бектест); `stop_distance` = χ·ATR із сайзера (Decimal).

### 3.3 Шість лімітів — `risk.rules.*` (кожне: `name`, `from_config(cfg)`, `check(ctx) -> RuleVerdict`)

| Клас (`name`) | observed / limit | Вердикт на приріст |
|---|---|---|
| `MaxPositionNotional(max_margin_fraction=0.30)` (`max_position_notional`) | `|q_post|·P/L_set/E` / 0.30 | cap `q ≤ 0.30·E·L_set/P` → ALLOW/SHRINK/VETO (R-05) |
| `MaxGrossLeverage(max_leverage=3.0)` (`max_gross_leverage`) | `(N_інших + |q_post|·P)/E` / 3.0 | cap `q ≤ (3E − N_інших)/P` |
| `MaxDailyLoss(max_loss_fraction=0.02)` (`max_daily_loss`) | `PnL_day/E_open` / −0.02 | VETO, якщо observed ≤ limit; PnL минулої доби (ctx.day_start_ns ≠ доба ts) → скинуто |
| `MaxDrawdownHalt(max_drawdown=0.12)` (`max_drawdown_halt`) | DD / 0.12 | VETO + `halt=True` (halt — і на редукції) |
| `LiquidationBufferGuard(min_dtl_atr=6, b=0.20)` (`liquidation_buffer`) | **знакова** DTL / 6 | DTL ≥ 6 → ALLOW; інакше SHRINK до `L_cap = min(L'_max, L_DTL)`; VETO лише коли навіть base за межею (R-06). DTL знакова (`signed_dtl_atr`): при плечі > 1/mmr P_liq по інший бік від P, observed < 0 ⇒ порушення (модуль із §5.10 дав би фіктивне ALLOW) |
| `StaleDataGuard(max_lag_s=5, min_dq=0.90)` (`stale_data`) | лаг, с / 5 (або Q / 0.90, якщо порушено лише Q) | VETO при `lag > 5` або `Q < 0.90` |

`observed = None`, якщо величина невизначена (E ≤ 0, немає позиції, ліквідація недосяжна, ATR ≤ 0).
При E ≤ 0 (або W ≤ 0, ATR ≤ 0 у LiquidationBuffer) приріст — VETO.

### 3.4 Ланцюг — `guard`
```python
RiskGuard(rules: Sequence[RiskRule], journal: RiskJournal | None = None, *,
          killswitch: KillSwitch | None = None, mode_gate: bool = True,
          cooldown_policy: CooldownPolicy | str = "scaled_entries")    # лише для вбудованого RiskModeGate
RiskGuard.from_config(cfg: RiskConfig | None = None, journal=None, *, killswitch=None)
    # 6 лімітів + gate; cooldown_policy = cfg.state_machine.cooldown_policy
default_rules(cfg: RiskConfig) -> list[RiskRule]
guard.evaluate(ctx) -> GuardResult
GuardResult(verdict, records: tuple[RuleVerdict, ...], approved_qty: Decimal,   # ЦІЛЬОВА позиція зі знаком
            requested_qty, current_qty, increase_requested, increase_approved, flatten_all: bool, halt: bool)
   .order_qty -> Decimal   # approved − current (що відправити в роутер)
   .to_dict() -> dict      # канонічний (Decimal → str), для DecisionTrace.risk
RiskModeGate(killswitch=None, cooldown_policy="scaled_entries")   # name="risk_mode", останнім у ланцюгу
```
Порядок `default_rules`: `stale_data` (скор якості — перший вхід ланцюга, §15), `max_position_notional`,
`max_gross_leverage`, `max_daily_loss`, `max_drawdown_halt`, `liquidation_buffer`; далі `risk_mode`.
Порядок впливає лише на порядок записів у журналі, не на вердикт (комутативність `compose`).
Алгоритм: кожне правило → `journal.record_rule(...)` (КОЖНА перевірка, не лише VETO) → `compose` →
`increase_approved = floor_qty(exposure(V, increase), step_size)` → `approved = side·(base + inc)`.
`RiskModeGate` (лише ALLOW/VETO приросту; зменшення/закриття — завжди): `observed` = тяжкість стану (NORMAL 0,
WARNING 1, COOLDOWN 2, HALTED 3; kill-switch → 3), `limit` = найвищий рівень, у якому дозволено **цей вид** приросту:
збільшення наявної позиції (`base_qty > 0`) — 1 за будь-якої політики; новий вхід (`base_qty = 0`: з пласкої книги або
новий бік розвороту) — 2 за `scaled_entries`, 1 за `reduce_only`. VETO ⇔ приріст > 0 ∧ observed > limit.
`payload`: `state, kill_switch, cooldown_policy, new_entry, reduce_only` (наявну позицію не збільшувати: COOLDOWN,
HALTED), `entries_allowed, flatten_all, increase_qty`. Множина VETO за `reduce_only` ⊇ множини за `scaled_entries`;
решта шести правил від політики не залежить (property `test_risk_chain_never_exceeds_requested_or_kappa_scaled_size`).
`flatten_all = halt ∨ kill-switch ∨ ctx.risk_state == HALTED` ⇒ `approved_qty = 0` (рушій має закрити **всі**
позиції). Сигнал halt від правила тригерить переданий `killswitch`. Дублікати `name` у ланцюгу → ValueError.
Вартість: ≈ 9 мкс/виклик без споживачів журналу, ≈ 21 мкс із `keep=True` (виміряно на машині розробки 2026-09-18) —
викликати на **намір заявки**, не на кожен бар.

### 3.5 Автомат станів — `state`
```python
class RiskEvent(StrEnum): HALT_BREACH | COOL_BREACH | WARN_BREACH | RECOVERY | STEADY | ADMIN_RELEASE
TRANSITIONS: Mapping[(RiskState, RiskEvent), RiskState]    # 24 клітинки, явний dict (MappingProxyType)
SEVERITY: Mapping[RiskState, int]                           # NORMAL 0, WARNING 1, COOLDOWN 2, HALTED 3
RiskObservation(ts_ns: int, equity: Decimal, vol_ratio: float | None = None)   # vol_ratio = σ_ann/σ_base
   # vol_ratio: None (невідоме) або скінченне ≥ 0; NaN/inf/від'ємне → ValueError на конструкторі (до зміни стану)
Transition(ts_ns, state_from, state_to, event, dwell_bars, drawdown, day_return, vol_ratio, actor=None)
   .payload() -> dict (канонічний)
RiskStateMachine(cfg: StateMachineCfg | None = None, *, killswitch=None, tracker=None, clock=None,
                 on_transition: Callable[[Transition], None] | None = None,
                 on_audit: Callable[[AuditRecord], None] | None = None)
fsm.update(obs) -> Transition | None     # оновлює трекер, dwell += 1, класифікує, переходить за TRANSITIONS
fsm.release(actor_role: Role | str, *, actor=None) -> Transition | None
   # не admin → PermissionDeniedError; поза HALTED → None (без запису risk.release), АЛЕ якщо засувка вже
   #   спрацювала, а автомат ще не латчив HALTED, — знімає засувку (аудит "killswitch.release"), пік не перебазує;
   # HALTED → COOLDOWN + tracker.rebase() + AuditRecord("risk.release", before/after) + killswitch.release
   #   (у COOLDOWN після зняття — κ = 0.25; нові входи — за cooldown_policy)
fsm.state, fsm.kappa_mode (Decimal), fsm.kappa_mode_float, fsm.dwell_bars, fsm.snapshot, fsm.reduce_only,
fsm.flatten_all, fsm.transitions (list), fsm.tracker, fsm.killswitch, fsm.cooldown_policy
fsm.reduce_only       # наявну позицію збільшувати заборонено: COOLDOWN (за обох політик) або HALTED
fsm.entries_allowed   # новий вхід дозволено: NORMAL/WARNING; COOLDOWN — лише за scaled_entries; HALTED/засувка — ні
fsm.classify(snap, vol_ratio, state=None, dwell=None) -> RiskEvent
```
Класифікація (пріоритет): HALT_BREACH (kill-switch ∨ DD ≥ 0.12 ∨ PnL_day ≤ −0.03·E_open) → [HALTED: STEADY]
→ COOL_BREACH (DD ≥ 0.08 ∨ PnL_day ≤ −0.02·E_open) → [COOLDOWN: RECOVERY якщо DD ≤ 0.05 ∧ dwell ≥ 30, інакше
STEADY] → WARN_BREACH (DD ≥ 0.04 ∨ vol_ratio > 1.6) → [WARNING: RECOVERY якщо DD ≤ 0.025 ∧ dwell ≥ 15] → STEADY.
`dwell` — барів у поточному стані (скидається лише при зміні стану). Вхід у HALTED тригерить `fsm.killswitch`;
ручний `killswitch.trip()` ⇒ HALTED на наступному барі; `killswitch.release(ADMIN)` напряму ⇒ автомат
синхронізується на наступному барі (actor="kill_switch"). **σ_base** брифінг не визначає — `vol_ratio`
рахує викликач (R-08). Рекомендоване з'єднання: `RiskGuard.from_config(cfg, …, killswitch=fsm.killswitch)` (та
сама `cooldown_policy`, що й у `cfg.state_machine`) і `ctx.risk_state = fsm.state`,
`SizingInput.kappa_mode = fsm.kappa_mode_float`. Автомат (класифікація, таблиця, витримка) від `cooldown_policy` не
залежить — політика діє лише в гейті `risk_mode` (тест `test_state_machine_path_is_independent_of_cooldown_policy`).

### 3.6 Маржа — `margin` (усе Decimal)
```python
liq_price_long(q, entry, wallet, mmr, maint_amount=0)   # (q·P_e − W − ma)/(q(1−mmr)); q>0; ≤0 ⇒ недосяжна
liq_price_short(q, entry, wallet, mmr, maint_amount=0)  # (W + q·P_e + ma)/(q(1+mmr)); q>0 (модуль)
liq_price(q_signed, entry, wallet, mmr, maint_amount=0) # (q·P_e − W − ma)/(q − |q|·mmr)
side_liq_price(side, q_abs, entry, wallet, mmr, *, maint_amount=0)
position_equity(q_signed, entry, price, wallet);  maintenance_margin(q_signed, price, mmr, maint_amount=0)
margin_ratio(q_signed, entry, price, wallet, mmr, *, maint_amount=0)   # MM/Equity; = 1 на P_liq
dtl_atr(price, liq, atr)                                 # |P − P_liq|/ATR (формула §5.10); ATR ≤ 0 → ValueError
signed_dtl_atr(side, price, liq, atr)                    # лонг (P − P_liq)/ATR, шорт (P_liq − P)/ATR; < 0 ⇒ ціна вже
                                                         # за ліквідацією (плече > 1/mmr); цю форму бере правило
reduced_max_leverage(stop_distance, entry, mmr, b)       # 1/(Δ_stop/(P_e(1−b)) + mmr)
max_qty_for_dtl(side, *, wallet, price, atr, min_dtl, mmr, maint_amount=0)   # найбільша |q| з DTL ≥ min_dtl
leverage(q_abs, price, wallet)                           # q·P/W
per_bar_loss_bound(kappa_mode, rho_base, kappa_slip) -> Decimal            # v_max = κ·ρ·(1+κ_slip)
   # κ — κ_mode НА ВХОДІ відкритої позиції (розмір фіксується на вході, ENG-04), не поточного стану; E — капітал
   # на вході; відкат нереалізованого прибутку (до (1+m+κ_slip)·κ·ρ за бар при тейку m·Δ_stop) не враховано (RF-04);
   # комісії/імпакт/фандинг теж поза межею: до f_taker·κ·L_max·E на бік (0.12 %·κ·E при 0.0004 і L_max = 3)
min_bars_to_drawdown(dd0, dd_max, v_max) -> DrawdownSpeedBound(dd0, dd_max, v_max, n_min)
   # n_min = (DD_max − DD₀)/v_max — ОЦІНКА гіршого випадку за припущень у докстрінгу, НЕ доведення стійкості
```
Контроль: `liq_price_long(1, 100, 10, 0.005)` = 90/0.995 = **90.4523** (4 зн.).

### 3.7 VaR/CVaR — `var` (float-статистика, звітна метрика, не блокує ордери)
```python
historical_var_cvar(returns, alpha=0.05, window=500 | None) -> VarResult(var, cvar, m, n)
   # m = max(1, ⌊α·n⌋); VaR = −r_(m) (нижня емпірична оцінка); CVaR = VaR + mean(r_(m) − r_(i)), i ≤ m
   # інваріант: CVaR ≥ VaR (точно, і в float); VaR ≥ 0 — НЕ гарантовано (R-04)
parametric_var(sigma, *, equity=1.0, h=1.0, alpha=0.05) -> float     # z_{1−α}·σ·√h·E (NormalDist)
returns_from_equity(equity: Sequence[Decimal]) -> np.ndarray         # ΔE/E_{t−1}
rolling_var_breaches(returns, window=500, alpha=0.05, chunk=4096) -> VarBacktest(breaches, n, var_series)
   # alpha ∉ (0,1) або window ≤ 0 → ValueError
   # VaR_t лише за r[t−W:t]; пробій: r_t < −VaR_t; вхід для kupiec_pof(breaches, n)
tail_count(n, alpha) -> int

# ковзні VaR/CVaR кривої капіталу (RF-03): точка t — на вікні r_{t−W+1..t} (закінчується на t включно)
rolling_var_cvar(returns, window=500, alpha=0.05, *, min_obs=None (= W), chunk=4096) -> (var[n+1], cvar[n+1])
   # частки капіталу; NaN до min_obs дохідностей; повні вікна — векторизовано блоками np.partition (O(W) на точку
   # в C, без матриці N×W); 0 < min_obs ≤ W, інакше ValueError; CVaR ≥ VaR у кожній точці (у float)
var_cvar_money(equity: Sequence[Decimal], window=500, alpha=0.05, *, min_obs=None)
   -> (list[Decimal | None], list[Decimal | None])     # гроші: E_t · частка; None до 500 дохідностей; не обрізано до 0
RollingVarCvar(window=500, alpha=0.05, *, min_obs=None).update(E_t) -> (VaR_t, CVaR_t) | (None, None)
   # покроково (live), те саме ядро _tail_stats ⇒ ті самі числа до біта, що й var_cvar_money на тій самій кривій
```
Конвенція `equity_point.var95/cvar95`: `E_t · VaR̂₉₅(r_{t−499..t})` у валюті котирування (USDT), додатне = збиток;
`None` для точок 0…499; значення не обрізаються до 0 (на вікні майже з самих виграшів VaR < 0, R-04). Виміряно:
прохід над 64 800 точками — 0.098–0.105 с (`runner --db … --report`, `docs/figures/riskfix_cooldown_policy.md`).

### 3.8 Тест Купця — `kupiec`
```python
CHI2_1_95: float = NormalDist().inv_cdf(0.975)**2     # 3.8414588…
kupiec_pof(breaches: int, W: int, p=0.05, critical=CHI2_1_95) -> KupiecResult(lr, reject, breaches, n, p,
                                                                               p_hat, critical)
acceptance_region(W, p=0.05, critical=CHI2_1_95) -> (x_lo, x_hi)   # W=500 → (17, 35)
```
`x = 0` і `x = W` — через угоду `0·ln 0 = 0`; `lr ≥ 0` (обрізання float-шуму). 20/500 **не** відкидається (R-02).

### 3.9 Журнал і аудит — `journal`, засувка — `killswitch`
```python
RiskJournal(run_id: UUID | None = None, sink: Callable[[RiskEventRecord], None] | None = None, *,
            event_journal: EventJournal | None = None, keep: bool = True)
   .record_rule(ts_ns, instrument, rv) -> RiskEventRecord | None   # None, якщо немає споживачів (лише лічильник)
   .record_transition(tr, instrument=None) -> RiskEventRecord      # rule="risk_state", state_from/to, dwell_bars
   .append(record); len(journal); .records; .by_rule(name); .vetoes(); .has_consumers
RiskEventRecord(ts_ns, rule, verdict: VerdictKind | None, factor, observed, limit_value, instrument=None,
                state_from=None, state_to=None, dwell_bars=None, actor=None, payload={}, run_id=None)
   .to_row() -> dict      # канонічні значення (Decimal → str без експоненти); ключі — колонки risk_event, КРІМ
                          # ts_ns (→ ts TIMESTAMPTZ) і instrument (канонічний символ → instrument_id INT):
                          # ці два відображає шар storage. Увага: DDL має factor NUMERIC(6,4), а factor_dec —
                          # 18 знаків; при вставці Postgres округлить (напр. SHRINK 0.99996 → 1.0000) — для
                          # точного множника storage має писати й payload або розширити колонку.
AuditRecord(ts_ns, action, target, actor_role: Role, actor, before, after).to_row()  # before_json/after_json
KillSwitch(*, clock: Clock | None = None, on_audit=None)
   .trip(reason, ts_ns=None) -> bool     # True лише для нового спрацювання; перша причина зберігається
   .release(actor_role, *, actor=None, ts_ns=None) -> AuditRecord | None   # не admin → PermissionDeniedError
   .is_tripped, .reason, .tripped_at_ns, .trip_count, .state()
```
`event_journal` задано ⇒ кожен запис також іде в хеш-ланцюг `EventJournal` з `kind="risk_event"`.

---

## 4. Приклад інтеграції (один бар рушія)

```python
cfg = load_risk_config()
vt, sizer, gate = VolTarget.from_config(cfg.sizing), PositionSizer(SizingParams.from_config(cfg.sizing)), \
    HysteresisGate(cfg.hysteresis.enter, cfg.hysteresis.exit)
fsm = RiskStateMachine(cfg.state_machine, on_transition=journal.record_transition, on_audit=audit_repo.add)
guard = RiskGuard.from_config(cfg, journal, killswitch=fsm.killswitch)

sigma_ann, _, s_t = vt.update(r_t)                                   # float
vol_ratio = sigma_ann / sigma_base if sigma_base else None       # None — «невідоме», не NaN/inf
fsm.update(RiskObservation(bar.close_time_ns, portfolio.equity, vol_ratio))
side = gate.update(trace.u_final)
sz = sizer.size(SizingInput(trace.u_final, equity, price, atr, s_t, fsm.kappa_mode_float,
                            inst.step_size, inst.min_notional, gross_notional_other, sigma_ann))
target = sz.qty * int(side)                                          # ціль лише, якщо гейт у позиції
ctx = RiskContext.from_snapshot(fsm.snapshot, instrument=inst.symbol_canon, price=price,
        current_qty=position.qty, target_qty=target, atr=to_decimal(atr, inst.tick_size),
        stop_distance=to_decimal(sz.stop_distance, inst.tick_size), mmr=inst.mmr,
        maint_amount=inst.maint_amount, risk_state=fsm.state, step_size=inst.step_size,
        last_data_ns=last_event_ns, dq_score=q_score, gross_notional_other=gross_notional_other)
res = guard.evaluate(ctx)
if res.flatten_all: router.flatten_all()
elif res.order_qty != 0: router.submit(res.order_qty, ...)
```

---

## 5. Зміни після незалежного рецензування (для `docs/deviations.d/risk.md`, R-13)

- **Що в спеці (§5.10):** `DTL = |P_t − P_liq|/ATR_t ≥ 6`.
- **Що насправді:** при плечі > 1/mmr (200× при mmr = 0.005) ціна ліквідації лежить по інший бік від P
  (лонг: `P_liq > P`), тобто позиція ліквідується одразу, а модуль дає фіктивний «запас»: запит 10 000× при
  ATR = 0.05% ціни отримував DTL ≈ 9.85 і ALLOW; із валовим лімітом 150× гард приймав позицію з DTL = 3.35 < 6.
- **Що зроблено:** `LiquidationBufferGuard` рахує знакову відстань `margin.signed_dtl_atr` (лонг `P − P_liq`,
  шорт `P_liq − P`); від'ємна = порушення ⇒ SHRINK до `L_cap`. `margin.dtl_atr` (формула брифінгу) лишився.
  Тести: `test_liquidation_guard_rejects_position_already_beyond_liquidation`,
  `test_liquidation_guard_alone_never_approves_dtl_below_limit` (property, плече до 2000×) — обидва падають на
  старій (модульній) версії правила.
- **Чим обґрунтовано:** по правильний бік ліквідації знакова і модульна форми збігаються (для штатного конфігу
  з `max_gross_leverage = 3` поведінка не змінилась); модуль має сенс лише як «відстань», а не як «запас».
