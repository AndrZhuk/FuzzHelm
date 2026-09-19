# Тести за групами A–N брифінгу (§10)

Автор: Андрій Жук, 2026. Згенеровано `uv run python -m tests.helpers.brief_test_groups` на `HEAD 415acbd`. Жодне число не введено вручну.

## Підсумок

* Зібрано всього (`pytest --collect-only -q -m ""`): **1034** вузлів (1034 tests collected).
* Типовий прогін `make test` (`uv run pytest`, відбір `addopts`: не `integration`/`live`/`slow`): **979** (979/1034 tests collected (55 deselected)).
* Інтеграційні (`-m integration`, PostgreSQL із docker-compose.test.yml): **54** (54/1034 tests collected (980 deselected)); решта поза типовим прогоном — 1 (маркер `slow`).
* Названих у §10 тест-функцій: **133** (сума заголовків груп — 131; заголовок §10 — «92 кейси», див. D-02). Визначено в tests/ (grep `def <назва>(`): **133**; зібрано pytest: **133**; вузлів від них (з параметризацією): **198**.
* Додаткових вузлів (не названих у брифінгу): **836** (у файлах груп — 437, у файлах поза групами — 399).

| група | назва | у заголовку §10 | названо | є (grep) | зібрано | вузлів названих | усіх вузлів у файлах групи | додаткових |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| A | Архітектурні | 3 | 3 | 3 | 3 | 6 | 7 | 1 |
| B | Канонічна серіалізація і журнал | 8 | 8 | 8 | 8 | 8 | 11 | 3 |
| C | Нормалізація та інжест | 13 | 13 | 13 | 13 | 29 | 69 | 40 |
| D | Якість даних | 7 | 7 | 7 | 7 | 7 | 20 | 13 |
| E | Індикатори | 9 | 9 | 9 | 9 | 9 | 21 | 12 |
| F | Детектори | 8 | 8 | 8 | 8 | 8 | 16 | 8 |
| G | Нечітке ядро | 17 | 17 | 17 | 17 | 17 | 65 | 48 |
| H | Агрегація і κ | 8 | 8 | 8 | 8 | 8 | 76 | 68 |
| I | Сайзинг | 9 | 9 | 9 | 9 | 13 | 24 | 11 |
| J | Ризик | 16 | 18 | 18 | 18 | 43 | 75 | 32 |
| K | Виконання і бектест | 13 | 13 | 13 | 13 | 17 | 66 | 49 |
| L | Метрики | 6 | 6 | 6 | 6 | 6 | 15 | 9 |
| M | Інтеграційні | 6 | 6 | 6 | 6 | 14 | 31 | 17 |
| N | API та e2e | 8 | 8 | 8 | 8 | 13 | 139 | 126 |
| — | поза групами | — | — | — | — | — | 399 | 399 |
| **Σ** | | 131 | 133 | 133 | 133 | 198 | 1034 | 836 |

