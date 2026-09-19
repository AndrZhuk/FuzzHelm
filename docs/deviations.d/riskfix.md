# Розходження «спека ↔ реальність»: `riskfix` (політика COOLDOWN, паспорт інструмента, VaR у кривій)

Формат: **що в спеці / що насправді / що зробив / чим обґрунтовано**. Числа — з команд, названих у кожному пункті;
45-денні прогони — `docs/figures/riskfix_cooldown_policy.md`.

## RF-01. COOLDOWN: `scaled_entries` (типово, рішення автора) замість буквального reduce-only (закриває ENG-14)
- **Що в спеці (§5.12):** `WARNING → COOLDOWN … (reduce-only)`; `COOLDOWN → WARNING: DD ≤ 0.05 ∧ dwell ≥ 30`;
  `κ_mode(COOLDOWN) = 0.25`; оцінка гіршого випадку `n_min ≥ (DD_max − DD₀)/v_max(COOLDOWN)`,
  `v_max = κ_mode·ρ_base·E·(1+κ_slip)`.
- **Що насправді:** брифінг внутрішньо неузгоджений. За reduce-only κ_mode(COOLDOWN) = 0.25 не множить жодної нової
  цілі, а `v_max(COOLDOWN) > 0` припускає торгівлю в COOLDOWN. Для пласкої книги reduce-only робить COOLDOWN
  поглинаючим (капітал і DD сталі, умова виходу недосяжна) — ENG-14: дефолтна стратегія на реальних даних
  заблокована з ~3-ї доби, 93.6 % вікна.
- **Що зробив:** перемикач `state_machine.cooldown_policy` у `config/risk_limits.yaml`
  (`risk.config.CooldownPolicy`):
  * `scaled_entries` (**типово**, варіант «г» з ENG-14 — рішення автора): у COOLDOWN дозволено **новий вхід** — з
    пласкої книги або новий бік розвороту (`RiskContext.base_qty = 0`); розмір уже помножений на κ_mode = 0.25 у
    сайзері (`q = floor(κ_mode·min(q_atr, q_vt, q_lev))`). Збільшувати **наявну** позицію заборонено — для позицій,
    відкритих до COOLDOWN, це вимога рішення; для відкритих у COOLDOWN — консервативне спрощення (рушій і так не
    доторговує, ENG-04, тож різниці в бектесті немає);
  * `reduce_only` — буквально §5.12, лишено для відтворення ENG-14 у звіті.
  Реалізація — лише в гейті `risk_mode` (`RiskModeGate`): `observed` = тяжкість стану (0…3), `limit` = найвищий
  рівень, у якому **цей вид** приросту дозволений: 1 (WARNING) для збільшення наявної позиції за будь-якої політики,
  2 (COOLDOWN) для нового входу за `scaled_entries`, 1 — за `reduce_only`; VETO ⇔ приріст > 0 ∧ observed > limit.
  Отже `risk_event.limit_value` правила `risk_mode` тепер залежить від контексту (раніше завжди 1); у `payload` —
  `cooldown_policy`, `new_entry`, `reduce_only` (= наявну позицію не збільшувати), `entries_allowed`.
  Не змінено: автомат (таблиця 4×6, пороги, витримка, гістерезис, засувка HALTED лише admin), шість лімітів,
  алгебра вердиктів. `BacktestConfig.cooldown_policy` перекриває дерево і завжди пишеться в нього явно, тож входить у
  `config_hash` (типове дерево без ключа дає той самий хеш, що й явне `scaled_entries`); гаряча заміна лімітів
  (`TradingLoop.apply_risk_limits`) без ключа зберігає політику прогону. CLI: `runner --cooldown-policy`.
  `RiskStateMachine.entries_allowed`, `.cooldown_policy`; `.reduce_only` лишився «наявну позицію не збільшувати»
  (COOLDOWN за обох політик, HALTED).
