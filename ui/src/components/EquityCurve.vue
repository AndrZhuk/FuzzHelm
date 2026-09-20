<script setup lang="ts">
/**
 * Крива капіталу з просадкою під нею і смугою режиму ризику (§8.2).
 *
 * Смугу режимів малюємо звичайним HTML під графіком, а не засобами ECharts: відрізки
 * виходять суцільного кольору (у друкованому звіті напівпрозорі заливки зливаються з тлом),
 * ширина рахується від кількості точок, а ліві/праві відступи збігаються з полем графіка.
 */
import { computed } from 'vue'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, FONT_MONO, type EChartsOption } from '@/lib/chart'
import { money, num, pct, ts } from '@/lib/format'
import type { EquityPoint } from '@/lib/types'

// withDefaults обов'язковий: Vue приводить оголошений Boolean-проп, який не передали,
// до false (а не до undefined), тож без явного true смуга режимів ніколи б не малювалась.
const props = withDefaults(
  defineProps<{ points: EquityPoint[]; height?: number; showRegimes?: boolean }>(),
  { height: 330, showRegimes: true },
)

/** Ті самі відступи, що й grid у графіку, — стрічка стоїть рівно під полем даних. */
const PAD_LEFT = 66
const PAD_RIGHT = 18

const REGIME_VAR: Record<string, string> = {
  NORMAL: '--rule', WARNING: '--warn', COOLDOWN: '--cool', HALTED: '--halt',
}
const REGIME_UK: Record<string, string> = {
  NORMAL: 'штатний', WARNING: 'застереження', COOLDOWN: 'витримка', HALTED: 'аварійний стоп',
}
const ORDER = ['NORMAL', 'WARNING', 'COOLDOWN', 'HALTED'] as const

const showRibbon = computed(() => props.showRegimes && props.points.length > 0)

/** Суцільні відрізки одного режиму з їхньою часткою ширини. */
const segments = computed(() => {
  const pts = props.points
  const out: { state: string; from: number; to: number; width: number; label: string }[] = []
  if (pts.length === 0) return out
  let start = 0
  let state = pts[0].risk_state ?? 'NORMAL'
  const push = (end: number): void => {
    out.push({
      state,
      from: start,
      to: end,
      width: ((end - start + 1) / pts.length) * 100,
      label: `${REGIME_UK[state] ?? state}: ${ts(pts[start].ts_ns)} — ${ts(pts[end].ts_ns)}`,
    })
  }
  for (let i = 1; i < pts.length; i += 1) {
    const st = pts[i].risk_state ?? 'NORMAL'
    if (st !== state) { push(i - 1); start = i; state = st }
  }
  push(pts.length - 1)
  return out
})

/** Режими, що реально трапились, — для легенди під стрічкою. */
const regimesPresent = computed(() => {
  const seen = new Set(segments.value.map((s) => s.state))
  return ORDER.filter((s) => seen.has(s))
})

