<script setup lang="ts">
/** Таблиця чутливості до 8 параметрів (§8.2) у вигляді «торнадо»:
    параметри відсортовані за розмахом Шарпа, смуга показує зсув від базового значення
    вліво (низьке значення параметра) і вправо (високе). Видно, що саме тримає результат. */
import { computed } from 'vue'
import { num } from '@/lib/format'
import type { Sensitivity } from '@/lib/experiments'

const props = defineProps<{ data: Sensitivity }>()

const rows = computed(() =>
  [...props.data.tornado].sort((a, b) => (b.sharpe_swing ?? 0) - (a.sharpe_swing ?? 0)),
)
/** Спільний масштаб для всіх смуг: інакше рядки не можна порівнювати між собою. */
const scale = computed(() =>
  Math.max(...rows.value.flatMap((r) => [Math.abs(r.sharpe_d_low), Math.abs(r.sharpe_d_high)]), 1e-9),
)

function bar(delta: number): { width: string; left: string; cls: string } {
  const half = (Math.abs(delta) / scale.value) * 50
  return {
    width: `${half}%`,
    left: delta >= 0 ? '50%' : `${50 - half}%`,
    cls: delta >= 0 ? 'up' : 'down',
  }
}
</script>

<template>
  <div>
    <div class="tbl-scroll">
      <table class="tbl">
        <thead>
          <tr>
            <th>параметр</th>
            <th class="r">база</th>
            <th class="r">низьке</th>
            <th class="r">високе</th>
            <th class="c-bar">зсув Шарпа від бази</th>
            <th class="r">розмах</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.param">
            <td>
              <b class="sym">{{ r.symbol }}</b>
              <span class="muted"> — {{ r.label_uk }}</span>
            </td>
            <td class="r num">{{ num(r.base_value, 4) }}</td>
            <td class="r num">{{ num(r.low_value, 4) }}</td>
            <td class="r num">{{ num(r.high_value, 4) }}</td>
            <td class="c-bar">
              <span class="track" aria-hidden="true">
                <i class="zero" />
                <i class="seg" :class="bar(r.sharpe_d_low).cls"
                   :style="{ width: bar(r.sharpe_d_low).width, left: bar(r.sharpe_d_low).left }" />
                <i class="seg" :class="bar(r.sharpe_d_high).cls"
                   :style="{ width: bar(r.sharpe_d_high).width, left: bar(r.sharpe_d_high).left }" />
              </span>
              <span class="sr-only">
                низьке {{ num(r.sharpe_d_low, 2) }}, високе {{ num(r.sharpe_d_high, 2) }}
              </span>
            </td>
            <td class="r num">{{ num(r.sharpe_swing, 2) }}</td>
          </tr>
        </tbody>
      </table>
    </div>
    <p class="micro muted note">
      Базовий Шарп {{ num(rows[0]?.sharpe_base, 3) }}; кожен параметр змінювали по одному
      ({{ data.levels.length }} рівні), решта — з робочої точки. Рядки відсортовано за спаданням розмаху.
    </p>
  </div>
</template>

<style scoped>
.sym { font-family: var(--font-mono); font-weight: 600; }
.c-bar { width: 230px; min-width: 180px; }
.track { position: relative; display: block; height: 10px; }
.zero { position: absolute; left: 50%; top: -2px; bottom: -2px; width: 1px; background: var(--rule-strong); }
.seg { position: absolute; top: 2px; height: 6px; border-radius: 1px; }
.seg.up { background: var(--long); opacity: 0.8; }
.seg.down { background: var(--short); opacity: 0.8; }
.note { margin-top: var(--s2); max-width: 78ch; }
</style>