- **Чим обґрунтовано:** монотонність (§5.11) зберігається — гейт повертає лише ALLOW/VETO, множина VETO за
  `reduce_only` містить множину VETO за `scaled_entries`, решта шести правил від політики не залежить (property
  `test_risk_chain_never_exceeds_requested_or_kappa_scaled_size`: приріст ≤ запиту, нова експозиція ≤ розміру сайзера з
  κ_mode стану = `floor(κ_mode·q_raw(NORMAL))`, у COOLDOWN приріст лише як новий вхід за `scaled_entries`, записи шести
  лімітів однакові за обох політик). Тести: `tests/unit/test_riskfix_cooldown.py` — (а) вхід з пласкої книги в
  COOLDOWN = `to_decimal(0.25·q_raw(NORMAL))`, `|q − q_NORMAL/4| < step`, як на рівні компонентів, так і в циклі
  після зняття HALTED (незалежний перерахунок входів сайзера); (б) позицію, відкриту до COOLDOWN, не збільшити за
  обох політик; (в) `reduce_only` відтворює поглинання ENG-14 у циклі (капітал сталий, жодного виконання, кожен
  намір — VETO `risk_mode`), а `scaled_entries` на тому самому шляху виходить із COOLDOWN (RECOVERY); автомат
  проходить той самий шлях за обох політик. Property рушія `test_cooldown_never_increases_an_open_position_and_reduce_only_never_enters`
  (тісні пороги: у 30 прикладах — 350 входів у COOLDOWN за `scaled_entries` і 783 VETO за `reduce_only`, лічильник
  з одноразового прогону). Мутації гейта «старе буквальне правило» і «scaled дозволяє збільшувати наявну
  позицію» валять відповідно 4 і 2 тести прогону `tests/unit/test_riskfix_cooldown.py` +
  `tests/property/test_risk_property.py` (перевірено; код гейта після перевірки відновлено).
- **Що показали реальні дані** (`runner --db <SYM> --report --repeats 1 --cooldown-policy <P>`, деталі —
  `docs/figures/riskfix_cooldown_policy.md`): `reduce_only` відтворює ENG-14 біт-у-біт (BTC `equity_hash
  8a82f814e93855ce…`, як до правки); `scaled_entries` — 190 (BTC) / 221 (ETH) входів у COOLDOWN з κ = 0.25, валовий
  результат цих угод +0.38 / −24.77 USDT при комісіях 601.28 / 572.72 USDT, DD 0.06 → 0.12 і **HALTED** на барі
  11 726 (8.14 доби) / 11 325 (7.86 доби); підсумок −12.00 % / −11.95 % проти −5.99 % / −5.98 % за `reduce_only`.
  Поглинання усунуто, засувка 12 % спрацювала за задумом; корінь збитку — очікування стратегії після витрат
  (валовий PnL за 45 днів ≈ 0 при комісіях ~1 200 USDT), а не автомат.
- **Наслідок для паспорта (рецензія):** політика пишеться в дерево явно, тож `config_hash` змінився для **всіх**
  прогонів, зокрема й буквального: BTC `reduce_only` — `61cbcda3dda5c2c4…` при тому самому `equity_hash
  8a82f814…`, що й у першому прогоні з `config_hash 7f91fc3408ff0766…` (`docs/figures/first_run_metrics.md`).
  Різниця — лише це поле: `BacktestConfig(cooldown_policy="reduce_only").identity_dict()` без поля
  `cooldown_policy` і без ключа `trees.risk_limits.state_machine.cooldown_policy` дає рівно
  `7f91fc3408ff0766c232eb339f6ea935a1a83294ce4f72128dbe2214fc56575e` (однорядок `uv run python -c …` над
  `backtest.manifest.config_hash`). Паспорти прогонів, записаних до правки (без ключа), виконувались за буквальною
  політикою, а те саме дерево зараз читається як `scaled_entries` — для їх відтворення потрібен явний
  `cooldown_policy: reduce_only`.
- **Перевірка на реальних даних (рецензія):** `runner --report` тепер для кожного входу/розвороту з трасою
  сайзера звіряє κ сайзера з κ_mode стану рішення і ціль із `floor(κ_mode·min(q_atr, q_vt, q_lev))`: 303/303,
  113/113 (BTC scaled/reduce_only), 358/358, 137/137 (ETH) — і κ, і межа виконані в кожному; у COOLDOWN
  (`scaled_entries`) це 190 / 221 входів з κ = 0.25. Property рушія
  `test_cooldown_never_increases_an_open_position_and_reduce_only_never_enters` посилено тим самим незалежним
  перерахунком (обгортка сайзера циклу): до посилення мутація «сайзер у COOLDOWN отримує κ = 1» (або 0.5)
  проходила обидва property-файли і ловилась лише одним unit-тестом; тепер її ловить і property.

