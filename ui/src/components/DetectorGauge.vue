<script setup lang="ts">
/** Один із шести детекторів: сила s ∈ [−1;1] і довіра c ∈ [0;1] різними кольорами (§8.2).
    s малюємо як відхилення від нуля вліво/вправо, c — як заповнення тієї ж шкали знизу:
    видно і напрям, і те, наскільки даним можна вірити. */
import { computed } from 'vue'
import { num, signed } from '@/lib/format'
import type { DetectorOutput } from '@/lib/types'

const props = defineProps<{ detector: DetectorOutput; colorVar: string }>()

const GROUP_UK: Record<string, string> = {
  trend: 'тренд', reversion: 'реверсія', context: 'контекст',
}
const NAME_UK: Record<string, string> = {
  ema_slope: 'нахил EMA',
  donchian: 'канал Дончіяна',
  rsi_exhaustion: 'виснаження RSI',
  bollinger_z: 'z Боллінджера',
  candle_geometry: 'геометрія свічки',
  vol_regime: 'режим волатильності',
}

const s = computed(() => Math.max(-1, Math.min(1, props.detector.s)))
const c = computed(() => Math.max(0, Math.min(1, props.detector.c)))
/** Half-ширина смуги s у відсотках від центру шкали. */
const sWidth = computed(() => Math.abs(s.value) * 50)
const sLeft = computed(() => (s.value < 0 ? 50 - sWidth.value : 50))
</script>

<template>
  <div class="det">
    <div class="det-top">
      <span class="det-name">{{ NAME_UK[detector.name] ?? detector.name }}</span>
      <span class="det-group micro muted">{{ GROUP_UK[detector.group] ?? detector.group }}</span>
    </div>

    <div class="scale" role="img"
         :aria-label="`${NAME_UK[detector.name] ?? detector.name}: сила ${signed(s, 2)}, довіра ${num(c, 2)}`">
      <span class="axis-zero" aria-hidden="true" />
      <span class="s-bar" :style="{ left: `${sLeft}%`, width: `${sWidth}%`, background: `var(${colorVar})` }" />
      <span class="c-bar" :style="{ width: `${c * 100}%`, background: `var(${colorVar})` }" />
    </div>

    <div class="det-vals">
      <span class="v"><i>s</i><b class="num" :class="{ pos: s > 0, neg: s < 0 }">{{ signed(s, 2) }}</b></span>
      <span class="v"><i>c</i><b class="num">{{ num(c, 2) }}</b></span>
    </div>
  </div>
</template>

<style scoped>
.det { display: flex; flex-direction: column; gap: var(--s1); padding: var(--s2) 0; }
.det-top { display: flex; align-items: baseline; justify-content: space-between; gap: var(--s2); }
.det-name { font-size: var(--t-small); font-weight: 500; }
.det-group { letter-spacing: 0.04em; }

.scale { position: relative; height: 16px; }
.axis-zero {
  position: absolute; left: 50%; top: 0; bottom: 6px; width: 1px;
  background: var(--rule-strong);
}
/* Верхня смуга — сила зі знаком, росте від центру. */
.s-bar { position: absolute; top: 1px; height: 7px; border-radius: 1px; opacity: 0.92; }
/* Нижня смуга — довіра, росте зліва: шкала одна, читання різне. */
.c-bar {
  position: absolute; left: 0; bottom: 0; height: 3px; border-radius: 99px; opacity: 0.4;
}

.det-vals { display: flex; gap: var(--s4); font-size: var(--t-micro); }
.v { display: inline-flex; align-items: baseline; gap: var(--s1); }
.v i { color: var(--ink-faint); font-style: italic; font-family: var(--font-mono); }
.v b { font-weight: 600; font-variant-numeric: tabular-nums; }
.v b.pos { color: var(--long); }
.v b.neg { color: var(--short); }
</style>
