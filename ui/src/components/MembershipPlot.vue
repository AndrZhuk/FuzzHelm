<script setup lang="ts">
/** Функції належності однієї лінгвістичної змінної: криві термів, вертикаль поточного
    значення і заштриховані активні терми (μ > 0). Це перший графік ExplainView. */
import { computed } from 'vue'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, FONT_MONO, type EChartsOption } from '@/lib/chart'
import { num } from '@/lib/format'
import type { FuzzyVariable } from '@/lib/types'

const props = defineProps<{ variable: FuzzyVariable; height?: number }>()

/** Активні терми фарбуємо повним кольором, решту — лінійкою: око одразу бачить, що спрацювало. */
const TERM_COLORS = ['--det-trend-1', '--det-rev-1', '--det-ctx-1', '--det-rev-2', '--det-trend-2']

const option = computed<EChartsOption>(() => {
  const p = palette()
  const v = props.variable
  const terms = [...v.terms].sort((a, b) => {
    // Терми йдуть зліва направо за положенням піка — читається як шкала приладу.
    const peak = (t: typeof a): number => v.x[t.curve.indexOf(Math.max(...t.curve))] ?? 0
    return peak(a) - peak(b)
  })

  const series = terms.map((t, i) => {
    const color = getComputedStyle(document.documentElement)
      .getPropertyValue(TERM_COLORS[i % TERM_COLORS.length]).trim()
    return {
      name: t.name_uk,
      type: 'line' as const,
      data: t.curve.map((y, j) => [v.x[j], y]),
      showSymbol: false,
      smooth: false,
      lineStyle: { width: t.active ? 2 : 1, color, opacity: t.active ? 1 : 0.42 },
      // Заштрихована площа лише під активними термами (§8.2).
      areaStyle: t.active ? { color, opacity: 0.13 } : undefined,
      z: t.active ? 3 : 2,
      emphasis: { focus: 'series' as const },
    }
  })

  // Вертикаль поточного значення змінної.
  series.push({
    name: 'значення',
    type: 'line' as const,
    data: [],
    showSymbol: false,
    smooth: false,
    lineStyle: { width: 0, color: p.ink, opacity: 0 },
    z: 6,
    emphasis: { focus: 'none' as const },
    markLine: {
      silent: true,
      symbol: ['none', 'none'],
      label: {
        formatter: `${v.name} = ${num(v.value, 3)}`,
        position: 'insideEndTop',
        rotate: 0,
        color: p.ink,
        fontFamily: FONT_MONO,
        fontSize: 11,
        backgroundColor: p.paper,
        padding: [2, 4],
        borderColor: p.ruleStrong,
        borderWidth: 1,
        borderRadius: 2,
      },
      lineStyle: { color: p.ink, width: 1.5, type: 'solid' },
      data: [{ xAxis: v.value }],
    },
  } as never)

  return {
    ...baseOption(p),
    grid: { left: 40, right: 16, top: 22, bottom: 26 },
    legend: { show: false },
    tooltip: {
      ...baseOption(p).tooltip,
      valueFormatter: (x: unknown) => num(Number(x), 3),
    },
    xAxis: { ...axis(p), type: 'value', min: v.range[0], max: v.range[1], splitLine: { show: false } },
    yAxis: { ...axis(p), type: 'value', min: 0, max: 1.02, interval: 0.5, name: 'μ' },
    series,
  } as EChartsOption
})

/** Легенда під графіком: ті самі кольори, що й криві, плюс μ кожного терму. */
const legend = computed(() => {
  const v = props.variable
  const sorted = [...v.terms].sort((a, b) => {
    const peak = (t: typeof a): number => v.x[t.curve.indexOf(Math.max(...t.curve))] ?? 0
    return peak(a) - peak(b)
  })
  return sorted.map((t, i) => ({
    name: t.name_uk,
    mu: t.mu,
    active: t.active || t.mu > 0,
    color: TERM_COLORS[i % TERM_COLORS.length],
  }))
})
</script>

<template>
  <div class="mf">
    <div class="mf-head">
      <h3>{{ variable.name }} — {{ variable.name_uk }}</h3>
      <span class="mf-val num">{{ num(variable.value, 4) }}</span>
    </div>
    <ChartFrame
      :option="option"
      :height="height ?? 200"
      :caption="`Функції належності змінної «${variable.name_uk}»; вертикаль — поточне значення ${num(variable.value, 3)}.`"
    />
    <ul class="mf-terms">
      <li v-for="t in legend" :key="t.name" :class="{ 'is-active': t.active }">
        <span class="t-dot" :style="{ background: `var(${t.color})` }" aria-hidden="true" />
        <span class="t-name">{{ t.name }}</span>
        <span class="t-mu num">{{ num(t.mu, 3) }}</span>
      </li>
    </ul>
  </div>
</template>

<style scoped>
.mf-head {
  display: flex; align-items: baseline; justify-content: space-between; gap: var(--s3);
  margin-bottom: var(--s2);
}
.mf-head h3 { font-size: var(--t-small); font-weight: 600; letter-spacing: 0; }
.mf-val {
  font-size: var(--t-small); font-weight: 600; color: var(--accent);
}
.mf-terms {
  list-style: none; padding: 0; margin: var(--s2) 0 0;
  display: grid; gap: 1px;
}
.mf-terms li {
  display: flex; align-items: center; gap: var(--s2);
  font-size: var(--t-micro); color: var(--ink-faint);
}
/* Неактивні терми лишаються видимими, але тихими — як тонкі криві на графіку. */
.mf-terms li.is-active { color: var(--ink); }
.t-dot { width: 8px; height: 2px; border-radius: 1px; flex: 0 0 auto; opacity: 0.45; }
.mf-terms li.is-active .t-dot { opacity: 1; height: 3px; }
.t-name { flex: 1 1 auto; }
.mf-terms li.is-active .t-name { font-weight: 500; }
.t-mu { font-family: var(--font-mono); font-variant-numeric: tabular-nums; }
.mf-terms li.is-active .t-mu { font-weight: 600; }
</style>
