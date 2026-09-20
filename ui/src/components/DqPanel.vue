<script setup lang="ts">
/** Здоров'я конвеєра: лаг даних, прогалини і погодинний скор якості Q
    з розкладкою на чотири компоненти (повнота, валідність, своєчасність, безперервність). */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, type EChartsOption } from '@/lib/chart'
import { duration, num, ts } from '@/lib/format'
import type { DqScore, Health } from '@/lib/types'

const props = defineProps<{ health: Health | null; dq: DqScore | null }>()
const { t } = useI18n()

const COMPONENTS = [
  { key: 'completeness', uk: 'повнота', color: '--det-trend-1' },
  { key: 'validity', uk: 'валідність', color: '--det-rev-1' },
  { key: 'timeliness', uk: 'своєчасність', color: '--det-rev-2' },
  { key: 'continuity', uk: 'безперервність', color: '--det-ctx-1' },
] as const

const hasDq = computed(() => (props.dq?.items.length ?? 0) > 0)

const option = computed<EChartsOption>(() => {
  const p = palette()
  const rows = [...(props.dq?.items ?? [])].sort((a, b) => a.hour_start_ns - b.hour_start_ns)
  const css = getComputedStyle(document.documentElement)

  return {
    ...baseOption(p),
    grid: { left: 40, right: 14, top: 26, bottom: 46 },
    legend: {
      show: true, bottom: 0, left: 'center', itemWidth: 12, itemHeight: 8, itemGap: 12,
      textStyle: { color: p.inkSoft, fontSize: 11 },
    },
    tooltip: { ...baseOption(p).tooltip, valueFormatter: (x: unknown) => num(Number(x), 3) },
    xAxis: {
      ...axis(p), type: 'category',
      data: rows.map((r) => ts(r.hour_start_ns, false)),
      axisLabel: { color: p.inkFaint, fontSize: 10, fontFamily: 'IBM Plex Mono', interval: Math.max(0, Math.floor(rows.length / 8) - 1) },
    },
    yAxis: { ...axis(p), type: 'value', min: 0, max: 1.02, interval: 0.25 },
    series: [
      ...COMPONENTS.map((c) => ({
        name: c.uk,
        type: 'bar' as const,
        stack: 'q',
        // Стек показує внесок кожної компоненти у зважений Q — ваги з AHP.
        data: rows.map((r) => {
          const w = props.dq?.weights[c.key] ?? 0.25
          const v = (r[c.key] ?? 0) as number
          return v * w
        }),
        itemStyle: { color: css.getPropertyValue(c.color).trim(), opacity: 0.85 },
        barMaxWidth: 18,
      })),
      {
        name: 'Q',
        type: 'line' as const,
        data: rows.map((r) => r.score ?? 0),
        showSymbol: false,
        lineStyle: { width: 2, color: p.ink },
        z: 5,
      },
    ],
  } as EChartsOption
})
</script>

<template>
  <div class="dq">
    <div v-if="health" class="inst">
      <div v-for="i in health.instruments" :key="i.instrument_id" class="inst-row">
        <span class="inst-sym">{{ i.symbol }}</span>
        <dl class="inst-facts">
          <div><dt>{{ t('live.lag') }}</dt><dd class="num" :class="{ warn: (i.data_lag_s ?? 0) > 120 }">{{ duration(i.data_lag_s) }}</dd></div>
          <div><dt>{{ t('live.gaps') }}</dt><dd class="num" :class="{ warn: i.open_gaps > 0 }">{{ i.open_gaps }}</dd></div>
          <div><dt>{{ t('live.lastbar') }}</dt><dd class="num">{{ ts(i.last_closed_open_time_ns) }}</dd></div>
          <div><dt>Q</dt><dd class="num">{{ i.latest_q?.score !== undefined && i.latest_q?.score !== null ? num(i.latest_q.score, 3) : '—' }}</dd></div>
        </dl>
      </div>
      <p class="micro muted gaps-total">
        Прогалини за статусом:
        <template v-for="(v, k) in health.gaps_by_status" :key="k">{{ k }} = {{ v }}&nbsp;&nbsp;</template>
        <span v-if="Object.keys(health.gaps_by_status).length === 0">немає</span>
      </p>
    </div>

    <ChartFrame
      :option="option"
      :height="180"
      :empty="!hasDq"
      empty-text="Погодинного скору Q ще не рахували для цього вікна"
      caption="Погодинний скор якості даних Q: стовпчики — зважені внески чотирьох компонент (ваги за AHP), лінія — підсумковий Q."
    />
  </div>
</template>

<style scoped>
.dq { display: flex; flex-direction: column; gap: var(--s4); }
.inst { display: flex; flex-direction: column; gap: var(--s2); }
.inst-row { display: flex; flex-direction: column; gap: var(--s1); padding-bottom: var(--s2); border-bottom: 1px solid var(--rule); }
.inst-row:last-of-type { border-bottom: 0; }
.inst-sym { font-family: var(--font-mono); font-size: var(--t-small); font-weight: 600; }
.inst-facts { display: flex; flex-wrap: wrap; gap: var(--s1) var(--s5); margin: 0; }
.inst-facts > div { display: flex; align-items: baseline; gap: var(--s2); }
.inst-facts dt { color: var(--ink-faint); font-size: var(--t-micro); }
.inst-facts dd { margin: 0; font-size: var(--t-small); font-variant-numeric: tabular-nums; font-family: var(--font-mono); }
.inst-facts dd.warn { color: var(--warn); font-weight: 600; }
.gaps-total { margin-top: var(--s1); }
</style>
