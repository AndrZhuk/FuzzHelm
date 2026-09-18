# API модуля `fuzzy` (фактичний, хвиля 1)

Нечітке ядро: функції належності, база правил Мамдані, рушій виведення, дефазифікація, лінійна
базова лінія, поверхня керування й аудит монотонності. Float-домен: `decimal` не імпортується.
Модуль детермінований: немає годинника, випадковості й `fuzzhelm.infra`. `mypy --strict` чистий.

```python
from fuzzhelm.fuzzy.membership import load_membership
from fuzzhelm.fuzzy.rules import load_rulebase
from fuzzhelm.fuzzy.mamdani import MamdaniEngine, default_engine

engine = default_engine()                        # config/membership.yaml + config/rules_mamdani.yaml
res = engine.infer(T=0.4, R=-0.3, V=0.3)         # FuzzyResult (fuzzy/base.py)
res.u_raw                                        # -0.13958542757729803
res.fired[0]                                     # FiredRule('R04', 0.6667, 'STRONG_SHORT', {...})
engine.infer_u(0.4, -0.3, 0.3)                   # той самий float побітово, без трасування (~7.7 мкс)
engine.infer_batch(T_arr, R_arr, V_arr)          # векторизовано, ~0.9 мкс/точку, збіг з infer_u ≤ 1e-12
```

## `fuzzy/membership.py`

| Сутність | Сигнатура / поведінка |
|---|---|
| `tri(a, b, c) -> Triangular` | ≡ trap(a,b,b,c). `mf(x: float) -> float`, `mf(x: np.ndarray) -> np.ndarray` |
| `trap(a, b, c, d) -> Trapezoidal` | 0 поза [a; d], підйом на [a; b), плато 1 на [b; c], спад на (c; d]. Дозволено a == b або c == d: це вертикальне ребро, і μ(a) = 1. Потрібно a ≤ b ≤ c ≤ d, a < d, інакше `ValueError` |
| `gauss(m, sigma) -> Gaussian` | exp(−(x−m)²/(2σ²)), σ > 0 |
| `MembershipFunction` | `.scalar(x)`, `.array(x)`, `.core -> (lo, hi)` (де μ = 1), `.support`, `.knots` (None для gauss), `.to_dict()` (формат YAML), `.kind ∈ {"tri","trap","gauss"}` |
| `LinguisticVariable(name, range, terms, meta={})` | frozen. `.term_names` (порядок як у YAML), `.fuzzify(x) -> dict[str, float]` (x **обрізається** до `range`, NaN/±inf → `ValueError`), `.evaluate(xs) -> ndarray (n_terms, len)` (без обрізання), `.clip(x)`, `.to_dict()` |
| `MembershipConfig(variables, defuzz, version, meta)` | `.T .R .V .U`, `cfg["T"]`, `.defuzz: DefuzzConfig(scheme, grid_nodes)`, `.to_dict()` |
| `load_membership(src=None) -> MembershipConfig` | `None` → `config/membership.yaml`; `Path` → файл; `Mapping` → розібраний YAML; `str` → текст YAML (рядок без `\n`, що закінчується на `.yaml`/`.yml`, трактується як шлях) |
| `membership_from_dict(data)` | те саме зі словника |
| константи | `ANY_TERM = "any"`, `INPUT_VARIABLES = ("T","R","V")`, `REQUIRED_VARIABLES = ("T","R","V","U")`, `DEFUZZ_SCHEMES` |

Валідація (pydantic, `strict`) кидає `ConfigValidationError(path=...)`, а `path` вказує точно на поле:
`variables.T.terms.WEAK_UP.points`, `variables.V.terms.LO.sigma`, `variables.T.terms.NEUTRAL.points[1]`,
`variables.U.range`, `variables` (бракує змінної), `defuzz.grid_nodes` (Сімпсон з непарною кількістю
інтервалів). Метадані змінної (`source`, `source_run_id`, `provisional`, `silhouette` і довільні
додаткові ключі) зберігаються в `LinguisticVariable.meta`. Ключ `provisional` мусить бути bool.
Формат, який пише `regimes.calibrate_mf.write_membership_yaml`, завантажується без змін (перевірено).

