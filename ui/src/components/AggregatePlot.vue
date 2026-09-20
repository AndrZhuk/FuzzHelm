<script setup lang="ts">
/** Агрегована вихідна фігура μ_agg(u) на сітці 201 вузол із позначеним центроїдом.
    Центроїд — це і є дефазифіковане u_raw: підписуємо його прямо на осі. */
import { computed } from 'vue'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, FONT_MONO, type EChartsOption } from '@/lib/chart'
import { num, signed } from '@/lib/format'
import type { Aggregate } from '@/lib/types'

const props = defineProps<{ aggregate: Aggregate; uFinal?: number; height?: number }>()

const option = computed<EChartsOption>(() => {
  const p = palette()
  const a = props.aggregate
  const tone = a.centroid > 0 ? p.long : a.centroid < 0 ? p.short : p.inkSoft

  const marks: { xAxis: number; lineStyle: Record<string, unknown>; label: Record<string, unknown> }[] = [
    {
      xAxis: a.centroid,
      lineStyle: { color: tone, width: 2 },
      label: {
        formatter: `центроїд ${signed(a.centroid, 3)}`, position: 'insideEndTop', rotate: 0,
        color: tone, fontFamily: FONT_MONO, fontSize: 11, fontWeight: 600,
        backgroundColor: p.paper, padding: [2, 4],
        borderColor: p.ruleStrong, borderWidth: 1, borderRadius: 2,
      },
    },
  ]
  if (props.uFinal !== undefined && Math.abs(props.uFinal - a.centroid) > 1e-9) {
    marks.push({
      xAxis: props.uFinal,
      lineStyle: { color: p.accent, width: 1.5, type: 'dashed' },
      label: {
        formatter: `u_final ${signed(props.uFinal, 3)}`, position: 'insideEndBottom', rotate: 0,
        color: p.accent, fontFamily: FONT_MONO, fontSize: 11,
        backgroundColor: p.paper, padding: [2, 4],
        borderColor: p.ruleStrong, borderWidth: 1, borderRadius: 2,
      },
    })
  }

  return {
    ...baseOption(p),
    grid: { left: 40, right: 16, top: 34, bottom: 28 },
    tooltip: { ...baseOption(p).tooltip, valueFormatter: (x: unknown) => num(Number(x), 3) },
    xAxis: { ...axis(p), type: 'value', min: -1, max: 1, splitLine: { show: false }, name: 'u' },
    yAxis: { ...axis(p), type: 'value', min: 0, max: 1.02, interval: 0.5, name: 'μ_agg' },
    series: [{
      name: 'μ_agg(u)',
      type: 'line',
      data: a.grid.map((x, i) => [x, a.mu[i]]),
      showSymbol: false,
      // Сходинки трапецієподібної агрегації — це max по зрізаних термах, тож без згладжування.
      lineStyle: { width: 2, color: tone },
      areaStyle: { color: tone, opacity: 0.15 },
      markLine: { silent: true, symbol: ['none', 'none'], data: marks },
      z: 3,
    }],
  } as EChartsOption
})
</script>

<template>
  <ChartFrame
    :option="option"
    :height="height ?? 220"
    :caption="`Агрегована фігура μ_agg(u) на сітці ${aggregate.nodes} вузлів (${aggregate.scheme}); центроїд ${num(aggregate.centroid, 4)}, площа ${num(aggregate.area, 4)}.`"
  />
</template>
