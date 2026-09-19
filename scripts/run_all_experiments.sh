#!/usr/bin/env bash
# Найменування: run_all_experiments.sh
# Призначення: фаза 7 — повний прогін обчислювальних експериментів на зафіксованому 45-денному вікні
#              у порядку, перевіреному рецензентами (docs/api/exp_search.md, docs/api/exp_analysis.md).
# Автор: Андрій Жук, 2026.
#
# Кожен крок логується з часом виконання; збій кроку не зупиняє решту (статус у підсумку).
# Передумова: код закомічено (скрипти з --persist відмовляються писати в БД з брудного дерева).
set -u
cd "$(dirname "$0")/.."
LOG_DIR=artifacts/exp_logs
mkdir -p "$LOG_DIR" docs/report_tables/raw
SUMMARY="$LOG_DIR/summary.tsv"
: > "$SUMMARY"
export FUZZHELM_DATABASE_URL="${FUZZHELM_DATABASE_URL:-postgresql+asyncpg://fuzzhelm:fuzzhelm@localhost:5442/fuzzhelm}"
MAX_LOAD="${MAX_LOAD:-4.0}"   # фонові застосунки робочої станції дають load ≈ 2.5–3 (фіксується у виході бенчмарку)

step() {
  local name="$1"; shift
  local t0; t0=$(date +%s)
  echo "=== [$(date -u +%H:%M:%S)] $name: $*" | tee -a "$LOG_DIR/all.log"
  if "$@" > "$LOG_DIR/$name.log" 2>&1; then st=OK; else st="FAIL($?)"; fi
  local dt=$(( $(date +%s) - t0 ))
  printf '%s\t%s\t%ss\t%s\n' "$name" "$st" "$dt" "$(uptime | sed 's/.*load averages*: //')" | tee -a "$SUMMARY"
}

PY="uv run python"
echo "git $(git rev-parse HEAD) dirty=$(git status --porcelain -- . ':(exclude)artifacts' ':(exclude)docs' | wc -l)" | tee "$LOG_DIR/all.log"

# 1. Амдал — першим, поки машину не навантажено власними прогонами
step amdahl_btc        $PY scripts/bench_amdahl.py --db --symbol BTCUSDT --cells 16 --repeats 3 --max-load "$MAX_LOAD"
step plot_amdahl       $PY scripts/plot_amdahl.py --symbol BTCUSDT

# 2–4. Пошук: сітка, walk-forward, чутливість (BTC), потім ETH
for S in BTCUSDT ETHUSDT; do
  step "grid_$S"        $PY scripts/run_grid.py --db --symbol "$S" --engine mamdani --workers 8
  step "pareto_$S"      $PY scripts/plot_pareto.py --symbol "$S" --engine mamdani
  step "wf_$S"          $PY scripts/run_walkforward.py --db --symbol "$S" --engine both --workers 8
  step "plot_wf_m_$S"   $PY scripts/plot_walkforward.py --symbol "$S" --engine mamdani
  step "plot_wf_l_$S"   $PY scripts/plot_walkforward.py --symbol "$S" --engine linear
done
step sens_oos_btc      $PY scripts/sensitivity.py --db --symbol BTCUSDT --target oos --workers 8
step plot_sens_oos     $PY scripts/plot_sensitivity.py --symbol BTCUSDT --target oos
step sens_full_btc     $PY scripts/sensitivity.py --db --symbol BTCUSDT --target full --workers 8
step plot_sens_full    $PY scripts/plot_sensitivity.py --symbol BTCUSDT --target full

# 5. Прогін фази 6 за профілем backtest на поточному коміті (повний паспорт, криві, рішення)
step run_backtest_btc  $PY scripts/run_backtest.py

# 6. Аналіз з робочою точкою сітки (помітка про упередженість вибору — у виходах, XA-17)
for S in BTCUSDT ETHUSDT; do
  G="@artifacts/exp_search/grid/grid_${S}_mamdani.json"
  step "cost_$S"        $PY scripts/cost_models.py --db --symbol "$S" --workers 8 --params "$G" --out "docs/report_tables/raw/cost_models_$S"
  step "ablation_$S"    $PY scripts/ablation.py --db --symbol "$S" --workers 8 --params "$G" --out "docs/report_tables/raw/ablation_$S"
  step "hyst_$S"        $PY scripts/hysteresis_cost.py --db --symbol "$S" --workers 8 --params "$G" --out "docs/report_tables/raw/hysteresis_$S"
  step "hyst_rel_$S"    $PY scripts/hysteresis_cost.py --db --symbol "$S" --workers 8 --params "$G" --risk-loop relaxed --out "docs/report_tables/raw/hysteresis_${S}_relaxed"
  step "var_$S"         $PY scripts/var_backtest.py --db --symbol "$S" --workers 8 --params "$G" --out "docs/report_tables/raw/var_$S"
  step "plot_var_$S"    $PY scripts/plot_var.py --results "docs/report_tables/raw/var_$S" --out docs/figures
  step "equity_$S"      $PY scripts/plot_equity.py --db --symbol "$S" --params "$G" --persist --out docs/figures
  step "equity_oos_$S"  $PY scripts/plot_equity.py --series "docs/report_tables/raw/var_$S/var_series.npz" --scope oos --out docs/figures
done

# 7. Чисті OOS-оцінки (вибір на IS кожного фолду за правилом Парето-фронту) — найдовші кроки
for S in BTCUSDT ETHUSDT; do
  step "cost_isgrid_$S"     $PY scripts/cost_models.py --db --symbol "$S" --workers 8 --selection is_grid --scope oos --out "docs/report_tables/raw/cost_models_${S}_isgrid"
  step "ablation_isgrid_$S" $PY scripts/ablation.py --db --symbol "$S" --workers 8 --selection is_grid --scope oos --out "docs/report_tables/raw/ablation_${S}_isgrid"
done

echo "=== done $(date -u +%H:%M:%S)" | tee -a "$LOG_DIR/all.log"
