<script setup lang="ts">
/** Парето-фронт сітки (§8.2): SR_OOS ↑ проти MaxDD_OOS ↓.
    Клітинки фронту виділено, обрану — підписано: видно, що робочу точку не вибирали «на око». */
import { computed } from 'vue'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, FONT_MONO, type EChartsOption } from '@/lib/chart'
import { num, pct } from '@/lib/format'
import type { Pareto } from '@/lib/experiments'

const props = defineProps<{ data: Pareto }>()

const frontSet = computed(() => new Set(props.data.front))
const chosen = computed(() => props.data.choice?.index ?? null)

const option = computed<EChartsOption>(() => {
  const p = palette()
  const cells = props.data.cells.filter(
    (c) => Number.isFinite(c.oos_max_drawdown) && Number.isFinite(c.oos_sharpe),
  )
  const point = (c: (typeof cells)[number]): [number, number, number] =>
    [c.oos_max_drawdown ?? 0, c.oos_sharpe ?? 0, c.cell]

  const rest = cells.filter((c) => !frontSet.value.has(c.cell))
  const front = cells.filter((c) => frontSet.value.has(c.cell) && c.cell !== chosen.value)
  const pick = cells.filter((c) => c.cell === chosen.value)

  return {
    ...baseOption(p),
    grid: { left: 62, right: 18, top: 20, bottom: 50 },
    legend: {
      show: true, bottom: 0, left: 'center', itemWidth: 10, itemHeight: 10, itemGap: 16,
      textStyle: { color: p.inkSoft, fontSize: 11 },
    },
    tooltip: {
      trigger: 'item',
      backgroundColor: '#fff', borderColor: p.ruleStrong, borderWidth: 1, padding: [8, 10],
      textStyle: { color: p.ink, fontFamily: FONT_MONO, fontSize: 12 },
      extraCssText: 'box-shadow: 0 2px 4px rgba(0,0,0,.07), 0 8px 24px rgba(0,0,0,.08); border-radius: 3px;',
      formatter: (params: unknown) => {
        const v = (params as { value: number[] }).value
        const c = cells.find((x) => x.cell === v[2])
        if (!c) return ''
        return [
          `<b>клітинка ${c.cell}</b>`,
          `SR oos ${num(c.oos_sharpe, 3)}`,
          `MaxDD oos ${pct(c.oos_max_drawdown, 2)}`,
          `оборот ${num(c.oos_turnover, 1)}`,
          `n_ATR ${c.n_atr} · χ ${c.chi} · u_enter ${c.u_enter} · λ ${c.lam}`,
        ].join('<br>')
      },
    },
    xAxis: { ...axis(p), type: 'value', scale: true, name: 'MaxDD oos',
             axisLabel: { color: p.inkFaint, fontSize: 11, fontFamily: FONT_MONO,
                          formatter: (v: number) => `${(v * 100).toFixed(0)}%` } },
    yAxis: { ...axis(p), type: 'value', scale: true, name: 'SR oos',
             axisLabel: { color: p.inkFaint, fontSize: 11, fontFamily: FONT_MONO } },
    series: [
      { name: 'клітинки сітки', type: 'scatter', data: rest.map(point), symbolSize: 6,
        itemStyle: { color: p.inkFaint, opacity: 0.45 }, z: 2 },
      { name: 'фронт Парето', type: 'scatter', data: front.map(point), symbolSize: 10,
        itemStyle: { color: p.accent, opacity: 0.95 }, z: 3 },
      { name: 'робоча точка', type: 'scatter', data: pick.map(point), symbolSize: 15,
        symbol: 'diamond',
        itemStyle: { color: p.long, borderColor: '#fff', borderWidth: 1.5 }, z: 4 },
    ],
  } as EChartsOption
})

const params = computed(() => props.data.choice?.params ?? null)
</script>

<template>
  <div>
    <ChartFrame
      :option="option"
      :height="290"
      :empty="data.cells.length === 0"
      empty-text="Результатів сітки немає — виконайте make grid"
      :caption="`${data.cells.length} клітинок сітки; ${data.front.length} на фронті Парето за критеріями ${data.criteria.join(', ')}. Ромб — обрана робоча точка.`"
    />
    <dl v-if="params" class="facts facts--row">
      <div class="fact"><dt>правило вибору</dt><dd>{{ data.choice?.rule }}</dd></div>
      <div v-if="data.dd_cap !== null" class="fact"><dt>стеля просадки</dt><dd>{{ pct(data.dd_cap, 0) }}</dd></div>
      <div v-for="(v, k) in params" :key="k" class="fact"><dt>{{ k }}</dt><dd>{{ num(v, 4) }}</dd></div>
      <div v-if="data.dsr !== null" class="fact"><dt>DSR</dt><dd>{{ num(data.dsr, 4) }}</dd></div>
    </dl>
    <p v-if="data.statement" class="micro muted stmt">{{ data.statement }}</p>
  </div>
</template>

<style scoped>
.facts--row { display: flex; flex-wrap: wrap; gap: 0 var(--s6); margin-top: var(--s3); }
.facts--row .fact { border-bottom: 0; padding: var(--s1) 0; }
.stmt { margin-top: var(--s2); max-width: 78ch; line-height: 1.5; }
</style>
