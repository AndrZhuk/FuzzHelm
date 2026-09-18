# `fuzzhelm.decision`: фактичний публічний API

Шар рішень: 6 виходів детекторів → консенсус `T, R, V` → `InferenceEngine.infer(T, R, V)` →
ентропійна узгодженість `κ` → `u_final = clip(κ·u_raw, −1, 1)` → `DecisionTrace` → україномовний текст.
Пакет належить до float-домену: `decimal` не імпортує, настінного часу й випадковості не має.
`decision/__init__.py` нічого не реекспортує, тому імпортуйте з підмодулів.

Константи: `aggregator.EPS = 1e-9`, `aggregator.V_DEFAULT = 0.5`, `aggregator.V_SOURCE_DEFAULT = "default"`,
`agreement.KAPPA_MIN = 0.35`, `agreement.NU = 1.0`.

---

## `decision/aggregator.py`

```python
@dataclass(frozen=True, slots=True)
class Consensus:
    T: float                                        # ∈ [−1, 1]
    R: float                                        # ∈ [−1, 1]
    V: float                                        # ∈ [0, 1]
    v_source: str = "default"                       # ім'я CONTEXT-детектора, звідки взято V, або "default"
    outputs: tuple[DetectorOutput, ...] | None = None   # вхід агрегатора (для лінивої діагностики)
    eps: float = 1e-9
    trend: GroupConsensus | None      # property: None лише коли outputs is None
    reversion: GroupConsensus | None  # property
    def to_dict(self) -> dict[str, object]   # {"T","R","V","v_source","trend","reversion"}

@dataclass(frozen=True, slots=True)
class GroupConsensus:
    group: DetectorGroup
    value: float                    # T або R (побітово дорівнює Consensus.T / .R)
    mass: float                     # Σ ω_k c_k
    names: tuple[str, ...]
    omegas: tuple[float, ...]       # ω_k
    effective: tuple[float, ...]    # ω_k · c_k
    strengths: tuple[float, ...]    # s_k
    eps: float = 1e-9
    shares: tuple[float, ...]       # property: effective_k / max(eps, mass); value == Σ share_k · s_k
    def to_dict(self) -> dict       # {"group","value","mass","members":[{"name","omega","effective","s","share"}]}

def consensus(outputs: Sequence[DetectorOutput], eps: float = 1e-9, v_default: float = 0.5) -> Consensus
def group_consensus(outputs, group: DetectorGroup, eps: float = 1e-9) -> GroupConsensus
```

Формули (брифінг §5.3): `T = Σ_{TREND} ω c s / max(ε, Σ_{TREND} ω c)`, `R` — те саме по REVERSION.
Групу і `ω` беремо з `DetectorOutput.group/.weight`, імена детекторів не мають значення.

Інваріанти:
* усі `c = 0` дають `T = R = 0.0` точно, без NaN;
* якщо маса групи ≥ ε, `T` лежить між найменшим і найбільшим `s_k` детекторів цієї групи, у яких `ω_k c_k > 0`.
  Якщо маса менша за ε, значення стискається до 0 (опукла оболонка `{0} ∪ {s_k}`);
* CONTEXT-виходи в `T/R` не голосують. `V = features["V"]` єдиного CONTEXT-виходу, який має ключ `"V"`;
* якщо такого виходу немає, його `c = 0` (прогрів за контрактом `detectors/base.py`; справжній `VolRegime`
  до прогріву повертає `c = 0, features = {"V": 0.5, "warm": 0.0}` — це заглушка, а не вимір), або його `V`
  дорівнює `None`/не скінченне, беремо `V = v_default` і ставимо `v_source = "default"`;
* `ValueError` виникає, коли вага `ω < 0` або нескінченна, коли `V` дають два CONTEXT-виходи (навіть якщо
  один із них ще прогрівається), коли виміряне `V ∉ [0, 1]` або коли `eps <= 0`.

Діагностику груп рахують ліниво, лише коли її читають. На гарячому шляху `consensus()` займає
близько 1.2 мкс, а `decide()` без детекторів і рушія близько 4 мкс (замір на машині розробки).

## `decision/agreement.py`