## RF-02. Специфікація інструмента — у паспорті прогону через `dataset_hash` (закриває ENG-13)
- **Що в спеці/завданні:** tick, step, minNotional, mmr, maint_amount, символ мають бути частиною паспорта прогону —
  у конфігу, з якого рахується `config_hash`, або окремим хешованим полем у `run.config`.
- **Що насправді:** `BacktestConfig` інструмента не знає (його дає `Dataset`), а API (`api/backtest_runner.py:439`,
  не мій компонент) рахує `cfg.config_hash` ДО прогону, пише `run.config = cfg.identity_dict()` і звіряє
  `manifest.config_hash == cfg.config_hash`; якщо рушій додав би специфікацію в хешований конфіг, ця звірка впала б
  на кожному прогоні API. `RunManifest` (`backtest/manifest.py`) — теж не мій файл.
- **Що зробив:** специфікація входить у **`dataset_hash`**: `Dataset.columns()` дає додаткову колонку
  `instrument_spec` — байти `canonical_json(instrument_spec(inst))` (uint8), де `instrument_spec` = біржа, символи,
  тип контракту, `tick_size, step_size, min_notional, mmr, maint_amount` (Decimal, квантовані до 1e−18 і записані
  рядком — '0.1' з БД і '0.10' з exchangeInfo дають ту саму специфікацію) і `max_leverage` (рушій бере його як біржове
  плече `L_set`). Обидва боки звірки API (`ds.dataset_hash` і `manifest.dataset_hash = hash(dataset.columns())`)
  рахують ту саму функцію, тож звірка тримається; інша mmr ⇒ інший `dataset_hash` ⇒ інший ключ `ux_run_identity` і
  інший `deterministic_run_id`. `BacktestResult.instrument_spec` — сама специфікація (для звіту; `runner --report`
  її друкує). Хеш лише свічок (`data/dataset_window.json`, `backtest.runner.load_db_window`) — як і раніше
  `dataset_hash(CandleArrays.columns())`, не змінився.
- **Чим обґрунтовано:** специфікація — дані біржі (як свічки й фандинг, що вже в `dataset_hash`), а не параметр
  стратегії: той самий `config_hash` для BTC і ETH лишається «та сама стратегія». Тести
  `tests/unit/test_riskfix_passport.py`: зміна mmr змінює `dataset_hash`, ідентичність manifest і run_id, а
  `config_hash` — ні; кожне з полів (tick, step, minNotional, maint_amount, max_leverage, символи) змінює хеш;
  масштаб Decimal — ні. **Наслідок:** `dataset_hash` усіх нових прогонів відрізняється від записаних до цієї правки
  (напр. BTCUSDT `99382675fda6dcab…`). **Залишок:** сам вміст специфікації в `run.config` API не пише (там
  `identity_dict()`); пропозиція власнику API — додати `run.config["instrument"] = result.instrument_spec`
  (хеш її вже фіксує `dataset_hash`). Масштаб Decimal tick-а (ENG-15) і далі змінює лише текстову форму журналу.

## RF-03. VaR₉₅/CVaR₉₅ у `equity_point`: гроші, W = 500, лише повні вікна; рушій заповнює сам
- **Що в спеці (§5.13):** `r^p_t = ΔE_t/E_{t−1}`, вікно W = 500, `VaR₉₅ = −Quantile₀.₀₅`, `CVaR₉₅ = −(1/m)Σ_{i≤m} r_(i)`,
  `m = ⌊0.05W⌋`; колонки `equity_point.var95/cvar95` (NUMERIC(38,18)).
- **Що насправді було:** рушій точки кривої без VaR (None), їх рахував лише `workers.persist` — з розгортанням вікна
  від 20 дохідностей (`m = ⌊0.05·n⌋` = 1 на 20 точках: інша, грубіша оцінка, ніж §5.13).
