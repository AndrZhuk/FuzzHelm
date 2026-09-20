<script setup lang="ts">
/** Walk-forward: 6 фолдів парними стовпчиками IS vs OOS (§8.2).
    Пара стовпчиків на фолд і є доказом: якщо OOS систематично гірший за IS — модель підігнано. */
import { computed } from 'vue'
import ChartFrame from './ChartFrame.vue'
import { palette, baseOption, axis, FONT_MONO, type EChartsOption } from '@/lib/chart'
import { num } from '@/lib/format'
import type { WalkForward } from '@/lib/experiments'

const props = defineProps<{ data: WalkForward; metric?: 'sharpe' | 'max_drawdown' | 'total_return' }>()

const METRIC_UK: Record<string, string> = {
  sharpe: 'коефіцієнт Шарпа', max_drawdown: 'максимальна просадка', total_return: 'дохідність',
}
const key = computed(() => props.metric ?? 'sharpe')

const option = computed<EChartsOption>(() => {
  const p = palette()
  const engines = props.data.engines
  const folds = engines[0]?.folds.map((f) => `фолд ${(f.fold ?? 0) + 1}`) ?? []

  // Дві серії на рушій: IS суцільним кольором, OOS — тим самим кольором зі штрихуванням.
  const series = engines.flatMap((e, i) => {
    const color = i === 0 ? p.accent : p.detectors[2]
    return [
      {
        name: `${e.engine} · IS`, type: 'bar' as const,
        data: e.folds.map((f) => f.is[key.value] ?? null),
        itemStyle: { color, opacity: 0.9 }, barMaxWidth: 16,
      },
      {
        name: `${e.engine} · OOS`, type: 'bar' as const,
        data: e.folds.map((f) => f.oos[key.value] ?? null),
        itemStyle: {
          color, opacity: 0.32,
          borderColor: color, borderWidth: 1, borderType: 'dashed' as const,
        },
        barMaxWidth: 16,
      },
    ]
  })

  return {
    ...baseOption(p),
    grid: { left: 62, right: 16, top: 20, bottom: 52 },
    legend: {
      show: true, bottom: 0, left: 'center', itemWidth: 12, itemHeight: 8, itemGap: 14,
      textStyle: { color: p.inkSoft, fontSize: 11 },
    },
    tooltip: { ...baseOption(p).tooltip, valueFormatter: (v: unknown) => num(Number(v), 3) },
    xAxis: { ...axis(p), type: 'category', data: folds, splitLine: { show: false },
             axisLabel: { color: p.inkFaint, fontSize: 11, fontFamily: FONT_MONO } },
    yAxis: { ...axis(p), type: 'value', scale: true,
             axisLabel: { color: p.inkFaint, fontSize: 11, fontFamily: FONT_MONO } },
    series,
  } as EChartsOption
})

const concat = computed(() => props.data.engines.map((e) => ({
  engine: e.engine, value: e.concat_oos[key.value],
})))
</script>

<template>
  <div>
    <ChartFrame
      :option="option"
      :height="260"
      :empty="data.engines.length === 0"
      empty-text="Результатів walk-forward немає — виконайте make walkforward"
      :caption="`${METRIC_UK[key] ?? key} по фолдах: суцільний стовпчик — навчальне вікно (IS), штрихований — позавибіркове (OOS). Embargo між вікнами не дає майбутньому протекти в минуле.`"
    />
    <dl v-if="concat.length > 0" class="facts facts--row">
      <div v-for="c in concat" :key="c.engine" class="fact">
        <dt>зчеплений OOS · {{ c.engine }}</dt>
        <dd>{{ num(c.value, 3) }}</dd>
      </div>
    </dl>
  </div>
</template>

<style scoped>
.facts--row { display: flex; flex-wrap: wrap; gap: 0 var(--s8); margin-top: var(--s3); }
.facts--row .fact { border-bottom: 0; padding: var(--s1) 0; }
</style>