```python
@dataclass(frozen=True, slots=True)
class Agreement:
    p_plus: float; p_minus: float; p_zero: float   # ≥ 0, сума = 1 ± 1e−12
    H: float                                       # нормована ентропія ∈ [0, 1]
    A_g: float                                     # 1 − H ∈ [0, 1]
    kappa: float                                   # ∈ [kappa_min, 1]
    mass: float = 0.0                              # Z = Σ ω c по TREND ∪ REVERSION
    kappa_min: float = 0.35
    nu: float = 1.0
    eps: float = 1e-9
    has_evidence: bool                             # property: mass >= eps
    def to_dict(self) -> dict                      # усі поля + "has_evidence"

def agreement(outputs, kappa_min: float = 0.35, nu: float = 1.0, eps: float = 1e-9) -> Agreement
def normalized_entropy(p_plus: float, p_minus: float, p_zero: float) -> float   # 0·ln0 = 0, обрізано до [0, 1]
def kappa_from_agreement(a_g: float, kappa_min: float = 0.35, nu: float = 1.0) -> float
def validate_params(kappa_min: float, nu: float) -> None   # ValueError, якщо kappa_min ∉ [0,1] або nu ∉ (0, ∞)
```

Формули (брифінг §5.4) рахуються лише по TREND ∪ REVERSION:
`p₊ = Σωc·max(s,0)/Z`, `p₋ = Σωc·max(−s,0)/Z`, `p₀ = Σωc·(1−|s|)/Z`, `H = −Σ p ln p / ln 3`,
`A_g = 1 − H`, `κ = κ_min + (1 − κ_min)·A_g^ν`.

Мала маса свідчень, `Z < ε` (детальніше в `docs/deviations.d/decision.md`). Недостачу `ε − Z` додаємо
рівномірно до трьох категорій: `p_i = (Σ ω c m_i + (ε − Z)⁺/3) / max(ε, Z)`.
* При `Z = 0` виходить `p = (⅓, ⅓, ⅓)`, `H = 1`, `κ = κ_min` (максимальне гасіння), `has_evidence = False`.
* При `Z ≥ ε` формула збігається з брифінгом дослівно, а на межі `Z = ε` залишається неперервною.

Межі: `p = (1,0,0)` дає `κ = 1`, `p = (⅓,⅓,⅓)` дає `κ = κ_min`. `κ` монотонно неспадна за `A_g`.
Коли всі сигнали мають один знак, а `|s| < 1`, то `A_g ≥ 1 − log₃2 ≈ 0.369`, але не 1.

## `decision/trace.py`

```python
@dataclass(frozen=True, slots=True)
class DecisionTrace:
    open_time_ns: int
    detector_outputs: tuple[DetectorOutput, ...]
    consensus: Consensus
    fuzzy: FuzzyResult                  # як повернув рушій (u_raw, inputs, memberships, fired, grid, mu_agg, engine, extras)
    agreement: Agreement
    u_raw: float
    kappa: float
    u_final: float                      # clip(kappa·u_raw, −1, 1)
    engine: str                         # engine.name
    sizing: Mapping[str, Any] | None = None   # дописує рушій бектесту/live
    risk: Mapping[str, Any] | None = None

    # properties
    T, R, V -> float                    # з consensus
    memberships -> Mapping[str, Mapping[str, float]]   # fuzzy.memberships
    fired_rules -> tuple[FiredRule, ...]  # УСІ fuzzy.fired, відсортовані (−alpha, rule_id), незалежно від порядку рушія
    top_rule -> FiredRule | None
    p_plus, p_minus, p_zero, H, A_g -> float

    def with_sizing(self, sizing: Mapping | None) -> DecisionTrace   # нова копія, dict(sizing)
    def with_risk(self, risk: Mapping | None) -> DecisionTrace
    def to_dict(self) -> dict[str, Any]   # JSON-сумісний
    def __eq__(self, other) -> bool       # структурно: self.to_dict() == other.to_dict()
    def __hash__(self) -> int             # hash((open_time_ns, engine, u_final)), узгоджено з __eq__

def rule_order(rule: FiredRule) -> tuple[float, str]     # ключ сортування (−α, rule_id)
def json_safe(obj: Any) -> Any
```