- **Що зробив:** `risk.var.rolling_var_cvar` (частки; повні вікна — векторизовано блоками `np.partition`, O(W) на
  точку в C, без матриці N×W), `risk.var.var_cvar_money` (гроші) і `risk.var.RollingVarCvar` (покроково, live) — одне
  ядро `_tail_stats`, тож пакетні й покрокові числа однакові до біта (тест). Конвенція: `var95_t = E_t · VaR̂₉₅(r_{t−499..t})`
  — гроші (USDT), додатне = збиток, вікно закінчується на t включно («ризик кривої станом на t»; прогноз для Купця —
  `rolling_var_breaches`, строго на минулому); `None`, поки дохідностей < 500 (точки 0…499); значення не
  обрізаються до 0 (R-04). `run_backtest` заповнює `BacktestResult.equity_points` одним векторизованим проходом;
  `workers.persist.plan_backtest` бере числа рушія (за нетипових `var_window/var_min_obs` — перераховує);
  `LivePersister` — `RollingVar` (= `RollingVarCvar`). `VAR_MIN_OBS = VAR_WINDOW = 500`; −0.0 нормалізується в 0.
- **Чим обґрунтовано / виміряно:** §5.13 задає W = 500; 45-денні прогони (`runner --db … --report`): 64 300 з 64 800
  точок визначені, `CVaR ≥ VaR` і `VaR ≥ 0` — у всіх 64 300 (на цих кривих), прохід над 64 800 точками —
  0.098–0.105 с. Тести `tests/unit/test_riskfix_var.py`: None до 500-ї дохідності; `CVaR ≥ VaR ≥ 0` на кривій
  фікстури; VaR = E_t·(оцінка `historical_var_cvar` на тому самому вікні); рядки плану запису = вихід рушія = live-шлях.
- **Відкрите (не мій файл):** `tests/unit/test_workers.py::test_plan_backtest_maps_engine_result_and_journal_chain`
  (рядки 88–89) і `tests/integration/test_worker_db.py` (рядки 175, 307–308, 383: `test_replay_worker_persists_run_with_full_passport`,
  `test_persist_backtest_batch_writes_passport_journal_and_var`, `test_api_backtest_runner_persists_journal_chain_and_var`)
  закріплюють стару конвенцію
  «VaR з 21-ї точки»; за RF-03 (W = 500, None до 500 дохідностей) вони мають очікувати `var95 is None` для
  `curve[:500]` (у 45-хвилинній сесії — для всіх точок). Власнику workers/тестів: оновити очікування (це зміна
  конвенції, а не послаблення перевірки); тест із явним `min_obs=20` (`test_equity_point_var_cvar_is_rolling_historical_estimate_in_money`)
  проходить без змін.

## RF-04. Оцінка гіршого випадку: κ входу, а не поточного стану; відкат нереалізованого прибутку (ENG-04, R-12)
- **Що в спеці (§5.12):** `|ΔE| ≤ κ_mode·ρ_base·E_t·(1+κ_slip)`, `κ_mode` незростаюча за DD ⇒ `n_min ≥ (DD_max − DD₀)/v_max(COOLDOWN)`.
- **Що насправді:** (1) розмір фіксується на вході (ENG-04), тож відкрита позиція при переході NORMAL → WARNING /
  COOLDOWN не зменшується — її бюджет `κ_entry·ρ·|u|·E_entry`, а не `κ_mode(поточний)·…`; монотонність κ за DD
  стосується лише НОВИХ входів; (2) DD рахується від піку, що містить нереалізований прибуток: позиція, що дійшла майже
  до тейку (`m·Δ_stop`, m = 2, ENG-01), а потім за бар — до стопа, дає приріст DD за бар до `(1 + m + κ_slip)·κ_entry·ρ`.
- **Що зробив:** формулу `per_bar_loss_bound` не змінено (буквальна §5.12, тест поза моїм пакетом), докстрінги
  `DrawdownSpeedBound`/`per_bar_loss_bound` (`risk/margin.py`) перелічують точні припущення: κ = κ_mode на вході
  відкритої позиції; `v_max(COOLDOWN)` діє лише для позицій, відкритих у COOLDOWN (`scaled_entries`), поки жива
  позиція з вищим κ_entry — межа з її κ_entry, за `reduce_only` після її закриття v = 0; E_t — капітал на вході; межа
  не враховує відкату нереалізованого прибутку.