«Додаткових» = усі вузли файлів групи − вузли дослівних назв (евристика прив'язки файлу — див. заголовок генератора, XA-13).

## A. Архітектурні

Файли групи: `tests/arch/test_decimal_float_boundary.py`, `tests/arch/test_determinism_boundary.py`, `tests/arch/test_mainnet_allowlist.py`. Дослівних: 3/3; додаткових вузлів: 1.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_no_wallclock_in_core` | `tests/arch/test_determinism_boundary.py:17` | 1 | так |
| `test_decimal_float_boundary_is_single_choke_point` | `tests/arch/test_decimal_float_boundary.py:21` | 1 | так |
| `test_mainnet_host_is_rejected_by_config` | `tests/arch/test_mainnet_allowlist.py:17` | 4 | так |

## B. Канонічна серіалізація і журнал

Файли групи: `tests/property/test_dedup_property.py`, `tests/unit/test_core_digest_journal.py`. Дослівних: 8/8; додаткових вузлів: 3.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_canonical_json_rejects_float` | `tests/unit/test_core_digest_journal.py:73` | 1 | так |
| `test_decimal_quantized_not_normalized` | `tests/unit/test_core_digest_journal.py:98` | 1 | так |
| `test_keys_sorted_lexicographically` | `tests/unit/test_core_digest_journal.py:113` | 1 | так |
| `test_state_digest_stable_across_processes` | `tests/unit/test_core_digest_journal.py:131` | 1 | так |
| `test_hash_chain_links_prev_hash` | `tests/unit/test_core_digest_journal.py:145` | 1 | так |
| `test_tampered_payload_breaks_chain_at_exact_seq` | `tests/unit/test_core_digest_journal.py:176` | 1 | так |
| `test_event_uid_stable_across_restart` | `tests/unit/test_core_digest_journal.py:224` | 1 | так |
| `test_dedup_idempotent_under_permutation` | `tests/property/test_dedup_property.py:76` | 1 | так |

## C. Нормалізація та інжест

Файли групи: `tests/property/test_retry_property.py`, `tests/unit/test_crosscheck.py`, `tests/unit/test_normalize.py`, `tests/unit/test_rest_ingest.py`. Дослівних: 13/13; додаткових вузлів: 40.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_kline_ms_to_ns_exact` | `tests/unit/test_normalize.py:94` | 1 | так |
| `test_price_quantized_to_tick_half_even` | `tests/unit/test_normalize.py:129` | 8 | так |
| `test_qty_floored_to_step` | `tests/unit/test_normalize.py:157` | 6 | так |
| `test_reject_below_min_notional_with_code` | `tests/unit/test_normalize.py:166` | 1 | так |
| `test_unknown_field_raises_normalization_error` | `tests/unit/test_normalize.py:203` | 5 | так |
| `test_event_and_ingest_time_never_mixed` | `tests/unit/test_normalize.py:259` | 1 | так |
| `test_paginator_stitches_segments_with_one_bar_overlap` | `tests/unit/test_rest_ingest.py:121` | 1 | так |
| `test_paginator_detects_gap_in_overlap` | `tests/unit/test_rest_ingest.py:145` | 1 | так |
| `test_token_bucket_blocks_on_weight_exhaustion` | `tests/unit/test_rest_ingest.py:202` | 1 | так |
| `test_retry_after_header_honored` | `tests/unit/test_rest_ingest.py:287` | 1 | так |
| `test_backoff_full_jitter_within_cap` | `tests/property/test_retry_property.py:23` | 1 | так |
| `test_binance_kraken_price_crosscheck_flags_divergence_above_50bps` | `tests/unit/test_crosscheck.py:46` | 1 | так |
| `test_gap_detected_and_backfilled_idempotently` | `tests/unit/test_rest_ingest.py:421` | 1 | так |

## D. Якість даних

Файли групи: `tests/property/test_quality_property.py`, `tests/unit/test_quality.py`. Дослівних: 7/7; додаткових вузлів: 13.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_high_below_close_rejected` | `tests/unit/test_quality.py:86` | 1 | так |
| `test_price_not_multiple_of_tick_rejected` | `tests/unit/test_quality.py:104` | 1 | так |
| `test_dq_score_in_unit_interval` | `tests/property/test_quality_property.py:46` | 1 | так |
| `test_perfect_hour_scores_one` | `tests/unit/test_quality.py:210` | 1 | так |
| `test_timeliness_decays_exponentially` | `tests/unit/test_quality.py:232` | 1 | так |
| `test_ahp_weights_sum_to_one_and_cr_below_0_1` | `tests/unit/test_quality.py:153` | 1 | так |
| `test_mlp_autoencoder_flags_injected_spike_and_not_normal_bar` | `tests/unit/test_quality.py:297` | 1 | так |

## E. Індикатори

Файли групи: `tests/property/test_features_property.py`, `tests/unit/test_indicators.py`. Дослівних: 9/9; додаткових вузлів: 12.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_rsi14_matches_wilder_golden_csv` | `tests/unit/test_indicators.py:53` | 1 | так |
| `test_atr14_matches_wilder_golden_csv` | `tests/unit/test_indicators.py:71` | 1 | так |
| `test_wilder_alpha_is_one_over_n_not_two_over_n_plus_one` | `tests/unit/test_indicators.py:145` | 1 | так |
| `test_ema_incremental_equals_batch` | `tests/unit/test_indicators.py:205` | 1 | так |
| `test_welford_stable_on_1e9_offset_series` | `tests/unit/test_indicators.py:219` | 1 | так |
| `test_donchian_deque_matches_naive_max` | `tests/property/test_features_property.py:31` | 1 | так |
| `test_parkinson_vol_exact_on_constant_range` | `tests/unit/test_indicators.py:279` | 1 | так |
| `test_ols_r2_near_one_on_clean_trend` | `tests/unit/test_indicators.py:315` | 1 | так |
| `test_indicators_return_none_before_warmup` | `tests/unit/test_indicators.py:362` | 1 | так |

## F. Детектори

Файли групи: `tests/property/test_detectors_property.py`, `tests/unit/test_detectors.py`. Дослівних: 8/8; додаткових вузлів: 8.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_all_detectors_bounded_and_no_nan_on_any_ohlcv` | `tests/property/test_detectors_property.py:92` | 1 | так |
| `test_all_detectors_are_pure` | `tests/unit/test_detectors.py:98` | 1 | так |
| `test_all_detectors_registered` | `tests/unit/test_detectors.py:71` | 1 | так |
| `test_ema_slope_positive_on_linear_uptrend` | `tests/unit/test_detectors.py:140` | 1 | так |
| `test_donchian_confidence_decays_with_staleness` | `tests/unit/test_detectors.py:156` | 1 | так |
| `test_rsi_convex_map_weak_in_middle` | `tests/unit/test_detectors.py:218` | 1 | так |
| `test_bollinger_confidence_drops_when_bandwidth_expands` | `tests/unit/test_detectors.py:242` | 1 | так |
| `test_pin_bar_score_zero_when_wicks_symmetric` | `tests/unit/test_detectors.py:276` | 1 | так |

## G. Нечітке ядро

Файли групи: `tests/property/test_fuzzy_property.py`, `tests/unit/test_fuzzy.py`. Дослівних: 17/17; додаткових вузлів: 48.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_tri_mf_peak_equals_one` | `tests/unit/test_fuzzy.py:84` | 1 | так |
| `test_trap_plateau_is_flat` | `tests/unit/test_fuzzy.py:97` | 1 | так |
| `test_gauss_mf_symmetry` | `tests/unit/test_fuzzy.py:112` | 1 | так |
| `test_mf_partition_of_unity_ruspini` | `tests/unit/test_fuzzy.py:123` | 1 | так |
| `test_mf_coverage_no_dead_zones` | `tests/unit/test_fuzzy.py:136` | 1 | так |
| `test_rulebase_yaml_has_exactly_45_rules` | `tests/unit/test_fuzzy.py:213` | 1 | так |
| `test_rulebase_no_duplicate_antecedents` | `tests/unit/test_fuzzy.py:225` | 1 | так |
| `test_rulebase_all_terms_exist_in_membership_config` | `tests/unit/test_fuzzy.py:238` | 1 | так |
| `test_rule_firing_is_min_of_memberships` | `tests/unit/test_fuzzy.py:340` | 1 | так |
| `test_dont_care_term_contributes_one` | `tests/unit/test_fuzzy.py:359` | 1 | так |
| `test_clipped_consequent_never_exceeds_alpha` | `tests/unit/test_fuzzy.py:377` | 1 | так |
| `test_aggregation_is_pointwise_max` | `tests/unit/test_fuzzy.py:392` | 1 | так |
| `test_centroid_of_symmetric_aggregate_is_zero` | `tests/unit/test_fuzzy.py:408` | 1 | так |
| `test_empty_activation_returns_exactly_zero` | `tests/unit/test_fuzzy.py:424` | 1 | так |
| `test_defuzz_grid_convergence_order_is_two_for_trapezoid` | `tests/unit/test_fuzzy.py:440` | 1 | так |
| `test_u_nondecreasing_in_trend_input` | `tests/property/test_fuzzy_property.py:99` | 1 | так |
| `test_weighted_rules_break_monotonicity_counterexample` | `tests/property/test_fuzzy_property.py:200` | 1 | так |

## H. Агрегація і κ

Файли групи: `tests/property/test_decision_property.py`, `tests/unit/test_decision.py`. Дослівних: 8/8; додаткових вузлів: 68.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_consensus_zero_when_all_confidences_zero` | `tests/unit/test_decision.py:146` | 1 | так |
| `test_consensus_between_min_and_max_contribution` | `tests/property/test_decision_property.py:64` | 1 | так |
| `test_membership_probabilities_sum_to_one` | `tests/property/test_decision_property.py:78` | 1 | так |
| `test_agreement_is_one_when_all_same_sign` | `tests/unit/test_decision.py:221` | 1 | так |
| `test_kappa_reaches_kmin_on_uniform_split` | `tests/unit/test_decision.py:259` | 1 | так |
| `test_kappa_monotone_in_agreement` | `tests/property/test_decision_property.py:91` | 1 | так |
| `test_entropy_handles_zero_probability` | `tests/unit/test_decision.py:276` | 1 | так |
| `test_decision_trace_contains_every_fired_rule` | `tests/unit/test_decision.py:411` | 1 | так |

## I. Сайзинг

Файли групи: `tests/property/test_sizing_property.py`, `tests/unit/test_sizing.py`. Дослівних: 9/9; додаткових вузлів: 11.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_position_size_inverse_to_atr` | `tests/unit/test_sizing.py:47` | 1 | так |
| `test_vol_target_halves_notional_when_vol_doubles` | `tests/unit/test_sizing.py:79` | 1 | так |
| `test_vol_target_clipped_at_bounds` | `tests/unit/test_sizing.py:96` | 1 | так |
| `test_first_order_filter_reaches_63pct_in_T` | `tests/unit/test_sizing.py:108` | 1 | так |
| `test_final_qty_is_min_of_three_constraints` | `tests/unit/test_sizing.py:151` | 3 | так |
| `test_sizer_reports_binding_constraint` | `tests/unit/test_sizing.py:167` | 3 | так |
| `test_qty_floored_never_rounded_up` | `tests/property/test_sizing_property.py:38` | 1 | так |
| `test_hysteresis_prevents_flip_flop` | `tests/unit/test_sizing.py:224` | 1 | так |
| `test_size_zero_in_halted_regardless_of_signal` | `tests/unit/test_sizing.py:191` | 1 | так |

## J. Ризик

Файли групи: `tests/property/test_risk_property.py`, `tests/unit/test_margin_var.py`, `tests/unit/test_risk.py`. Дослівних: 18/18; додаткових вузлів: 32.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_risk_chain_never_increases_exposure` | `tests/property/test_risk_property.py:51` | 1 | так |
| `test_veto_absorbs_everything` | `tests/property/test_risk_property.py:74` | 1 | так |
| `test_compose_is_order_independent` | `tests/property/test_risk_property.py:63` | 1 | так |
| `test_risk_fsm_transition_table_is_total` | `tests/unit/test_risk.py:117` | 24 | так |
| `test_hysteresis_blocks_recovery_inside_band` | `tests/unit/test_risk.py:153` | 1 | так |
| `test_dwell_time_blocks_premature_recovery` | `tests/unit/test_risk.py:168` | 1 | так |
| `test_halted_requires_manual_release_by_admin` | `tests/unit/test_risk.py:184` | 1 | так |
| `test_max_daily_loss_resets_at_utc_midnight` | `tests/unit/test_risk.py:335` | 1 | так |
| `test_drawdown_uses_running_peak` | `tests/unit/test_risk.py:314` | 1 | так |
| `test_stale_data_vetoes_on_lag_and_on_low_dq` | `tests/unit/test_risk.py:376` | 1 | так |
| `test_liq_price_long_10x_equals_90_4523` | `tests/unit/test_margin_var.py:51` | 1 | так |
| `test_liq_price_short_symmetry` | `tests/unit/test_margin_var.py:59` | 1 | так |
| `test_margin_ratio_is_one_at_liq_price` | `tests/unit/test_margin_var.py:81` | 3 | так |
| `test_liquidation_guard_reduces_leverage_before_veto` | `tests/unit/test_risk.py:425` | 1 | так |
| `test_every_verdict_written_to_risk_event_with_observed_and_limit` | `tests/unit/test_risk.py:479` | 1 | так |
| `test_cvar_ge_var_always` | `tests/property/test_risk_property.py:86` | 1 | так |
| `test_kupiec_lr_zero_when_breaches_equal_expected` | `tests/unit/test_margin_var.py:133` | 1 | так |
| `test_kupiec_rejects_at_20_breaches_of_500` | `tests/unit/test_margin_var.py:139` | 1 | так |

## K. Виконання і бектест

Файли групи: `tests/property/test_portfolio_property.py`, `tests/unit/test_backtest_engine.py`, `tests/unit/test_execution.py`, `tests/unit/test_walkforward_pareto.py`, `tests/unit/test_window.py`. Дослівних: 13/13; додаткових вузлів: 49.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_fill_on_next_bar_open_not_current_close` | `tests/unit/test_execution.py:67` | 1 | так |
| `test_sqrt_impact_scales_with_sqrt_of_qty` | `tests/unit/test_execution.py:99` | 1 | так |
| `test_taker_fee_higher_than_maker` | `tests/unit/test_execution.py:120` | 1 | так |
| `test_funding_charged_at_00_08_16_utc` | `tests/unit/test_execution.py:138` | 1 | так |
| `test_intrabar_pessimism_resolves_stop_before_tp` | `tests/unit/test_execution.py:200` | 1 | так |
| `test_equity_accounting_identity` | `tests/property/test_portfolio_property.py:44` | 1 | так |
| `test_lookahead_guard_raises_on_future_index` | `tests/unit/test_window.py:20` | 1 | так |
| `test_shuffling_future_bars_does_not_change_past_decisions` | `tests/unit/test_backtest_engine.py:131` | 1 | так |
| `test_backtest_deterministic_same_seed_same_equity_sha256` | `tests/unit/test_backtest_engine.py:58` | 1 | так |
| `test_different_seed_changes_equity` | `tests/unit/test_backtest_engine.py:71` | 1 | так |
| `test_zero_signal_yields_flat_equity` | `tests/unit/test_backtest_engine.py:90` | 2 | так |
| `test_walkforward_embargo_no_overlap` | `tests/unit/test_walkforward_pareto.py:46` | 4 | так |
| `test_grid_results_independent_of_worker_count` | `tests/unit/test_backtest_engine.py:330` | 1 | так |

## L. Метрики

Файли групи: `tests/unit/test_metrics.py`. Дослівних: 6/6; додаткових вузлів: 9.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_sharpe_matches_reference_series` | `tests/unit/test_metrics.py:37` | 1 | так |
| `test_sortino_penalizes_only_downside` | `tests/unit/test_metrics.py:51` | 1 | так |
| `test_max_drawdown_on_known_curve` | `tests/unit/test_metrics.py:66` | 1 | так |
| `test_ulcer_zero_on_monotone_curve` | `tests/unit/test_metrics.py:77` | 1 | так |
| `test_psr_below_threshold_on_short_sample` | `tests/unit/test_metrics.py:86` | 1 | так |
| `test_pareto_front_contains_only_nondominated` | `tests/unit/test_walkforward_pareto.py:76` | 1 | так |

## M. Інтеграційні

Файли групи: `tests/integration/test_candles.py`, `tests/integration/test_journal.py`, `tests/integration/test_migrations.py`, `tests/integration/test_trading.py`. Дослівних: 6/6; додаткових вузлів: 17.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_candle_upsert_idempotent` | `tests/integration/test_candles.py:46` | 2 | ні (інтеграційний) |
| `test_upsert_does_not_overwrite_closed_candle` | `tests/integration/test_candles.py:77` | 1 | ні (інтеграційний) |
| `test_check_constraint_rejects_invalid_candle` | `tests/integration/test_candles.py:165` | 8 | ні (інтеграційний) |
| `test_journal_append_only_revoked_update` | `tests/integration/test_journal.py:54` | 1 | ні (інтеграційний) |
| `test_order_requires_decision_fk` | `tests/integration/test_trading.py:85` | 1 | ні (інтеграційний) |
| `test_migrations_up_and_down_clean` | `tests/integration/test_migrations.py:66` | 1 | ні (інтеграційний) |

## N. API та e2e

Файли групи: `tests/e2e/test_api.py`, `tests/e2e/test_pathological_sessions.py`, `tests/e2e/test_replay_e2e.py`. Дослівних: 8/8; додаткових вузлів: 126.

| тест (дослівно з §10) | визначено (grep) | вузлів | у `make test` |
|---|---|---:|---|
| `test_login_returns_jwt_and_role` | `tests/e2e/test_api.py:195` | 1 | так |
| `test_put_risk_limits_requires_admin_role_403_for_analyst` | `tests/e2e/test_api.py:229` | 1 | так |
| `test_limit_change_written_to_audit_log_with_before_after` | `tests/e2e/test_api.py:245` | 1 | так |
| `test_get_explain_returns_fired_rules_and_memberships` | `tests/e2e/test_api.py:286` | 1 | так |
| `test_explain_narrative_is_ukrainian_and_non_empty` | `tests/e2e/test_api.py:337` | 1 | так |
| `test_strategy_post_invalid_yaml_returns_422_with_field_path` | `tests/e2e/test_api.py:356` | 1 | так |
| `test_replay_session_end_to_end` | `tests/e2e/test_replay_e2e.py:31` | 1 | так |
| `test_all_pathological_sessions_recover_with_zero_lost_events` | `tests/e2e/test_pathological_sessions.py:56` | 6 | так |

## Файли поза групами

Файли без жодного дослівного тесту §10 (додаткові перевірки модулів).

| файл | вузлів |
|---|---:|
| `tests/arch/test_brief_test_inventory.py` | 2 |
| `tests/integration/test_api_db.py` | 12 |
| `tests/integration/test_auth_audit.py` | 2 |
| `tests/integration/test_cli_db.py` | 4 |
| `tests/integration/test_storage_offline.py` | 24 |
| `tests/integration/test_worker_db.py` | 5 |
| `tests/property/test_engine_property.py` | 3 |
| `tests/unit/test_api_backtest_runner.py` | 5 |
| `tests/unit/test_auth.py` | 11 |
| `tests/unit/test_calibration_pipeline.py` | 13 |
| `tests/unit/test_cli.py` | 71 |
| `tests/unit/test_contract_edges.py` | 31 |
| `tests/unit/test_experiments_analysis.py` | 33 |
| `tests/unit/test_experiments_search.py` | 17 |
| `tests/unit/test_notify.py` | 11 |
| `tests/unit/test_portfolio.py` | 6 |
| `tests/unit/test_regimes.py` | 8 |
| `tests/unit/test_riskfix_cooldown.py` | 14 |
| `tests/unit/test_riskfix_passport.py` | 9 |
| `tests/unit/test_riskfix_var.py` | 9 |
| `tests/unit/test_scheduler.py` | 9 |
| `tests/unit/test_testnet_venue.py` | 12 |
| `tests/unit/test_trading_loop.py` | 19 |
| `tests/unit/test_wiring_anomaly.py` | 11 |
| `tests/unit/test_wiring_anomaly_edges.py` | 2 |
| `tests/unit/test_wiring_ci_deploy.py` | 3 |
| `tests/unit/test_wiring_git_state.py` | 4 |
| `tests/unit/test_wiring_ws_monotonic.py` | 4 |
| `tests/unit/test_workers.py` | 10 |
| `tests/unit/test_ws_ingest.py` | 35 |