const option = computed<EChartsOption>(() => {
  const p = palette()
  const pts = props.points
  const times = pts.map((x) => ts(x.ts_ns, false))

  // Вузький екран тримає менше підписів дати, інакше вони наїжджають один на одного.
  const slots = window.innerWidth < 720 ? 3 : 8
  const tickEvery = Math.max(0, Math.ceil(times.length / slots) - 1)

  // Просадка буває часткою відсотка: тоді «0 %» на всіх поділках нічого не каже.
  const maxDd = Math.max(...pts.map((x) => x.drawdown ?? 0), 0)
  const ddDigits = maxDd >= 0.1 ? 0 : maxDd >= 0.01 ? 1 : maxDd > 0 ? 2 : 0

  return {
    ...baseOption(p),
    grid: [
      { left: PAD_LEFT, right: PAD_RIGHT, top: 16, height: '60%' },
      { left: PAD_LEFT, right: PAD_RIGHT, top: '76%', height: '16%' },
    ],
    axisPointer: { link: [{ xAxisIndex: 'all' }] },
    tooltip: {
      ...baseOption(p).tooltip,
      formatter: (params: unknown) => {
        const arr = params as { dataIndex: number }[]
        const i = arr[0]?.dataIndex ?? 0
        const pt = pts[i]
        if (!pt) return ''
        return [
          `<b>${ts(pt.ts_ns)}</b>`,
          `капітал ${money(pt.equity)}`,
          `просадка ${pct(pt.drawdown, 2)}`,
          `плече ${num(pt.leverage, 2)}`,
          `режим ${REGIME_UK[pt.risk_state ?? 'NORMAL'] ?? pt.risk_state}`,
          pt.kappa !== null ? `κ ${num(pt.kappa, 2)}` : '',
        ].filter(Boolean).join('<br>')
      },
    },
    xAxis: [
      { ...axis(p), type: 'category', data: times, gridIndex: 0, boundaryGap: false,
        axisLabel: { show: false }, splitLine: { show: false } },
      { ...axis(p), type: 'category', data: times, gridIndex: 1, boundaryGap: false,
        axisLabel: {
          color: p.inkFaint, fontSize: 10, fontFamily: FONT_MONO,
          interval: tickEvery, hideOverlap: true, showMaxLabel: false,
        },
        splitLine: { show: false } },
    ],
    yAxis: [
      // Назви осей не малюємо: над сіткою для них немає місця, а підпис під рисунком їх називає.
      { ...axis(p), type: 'value', scale: true, gridIndex: 0,
        axisLabel: { color: p.inkFaint, fontSize: 11, fontFamily: FONT_MONO } },
      { ...axis(p), type: 'value', gridIndex: 1, inverse: true, splitNumber: 2,
        axisLabel: {
          color: p.inkFaint, fontSize: 10, fontFamily: FONT_MONO, showMinLabel: false,
          formatter: (v: number) => `${(v * 100).toFixed(ddDigits)}%`,
        } },
    ],
    series: [
      { name: 'капітал', type: 'line', xAxisIndex: 0, yAxisIndex: 0,
        data: pts.map((x) => Number(x.equity ?? 0)),
        showSymbol: false, lineStyle: { width: 1.6, color: p.accent },
        areaStyle: { color: p.accent, opacity: 0.08 }, z: 3 },
      { name: 'просадка', type: 'line', xAxisIndex: 1, yAxisIndex: 1,
        data: pts.map((x) => x.drawdown ?? 0),
        showSymbol: false, lineStyle: { width: 1.2, color: p.short },
        areaStyle: { color: p.short, opacity: 0.14 }, z: 3 },
    ],
  } as EChartsOption
})
</script>

<template>
  <div class="eq">
    <ChartFrame
      :option="option"
      :height="height"
      :empty="points.length === 0"
      empty-text="Крива капіталу для цього прогону порожня"
      :caption="points.length > 0
        ? `Капітал (угорі) і просадка (внизу) за ${points.length} точками; смуга під графіком — режим ризик-автомата.`
        : undefined"
    />

    <div v-if="showRibbon" class="ribbon-wrap">
      <div
        class="ribbon"
        :style="{ marginLeft: `${PAD_LEFT}px`, marginRight: `${PAD_RIGHT}px` }"
        role="img"
        aria-label="Смуга режимів ризик-автомата за час прогону"
      >
        <span
          v-for="(sg, i) in segments" :key="i"
          class="seg"
          :style="{ width: `${sg.width}%`, background: `var(${REGIME_VAR[sg.state]})` }"
          :title="sg.label"
        />
      </div>
      <ul class="legend" :style="{ marginLeft: `${PAD_LEFT}px` }">
        <li v-for="s in regimesPresent" :key="s">
          <span class="sw" :style="{ background: `var(${REGIME_VAR[s]})` }" aria-hidden="true" />
          {{ REGIME_UK[s] }}
        </li>
      </ul>
    </div>
  </div>
</template>

<style scoped>
.ribbon-wrap { margin-top: var(--s2); }
.ribbon {
  display: flex; height: 10px; overflow: hidden;
  border: 1px solid var(--rule-strong); border-radius: 2px; background: var(--paper-sunk);
}
.seg { display: block; height: 100%; min-width: 1px; }
.legend {
  list-style: none; margin: var(--s2) 0 0; padding: 0;
  display: flex; flex-wrap: wrap; gap: var(--s1) var(--s4);
  font-size: var(--t-micro); color: var(--ink-faint);
}
.legend li { display: inline-flex; align-items: center; gap: var(--s2); }
.sw { width: 14px; height: 8px; border-radius: 1px; border: 1px solid oklch(0% 0 0 / 0.12); }
@media (max-width: 720px) {
  .ribbon { margin-left: 44px !important; margin-right: 10px !important; }
  .legend { margin-left: 0 !important; }
}
</style>