## `fuzzy/rules.py`

| Сутність | Сигнатура / поведінка |
|---|---|
| `Rule(id, antecedent, consequent, w=1.0)` | `antecedent = {"T": терм \| "any", "R": ..., "V": ...}`, `.to_dict()` → `{id, if, then, w}` |
| `RuleBase(rules: tuple[Rule,...], version=3, meta={})` | Конструктор повноту **не** перевіряє. `len()`, ітерація, `.by_id(id)`, `.lookup(T, R, V)` (KeyError), `.with_weights({id: w}) -> RuleBase` (копія, w ∈ (0; 1]), `.to_dict()` |
| `load_rulebase(src, membership, *, production=True, require_complete=True) -> RuleBase` | `src`: як у `load_membership` (`None` → `config/rules_mamdani.yaml`) |
| `rulebase_from_dict(data, membership, *, production, require_complete)` | те саме зі словника |
| `validate_rulebase(rb, membership, *, production=True, require_complete=True) -> None` | семантична перевірка вже побудованої бази |
| `rule_table(rb, membership) -> {V: [[U-терм за T] за R]}` · `format_rule_table_md(rb, membership)` | таблиця для Додатка А |

Помилки `ConfigValidationError.path` (для 422 в API): `rules` (не список або кількість ≠ 45; у
повідомленні перелічено непокриті комбінації), `rules[i].id` (дублікат id), `rules[i].if.T`
(невідомий терм), `rules[i].if.R` (змінну пропущено), `rules[i].if.Q` (зайва змінна), `rules[i].then`,
`rules[i].w` (не число, поза (0; 1] або `production=True` і w ≠ 1.0), `rules[i].if` (дублікат
антецедента, у повідомленні id першого правила), `rules[i].<key>` (зайвий ключ).
`production=False` дозволяє w ∈ (0; 1] і потрібен лише для тесту-контрприкладу. `require_complete=False`
знімає вимогу «рівно 45, кожна комбінація раз», але дублікати антецедентів і далі заборонені. Терм `"any"`
розгортається в усі терми змінної, тож у повній базі він неможливий.

`config/rules_mamdani.yaml`: 45 правил R01…R45 у порядку V (LO, MID, HI) → R (SELL, NO, BUY) → T
(STRONG_DOWN … STRONG_UP), усі `w: 1.0`. Таблиця є в `docs/figures/fuzzy_rules_table.md`.
Індекси t ∈ {−2..2}, r ∈ {−1,0,1}, c ∈ {−2..2}:
* **LO** (домінує реверсія): c = clip(2r + trunc(t/2), −2, 2);
* **MID** (тренд перемагає у конфлікті): c = t при t ≠ 0, інакше r; при sign(r) = sign(t) і |t| = 1 маємо c = t + r;
* **HI** (усе до HOLD): c = sign(t) лише при sign(t) = sign(r) ≠ 0, інакше 0.

Інваріанти, які вичерпно перевіряють тести: c не спадає за t (при фіксованих r, V) і за r (при фіксованих t, V),
c(−t, −r, V) = −c(t, r, V), R01 = (STRONG_DOWN, SELL_PRESSURE, LO) → STRONG_SHORT.

## `fuzzy/mamdani.py`

`MamdaniEngine(membership, rulebase, scheme=None, nodes=None)` реалізує `InferenceEngine`, `name = "mamdani"`.
Якщо `scheme`/`nodes` = None, значення беруться з `membership.defuzz` (робочі: `"trapezoid"`, 201).
Конструктор структурно перевіряє базу: терми існують, дублікатів немає. Невідомий терм дає
`ConfigValidationError`, невідома схема або Сімпсон з непарною кількістю інтервалів дають `ValueError`.

