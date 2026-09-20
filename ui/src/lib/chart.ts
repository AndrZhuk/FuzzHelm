/**
 * Спільна основа ECharts: реєструємо лише потрібні модулі (збірка не тягне весь echarts)
 * і задаємо тему «лабораторний журнал» — ті самі чорнило, лінійки і моноширинні цифри.
 */
import * as echarts from 'echarts/core'
import { CandlestickChart, LineChart, BarChart, ScatterChart, CustomChart } from 'echarts/charts'
import {
  GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, MarkPointComponent,
  MarkAreaComponent, DataZoomComponent, TitleComponent, AxisPointerComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'
import type { EChartsOption } from 'echarts'
import { cssVar } from './format'

echarts.use([
  CandlestickChart, LineChart, BarChart, ScatterChart, CustomChart,
  GridComponent, TooltipComponent, LegendComponent, MarkLineComponent, MarkPointComponent,
  MarkAreaComponent, DataZoomComponent, TitleComponent, AxisPointerComponent,
  CanvasRenderer,
])

export { echarts }
export type { EChartsOption }

export interface Palette {
  ink: string; inkSoft: string; inkFaint: string; rule: string; ruleStrong: string
  paper: string; paperSunk: string
  accent: string; accentLine: string; accentSoft: string
  long: string; short: string; warn: string; halt: string; cool: string
  detectors: string[]
}

/** Читаємо кольори з CSS-змінних: тема лишається однією правдою в tokens.css. */
export function palette(): Palette {
  return {
    ink: cssVar('--ink'), inkSoft: cssVar('--ink-soft'), inkFaint: cssVar('--ink-faint'),
    rule: cssVar('--rule'), ruleStrong: cssVar('--rule-strong'),
    paper: cssVar('--paper'), paperSunk: cssVar('--paper-sunk'),
    accent: cssVar('--accent'), accentLine: cssVar('--accent-line'), accentSoft: cssVar('--accent-soft'),
    long: cssVar('--long'), short: cssVar('--short'), warn: cssVar('--warn'),
    halt: cssVar('--halt'), cool: cssVar('--cool'),
    detectors: [
      cssVar('--det-trend-1'), cssVar('--det-trend-2'),
      cssVar('--det-rev-1'), cssVar('--det-rev-2'), cssVar('--det-rev-3'),
      cssVar('--det-ctx-1'),
    ],
  }
}

const MONO = "'IBM Plex Mono', ui-monospace, monospace"
const SANS = "'IBM Plex Sans', system-ui, sans-serif"

/** Базові налаштування, спільні для всіх графіків панелі. */
export function baseOption(p: Palette): EChartsOption {
  return {
    animationDuration: 320,
    animationEasing: 'cubicOut',
    textStyle: { fontFamily: SANS, color: p.ink, fontSize: 12 },
    grid: { left: 52, right: 18, top: 24, bottom: 34, containLabel: false },
    tooltip: {
      trigger: 'axis',
      backgroundColor: cssVar('--paper-raised') || '#fff',
      borderColor: p.ruleStrong,
      borderWidth: 1,
      padding: [8, 10],
      textStyle: { color: p.ink, fontFamily: MONO, fontSize: 12 },
      extraCssText: 'box-shadow: 0 2px 4px rgba(0,0,0,.07), 0 8px 24px rgba(0,0,0,.08); border-radius: 3px;',
      axisPointer: { lineStyle: { color: p.inkFaint, width: 1, type: 'dashed' } },
    },
  }
}

/** Вісь у стилі приладу: волосяна лінія, моноширинні підписи, без зубців. */
export function axis(p: Palette, opts: { name?: string; mono?: boolean } = {}): Record<string, unknown> {
  return {
    name: opts.name,
    nameTextStyle: { color: p.inkFaint, fontSize: 11, fontFamily: SANS },
    axisLine: { lineStyle: { color: p.ruleStrong, width: 1 } },
    axisTick: { show: false },
    axisLabel: {
      color: p.inkFaint, fontSize: 11,
      fontFamily: opts.mono === false ? SANS : MONO,
    },
    splitLine: { lineStyle: { color: p.rule, width: 1 } },
  }
}

export const FONT_MONO = MONO
export const FONT_SANS = SANS