Ключі `to_dict()`: `open_time_ns, engine, inputs{T,R,V}, consensus{…, trend, reversion},
detector_outputs[{name, group, s, c, weight, features}], memberships{var:{term:μ}},
fired_rules[{rule_id, alpha, consequent, antecedent}]` (за спаданням α), `u_raw, kappa, u_final,
agreement{p_plus, p_minus, p_zero, H, A_g, kappa, mass, kappa_min, nu, has_evidence}, grid (list|None),
mu_agg (list|None), fuzzy_extras, sizing, risk`.

Правила перетворення в `json_safe`:
* numpy-масиви стають списками, numpy-скаляри — Python-числами;
* `Enum` стає `.value`, `DetectorGroup.TREND` — рядком `"trend"`;
* `Decimal` перетворюється на рядок через `core.money.dec_str`, без експоненти (`Decimal("1E+2")` дає `"100"`);
* NaN та ±inf стають `None`;
* dataclass і об'єкти з `model_dump()` стають dict, tuple і set — list, а решта — `str(obj)`.

Результат проходить `json.dumps(…, allow_nan=False)`. Ці дані не канонічні: float допустимі, хеш
від них не рахують.

Рівність трасувань: згенерований dataclass-ом `__eq__` падав би на `FuzzyResult.grid/mu_agg` (numpy,
«truth value of an array is ambiguous»), тому `DecisionTrace.__eq__` порівнює `to_dict()`. Отже,
`trace_live == trace_backtest` можна писати напряму в тестах детермінізму; NaN/inf у полях
(`features`, `extras`) порівнюються як рівні, бо обидва стають `None`.