| Метод | Що повертає |
|---|---|
| `infer(T, R, V) -> FuzzyResult` | `u_raw ∈ [−1; 1]`; `inputs` (як передано); `memberships = {"T": {терм: μ}, "R": ..., "V": ...}` (входи обрізані до діапазонів); `fired` — лише α > 0, сортування (−α, rule_id); `grid` (201 вузол, лише читання); `mu_agg`; `engine="mamdani"`; `extras = {"area": ∫μ_agg (тією ж квадратурою), "height": max μ_agg}` |
| `infer_u(T, R, V) -> float` | лише u_raw, **побітово** дорівнює `infer(...).u_raw`. Швидкий шлях для бектесту |
| `infer_batch(T, R, V) -> ndarray` | broadcast масивів, збіг з `infer_u` ≤ 1e−12 |
| `consequent_strengths(T, R, V) -> ndarray (5,)` | β_k = max{α_r : D_r = k} у порядку термів U |
| `with_grid(nodes=None, scheme=None) -> MamdaniEngine` | інша сітка чи схема |
| `.membership`, `.rulebase`, `.grid`, `.consequent_mu` (5 × nodes, лише читання), `.scheme`, `.nodes` | |

Формули: α_r = w_r·min(μ_A(T), μ_B(R), μ_C(V)), де `"any"` дає 1.0; μ_agg(u) = max_k min(β_k, μ_k(u))
(тотожне max_r min(α_r, μ_{D_r}(u)), це перевірено побітово). Центроїд рахується на сітці u_j = −1 + jΔ.
Порожня активація дає `u_raw == 0.0` рівно, не NaN. Нескінченний або NaN вхід дає `ValueError`.
Рушій серіалізується pickle, отже придатний для воркерів `run_parallel`. `default_engine(scheme=None, nodes=None)`
збирає рушій із робочих конфігів (`production=True`).

Швидкодія (Apple M3, CPython 3.12.12, numpy 2.5.3, медіана 7 повторів по 10⁴ випадкових входів): `infer_u` 7.7 мкс,
`infer` 19.1 мкс, `infer_batch` 0.88 мкс на точку, `LinearVoteEngine.infer_u` 0.16 мкс.

## `fuzzy/defuzz.py`

| Функція | Поведінка |
|---|---|
| `centroid(grid, mu, scheme="trapezoid") -> float` | `rect` (Σu_jμ_j/Σμ_j), `trapezoid`, `simpson` (парна кількість інтервалів). Σw·μ = 0 → 0.0 |
| `centroid_weighted(w, wu, mu, lo, hi)` | ядро з попередньо обчисленими вагами |
| `quadrature_weights(nodes, scheme, lo=-1, hi=1)` | ваги з урахуванням Δ (Σw = довжина відрізка для trapezoid/simpson) |
| `make_grid(nodes, lo=-1, hi=1)` | u_j = c + h·(2j − n)/n, точно антисиметрична, кінці точні |
| `nodes_for_delta(delta, lo=-1, hi=1)` | (hi−lo)/Δ + 1; якщо (hi−lo)/Δ не ціле, то `ValueError` |
| `aggregate(betas, consequent_mu)` | max_k min(β_k, μ_k(u_j)) |
| `exact_centroid(mfs, betas, lo=-1, hi=1)` | точний центроїд для tri/trap-термів: аналітичне інтегрування між усіма точками зламу. Для gauss дає `ValueError` |
| `convergence_study(engine, inputs, deltas=(0.02,0.01,0.002,0.001), *, schemes=(...), fit_base_delta=0.04, fit_levels=8) -> dict` | див. нижче |

