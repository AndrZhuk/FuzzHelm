<script setup lang="ts">
/** Свічки з маркерами входів/виходів і лінією ліквідації (§8.2).
    Синтетичні свічки (заповнені прогалини) підсвічуємо — оператор мусить бачити, де дані добудовані. */
import { computed } from 'vue'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, FONT_MONO, type EChartsOption } from '@/lib/chart'
import { num, ts } from '@/lib/format'
import type { Candle } from '@/lib/types'

export interface TradeMarker {
  ts_ns: number; price: number; side: 1 | -1; kind: 'entry' | 'exit'; label?: string
}

const props = defineProps<{
  candles: Candle[]
  markers?: TradeMarker[]
  liquidation?: number | null
  height?: number
}>()

const option = computed<EChartsOption>(() => {
  const p = palette()
  const rows = props.candles.filter((c) => c.o !== null && c.c !== null)
  const times = rows.map((c) => ts(c.open_time_ns, false))
  // ECharts чекає [open, close, low, high].
  const ohlc = rows.map((c) => [Number(c.o), Number(c.c), Number(c.l), Number(c.h)])

  const synthetic = rows
    .map((c, i) => (c.is_synthetic ? i : -1))
    .filter((i) => i >= 0)
    .map((i) => [{ xAxis: i }, { xAxis: i }])

  const markPoints = (props.markers ?? []).map((m) => {
    const idx = rows.findIndex((c) => c.open_time_ns >= m.ts_ns)
    return {
      name: m.label ?? (m.kind === 'entry' ? 'вхід' : 'вихід'),
      coord: [idx < 0 ? rows.length - 1 : idx, m.price],
      symbol: m.kind === 'entry' ? (m.side > 0 ? 'triangle' : 'diamond') : 'circle',
      symbolSize: m.kind === 'entry' ? 11 : 8,
      symbolRotate: m.kind === 'entry' && m.side < 0 ? 180 : 0,
      itemStyle: { color: m.side > 0 ? p.long : p.short, borderColor: p.paper, borderWidth: 1 },
      label: { show: false },
    }
  })

  const liqLine = props.liquidation
    ? [{
        yAxis: props.liquidation,
        lineStyle: { color: p.halt, width: 1.5, type: 'dashed' as const },
        label: {
          formatter: `ліквідація ${num(props.liquidation, 1)}`, position: 'insideEndTop' as const,
          color: p.halt, fontFamily: FONT_MONO, fontSize: 11,
          backgroundColor: p.paper, padding: [2, 4], borderColor: p.ruleStrong, borderWidth: 1, borderRadius: 2,
        },
      }]
    : []

  return {
    ...baseOption(p),
    grid: { left: 62, right: 16, top: 20, bottom: 58 },
    tooltip: {
      ...baseOption(p).tooltip,
      formatter: (params: unknown) => {
        const arr = params as { seriesType: string; dataIndex: number; value: number[] }[]
        const bar = arr.find((a) => a.seriesType === 'candlestick')
        if (!bar) return ''
        const c = rows[bar.dataIndex]
        const [, o, cl, lo, hi] = bar.value as unknown as number[]
        return [
          `<b>${ts(c.open_time_ns)}</b>`,
          `O ${num(o, 1)}&nbsp; H ${num(hi, 1)}`,
          `L ${num(lo, 1)}&nbsp; C ${num(cl, 1)}`,
          `V ${num(c.volume, 3)}`,
          c.is_synthetic ? '<i>синтетична свічка</i>' : '',
        ].filter(Boolean).join('<br>')
      },
    },
    xAxis: {
      ...axis(p), type: 'category', data: times, boundaryGap: true,
      axisLabel: {
        // 'auto' рахує підписи для видимого вікна dataZoom, а не для всього ряду.
        color: p.inkFaint, fontSize: 10, fontFamily: FONT_MONO,
        interval: 'auto', hideOverlap: true,
      },
      splitLine: { show: false },
    },
    yAxis: { ...axis(p), type: 'value', scale: true, axisLabel: { color: p.inkFaint, fontSize: 11, fontFamily: FONT_MONO } },
    dataZoom: [
      { type: 'inside', start: Math.max(0, 100 - (2400 / Math.max(times.length, 1))), end: 100 },
      {
        type: 'slider', height: 16, bottom: 4,
        borderColor: p.rule, fillerColor: p.accentSoft, handleStyle: { color: p.accent },
        dataBackground: { lineStyle: { color: p.rule }, areaStyle: { color: p.paperSunk } },
        textStyle: { color: p.inkFaint, fontSize: 10, fontFamily: FONT_MONO },
      },
    ],
    series: [{
      name: 'OHLC',
      type: 'candlestick',
      data: ohlc,
      itemStyle: {
        color: p.paper, color0: p.short,            // порожнє тіло — зростання
        borderColor: p.long, borderColor0: p.short, borderWidth: 1,
      },
      markPoint: markPoints.length > 0 ? { data: markPoints, silent: true } : undefined,
      markLine: liqLine.length > 0 ? { silent: true, symbol: ['none', 'none'], data: liqLine } : undefined,
      markArea: synthetic.length > 0
        ? { silent: true, itemStyle: { color: p.warn, opacity: 0.08 }, data: synthetic }
        : undefined,
    }],
  } as EChartsOption
})
</script>

<template>
  <ChartFrame
    :option="option"
    :height="height ?? 320"
    :empty="candles.length === 0"
    empty-text="Свічок у базі немає — виконайте backfill"
    :caption="candles.length > 0
      ? `${candles.length} свічок; порожнє тіло — зростання, залите — падіння. Жовтим позначено синтетичні бари.`
      : undefined"
  />
</template>
