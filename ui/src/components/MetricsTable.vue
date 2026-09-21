<script setup lang="ts">
/** Таблиця метрик прогону з україномовними назвами і правильною формою числа:
    частки — у відсотках, коефіцієнти — з трьома знаками, лічильники — цілими. */
import { computed } from 'vue'
import { num, pct } from '@/lib/format'
import { METRIC_ORDER } from '@/stores/backtest'

const props = defineProps<{ metrics: Record<string, number> | null }>()

type Kind = 'pct' | 'ratio' | 'count' | 'money' | 'raw'

const META: Record<string, { uk: string; kind: Kind; hint?: string }> = {
  total_return:   { uk: 'Сукупна дохідність', kind: 'pct' },
  cagr:           { uk: 'CAGR', kind: 'pct', hint: 'річна дохідність за складним відсотком' },
  sharpe:         { uk: 'Коефіцієнт Шарпа', kind: 'ratio' },
  sortino:        { uk: 'Коефіцієнт Сортіно', kind: 'ratio' },
  calmar:         { uk: 'Коефіцієнт Калмара', kind: 'ratio' },
  max_drawdown:   { uk: 'Максимальна просадка', kind: 'pct' },
  ulcer_index:    { uk: 'Ulcer index', kind: 'ratio' },
  ann_vol:        { uk: 'Річна волатильність', kind: 'pct' },
  win_rate:       { uk: 'Частка виграшних', kind: 'pct' },
  profit_factor:  { uk: 'Профіт-фактор', kind: 'ratio' },
  expectancy:     { uk: 'Матсподівання угоди', kind: 'money' },
  avg_win:        { uk: 'Середній виграш', kind: 'money' },
  avg_loss:       { uk: 'Середній програш', kind: 'money' },
  n_trades:       { uk: 'Угод', kind: 'count' },
  n_fills:        { uk: 'Виконань', kind: 'count' },
  turnover:       { uk: 'Оборот', kind: 'ratio' },
  exposure:       { uk: 'Експозиція', kind: 'pct' },
  traded_notional:{ uk: 'Торгований номінал', kind: 'money' },
  tail_ratio:     { uk: 'Хвостове відношення', kind: 'ratio' },
  halted:         { uk: 'Досягнуто HALTED', kind: 'raw' },
  wall_engine_s:  { uk: 'Час рушія', kind: 'ratio', hint: 'секунди' },
  wall_load_db_s: { uk: 'Час завантаження БД', kind: 'ratio', hint: 'секунди' },
  wall_persist_s: { uk: 'Час запису', kind: 'ratio', hint: 'секунди' },
  git_dirty:      { uk: 'Дерево брудне', kind: 'raw' },
}

function show(key: string, v: number): string {
  const kind = META[key]?.kind ?? 'ratio'
  if (kind === 'pct') return pct(v, 2)
  if (kind === 'count') return num(v, 0)
  if (kind === 'money') return num(v, 2)
  if (kind === 'raw') return v === 1 ? 'так' : v === 0 ? 'ні' : num(v, 3)
  return num(v, 3)
}

/** Підсвічуємо знак лише там, де він щось означає (дохідність, Шарп, просадка). */
const SIGNED = new Set(['total_return', 'cagr', 'sharpe', 'sortino', 'calmar', 'expectancy', 'sr_period'])

const allRows = computed(() => {
  const m = props.metrics
  if (m === null) return []
  const ordered = METRIC_ORDER.filter((k) => k in m)
  const rest = Object.keys(m).filter((k) => !(METRIC_ORDER as readonly string[]).includes(k)).sort()
  return [...ordered, ...rest].map((k) => ({
    key: k,
    uk: META[k]?.uk ?? k,
    hint: META[k]?.hint,
    value: show(k, m[k]),
    signed: SIGNED.has(k) ? Math.sign(m[k]) : 0,
  }))
})

/** Дві половини — щоб довгий список метрик читався у дві колонки без CSS-columns. */
const halves = computed(() => {
  const all = allRows.value
  const cut = Math.ceil(all.length / 2)
  return [all.slice(0, cut), all.slice(cut)]
})
</script>

<template>
  <div v-if="allRows.length > 0" class="metrics">
    <table v-for="(half, hi) in halves" :key="hi" class="tbl">
      <thead>
        <tr><th>Метрика</th><th class="r">Значення</th><th class="c-key">ключ</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in half" :key="r.key">
          <td>
            {{ r.uk }}
            <span v-if="r.hint" class="micro muted"> — {{ r.hint }}</span>
          </td>
          <td class="r num" :class="{ pos: r.signed > 0, neg: r.signed < 0 }">{{ r.value }}</td>
          <td class="c-key num micro muted">{{ r.key }}</td>
        </tr>
      </tbody>
    </table>
  </div>
  <p v-else class="muted small">Метрик для цього прогону немає.</p>
</template>

<style scoped>
.metrics { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--s5) var(--s8); align-items: start; }
@media (max-width: 900px) { .metrics { grid-template-columns: minmax(0, 1fr); } }
td.pos { color: var(--long); font-weight: 600; }
td.neg { color: var(--short); font-weight: 600; }
.c-key { width: 1%; white-space: nowrap; }
@media (max-width: 720px) { .c-key { display: none; } }
</style>