`convergence_study` повертає `{"reference": "exact_piecewise_linear", "n_inputs": int, "deltas": [...],
"schemes": {scheme: {"rows": [{"delta", "nodes", "max_abs_err", "rms_err", "p_brief_median",
"p_brief_q1", "p_brief_q3", "p_brief_n"}], "fitted_order": float, "fit_deltas": [...], "fit_rms_err": [...],
"criterion_max_diff": max|u(0.01) − u(0.001)|}}}`. Від `engine` потрібні лише `.membership` і
`.consequent_strengths` (Protocol `StrengthEngine`).

## `fuzzy/linear.py`

`LinearVoteEngine(weights={"T": 0.5, "R": 0.5})`, `name = "linear"`:
u = clip((w_T·T + w_R·R)/(|w_T| + |w_R|), −1, 1). V за побудовою не впливає, тож базова лінія «сліпа» до режиму.
`infer` повертає `fired=()`, `memberships={}`, `extras={"vote_T", "vote_R"}`. Також є `infer_u`, `infer_batch`
і `.weights`. Ключі, відмінні від T/R, нульові або нескінченні ваги дають `ValueError`.

## `fuzzy/surface.py`

* `control_surface(engine, V, n=41, t_range=(-1,1), r_range=(-1,1)) -> (T_grid, R_grid, U)` повертає
  масиви (n, n): рядки відповідають R, стовпці T, `U[i, j] = u(T_j, R_i, V)`. Працює з будь-яким
  `InferenceEngine`; за наявності `infer_batch` використовує векторизований шлях.
* `evaluate_grid(engine, T, R, V)` обчислює u_raw з broadcast.
* `monotonicity_scan(engine, *, n_T=401, n_R=81, n_V=41, gap=1.0, tol=1e-9) -> MonotonicityReport`
  повертає `decreasing_steps/total_steps`, `max_reversal` і точку `reversal_at = (T1, T2, R, V, u1, u2)`,
  а також `min_gap_slack` («груба» монотонність при T₂ ≥ T₁ + gap) з точкою `gap_slack_at = (T1, T2, R, V)`,
  де T2 — фактичний argmin u на [T1 + gap; 1]. Усі числа — оцінки на сітці: справжній мінімум запасу
  може бути меншим, а максимум реверсу — більшим.

**Монотонність (важливо для інтеграції та звіту).** u(T) при w ≡ 1 **не** монотонна локально. Для
МФ зі специфікації максимальний реверс ≈ 0.159, що більше за поріг виходу гістерезису 0.12.
Виконується лише «груба» монотонність: T₂ ≥ T₁ + 1.0 ⇒ u(T₂) ≥ u(T₁), із запасом лише ≈ 0.00723
(T₁ = 0, T₂ = 1, R ≈ 0.007, V = 1). Для інших конфігурацій МФ
її треба перевіряти заново. Подробиці: `docs/deviations.d/fuzzy.md` (FZ-01),
`docs/figures/fuzzy_monotonicity.md`.

## Скрипти й артефакти

| Скрипт | Виходи |
|---|---|
| `scripts/plot_membership.py [--membership P] [--out P]` | `docs/figures/fuzzy_membership.png` (4 панелі T, R, V, U) |
| `scripts/plot_control_surface.py [--n 201] [--skip-scan]` | `docs/figures/fuzzy_control_surface.png` (V = 0.2 / 0.5 / 0.9, ізолінії ±0.12 / ±0.25), `fuzzy_rules_table.md`, `fuzzy_monotonicity.md` |
| `scripts/defuzz_convergence.py [--n-inputs 60] [--seed 20260918]` | stdout та `docs/figures/fuzzy_defuzz_convergence.md` і `.png` |

Після калібрування V (`regimes`) треба перезапустити всі три скрипти. Числа монотонності в
`tests/property/test_fuzzy_property.py` прив'язані до зафіксованих МФ зі специфікації, тож калібрування
їх не ламає, але висновок для нової конфігурації дає лише `fuzzy_monotonicity.md`.