- **Доповнення рецензента:** (3) витрати виконання в межу теж не входять — κ_slip моделює лише проковзування
  стопа. Номінал сайзер обмежує `κ·L_max·E` (`q_lev`), тож комісія taker кожного боку — до `f_taker·κ·L_max·E_entry`
  = 0.12 %·κ·E при `f_taker = 0.0004` (`config/cost_model.yaml`) і `L_max = 3`, до 0.24 %·κ·E за коло — порівнянно з
  бюджетом κ·ρ = 0.5 %·κ; додано пунктом 6) до докстрінга `DrawdownSpeedBound`. **Виміряно** (`runner --db <SYM>
  --report --cooldown-policy <P>`, рядок «max per-bar DD increase by state of the previous bar»): найбільший
  приріст DD за бар — NORMAL 0.00170 / 0.00133 (BTC / ETH; κ·ρ = 0.005), WARNING 0.00086 / 0.00073 (0.0025),
  COOLDOWN 0.00064 / 0.00056 за `scaled_entries` і 0.00038 / 0.00033 за `reduce_only` (0.00125; за `reduce_only`
  це позиція, відкрита до COOLDOWN, — бар 1 375 при вході в COOLDOWN на 1 373), HALTED 0 — на цих даних межа з
  κ_slip = 0 не пробита в жодному стані, але це спостереження, а не доведення.
- **Чим обґрунтовано:** брифінг сам вимагає формулювати це як оцінку за заданих припущень; у звіті — саме так.

## RF-05. Рецензія riskfix (2026-09-19): що перевірено, що виправлено
- **Перевірено повторним прогоном** (команда з RF-01, чотири прогони послідовно): кожне число таблиці
  `docs/figures/riskfix_cooldown_policy.md` (угоди, виконання, комісії, валовий PnL, фандинг, капітал, total_return,
  max_drawdown, частки барів у станах, переходи, бари HALTED, входи й VETO в COOLDOWN, `equity_hash`, VaR-зведення)
  збіглося; час повного прогону 4.06 / 4.58 / 4.05 / 4.77 с (BTC scaled / BTC reduce_only / ETH scaled / ETH
  reduce_only), прохід VaR — 0.095–0.106 с. Засувка HALTED: `--report` іде з `check_invariants=True`, а ця
  перевірка рушія (`_check_accounting(latched=…)`) падає на будь-якому прирості позиції під засувкою — обидва
  прогони `scaled_entries`, що дійшли до HALTED, завершились без помилки; за `reduce_only` — 0 входів, рішених у
  COOLDOWN.
- **Мутаційна перевірка** (ізольована копія дерева, не робоче дерево): «гейт як до правки», «scaled дозволяє
  збільшувати наявну позицію», «scaled дозволяє вхід у HALTED», «сайзер у COOLDOWN з κ = 1 / 0.5» — кожну ловить
  щонайменше один тест `tests/unit/test_riskfix_cooldown.py`; κ-мутації тепер ловить і property рушія (RF-01).
- **Інтеграційні тести** (окрема база `fuzzhelm_test_riskfix` на тестовому сервері 5443, щоб не чіпати
  `fuzzhelm_test` іншого агента; `-m integration tests/integration/{test_worker_db,test_api_db,test_trading}.py`):
  24 passed, 3 failed — рівно ті три тести `test_worker_db.py`, що закріплюють «VaR з 21-ї точки» (RF-03); звірка
  паспорта API зі специфікацією в `dataset_hash` (RF-02) проходить.
- **Виправлено:** посилено property рушія (κ сайзера і межа `floor(κ_mode·q_raw(NORMAL))` для кожного нового
  входу, виконана позиція ≤ схваленої цілі); у межу гіршого випадку додано припущення про витрати (RF-04); у
  `runner --report` — дві перевірки з реальних даних (швидкість DD за станом, κ на трасах сайзера); задокументовано
  зміну `config_hash` усіх прогонів (RF-01).