Рекомендовані ключі для `sizing` (їх розуміє `narrate`): `binding_constraint ∈ {"ATR_RISK","VOL_TARGET","LEVERAGE"}`,
`qty`, `side`, `q_atr`, `q_vt`, `q_lev`, `s_t`, `sigma_ann`, `kappa_mode`, `reject_code`. Найпростіше
передати `SizingResult.to_dict()` (`sizing/sizer.py`, уже JSON-сумісний) або `dataclasses.asdict(result)`.
Рекомендовані ключі для `risk`: `state` (RiskState/str), `kappa_mode`, `verdict` (str | {"kind","factor"} | об'єкт
з `.kind/.factor`, напр. `risk.verdict.Verdict` з `factor: Fraction`), `vetoes` (list[str | {"rule","observed","limit"}
| об'єкт з `.rule/.observed/.limit`, напр. `risk.verdict.RuleVerdict`]), `approved_qty`.

## `decision/core.py`

```python
class DecisionCore:
    def __init__(self, detectors: Sequence[Detector], engine: InferenceEngine,
                 kappa_min: float = 0.35, nu: float = 1.0, *, eps: float = 1e-9, v_default: float = 0.5)
    @classmethod
    def from_config(cls, detectors, engine, cfg: Mapping[str, Any] | None) -> DecisionCore
        # cfg = fuzzhelm.config.load_yaml("detectors"); береться секція "agreement"
    detectors: tuple[Detector, ...]     # property
    engine: InferenceEngine             # property
    kappa_min, nu, eps, v_default       # атрибути
    def decide(self, window: BarWindow, open_time_ns: int | None = None) -> DecisionTrace

class AgreementParams(BaseModel):       # frozen, extra="forbid"; kappa_min ∈ [0,1], nu > 0
def agreement_params_from_config(cfg: Mapping | None) -> AgreementParams   # помилка → ConfigValidationError(path="agreement")
def clip_unit(x: float) -> float
```

`decide()` викликає `d.compute(window)` рівно один раз для кожного детектора в порядку списку.
Далі по черзі:
1. `consensus(outputs)`;
2. `engine.infer(T, R, V)`;
3. `agreement(outputs, kappa_min, nu)`;
4. `u_final = clip(κ·u_raw)`.

`open_time_ns` за замовчуванням дорівнює `window.bar(0).t_ns`. Між викликами `decide()` нічого
не зберігає (чиста функція від вікна). `ValueError` виникає, якщо рушій повернув нескінченне
`u_raw`, імена детекторів дублюються або параметри некоректні.

Інваріанти:
* `|u_final| ≤ |u_raw|`;
* знак `u_final` ніколи не протилежний знаку `u_raw`;
* `u_final ∈ [−1, 1]`, навіть якщо рушій порушив свій контракт.

## `decision/narrative_uk.py`

```python
def narrate(trace: DecisionTrace) -> str
def rule_sentence(rule: FiredRule) -> str
def term_uk(var: str, term: str) -> str       # "any" → "БУДЬ-ЯКА"; невідомий терм повертається без змін
def f2(x: float) -> str                       # 2 знаки, крапка, "-0.00" → "0.00"
def fnum(x: Any) -> str                       # кількості: Decimal через dec_str, float — до 8 знаків без хвостових нулів
TERMS_UK, VAR_NAMES, BINDING_UK, RISK_STATE_UK, VERDICT_UK   # словники
```

Речення йдуть у такому порядку:
1. Головне правило з найбільшим α:
   «Спрацювало правило R07 з α = 0.62: ЯКЩО тренд СИЛЬНЕ_ЗРОСТАННЯ І реверсія НЕМА_ТИСКУ І волатильність
   ПОМІРНА ТО сигнал СИЛЬНИЙ_ЛОНГ.» Антецедент завжди виводиться в порядку T, R, V.
2. До 3 інших правил: «Також спрацювали правила R08 (α = 0.31, сигнал ЛОНГ), …; ще N правил з меншою активацією.»
3. Консенсус: T, R, V. Якщо `v_source == "default"`, це позначається.
4. Узгодженість: `p₊, p₋, p₀, H, A_g, κ` і коефіцієнт приглушення. Якщо свідчень немає,
   виводиться окреме речення про `κ = κ_min`.
5. Вихід: «u_raw = …; … u_final = … (у бік лонгу|шорту|нейтральний).» Напрям визначається за
   надрукованим значенням: якщо `u_final` округлюється до «0.00» (напр. шум центроїда ~1e−19), то «нейтральний».
6. Сайзер, якщо `sizing` задано: обмежувальний чинник, тобто ATR_RISK → «ризик на ATR», VOL_TARGET → «таргет
   волатильності», LEVERAGE → «ліміт плеча». Далі q_atr/q_vt/q_lev, κ_mode, кількість і `reject_code`.
7. Ризик, якщо `risk` задано: стан, κ_mode, вердикт, вето або «вето немає», дозволена кількість.

Коли `fired` порожній, `u_raw == 0` і рушій має належності (`fuzzy.memberships` не порожні — Мамдані з
порожньою активацією), текст прямо каже: «Жодне правило не спрацювало … рішення — УТРИМАННЯ.»
Коли `fired` порожній, а `u_raw ≠ 0` або `fuzzy.memberships` порожні (рушій без бази правил, `LinearVoteEngine`),
текст пояснює, що цей рушій не має бази правил, і далі друкує речення з виходом.

## Приклад

```python
from fuzzhelm.config import load_yaml
from fuzzhelm.decision.core import DecisionCore
from fuzzhelm.decision.narrative_uk import narrate
from fuzzhelm.detectors.registry import build_detectors
from fuzzhelm.features.pipeline import FeatureParams, FeaturePipeline
from fuzzhelm.features.window import BarWindow
from fuzzhelm.fuzzy.mamdani import default_engine

cfg = load_yaml("detectors")
pipe, window = FeaturePipeline(FeatureParams.from_config(cfg)), BarWindow(64)
core = DecisionCore.from_config(build_detectors(cfg), default_engine(), cfg)
for bar in bars:                                          # features.convert.Bar, закриті свічки
    window.append(bar, pipe.update(bar))
    trace = core.decide(window)                           # open_time_ns = bar.t_ns
trace = trace.with_sizing(sizing_result.to_dict()).with_risk({"state": sm.state, "vetoes": []})
payload = {"trace": trace.to_dict(), "narrative": narrate(trace)}   # тіло /decisions/{id}/explain
```

Саме цей ланцюжок (700 реальних хвилинних свічок BTCUSDT з `fixtures/golden/`) проганяє
`test_decision_core_end_to_end_with_real_detectors_and_mamdani`: кожне правило з
`α_r = min(μ_T, μ_R, μ_V) > 0` присутнє в трасуванні, `v_source = "default"` рівно на барах прогріву
VolRegime, а повторний прогін дає рівні трасування.
