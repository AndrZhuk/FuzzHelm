<script setup lang="ts">
/** Таблиця спрацьованих правил зі стовпцем α, відсортована за спаданням (§8.2).
    Смуга під α — не декорація, а сама величина активації: рядки читаються як гістограма. */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { num } from '@/lib/format'
import type { FiredRule } from '@/lib/types'

const props = defineProps<{ rules: FiredRule[]; limit?: number }>()
const { t } = useI18n()

const shown = computed(() => (props.limit ? props.rules.slice(0, props.limit) : props.rules))
const maxAlpha = computed(() => Math.max(...props.rules.map((r) => r.alpha), 1e-9))

function tone(consequent: string): string {
  if (consequent.includes('LONG')) return 'long'
  if (consequent.includes('SHORT')) return 'short'
  return 'neutral'
}
</script>

<template>
  <div class="tbl-scroll">
    <table class="tbl rules">
      <thead>
        <tr>
          <th class="c-id">{{ t('explain.rule') }}</th>
          <th class="c-a r">{{ t('explain.alpha') }}</th>
          <th class="c-c">{{ t('explain.consequent') }}</th>
          <th class="c-t">{{ t('explain.text') }}</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="(r, i) in shown" :key="r.rule_id" :class="{ 'is-active': i === 0 }">
          <td class="c-id num">{{ r.rule_id }}</td>
          <td class="c-a r">
            <div class="alpha">
              <span class="alpha-v num">{{ num(r.alpha, 3) }}</span>
              <span class="alpha-bar" aria-hidden="true">
                <i :style="{ width: `${(r.alpha / maxAlpha) * 100}%` }" :class="`t-${tone(r.consequent)}`" />
              </span>
            </div>
          </td>
          <td class="c-c">
            <span class="badge" :class="`badge--${tone(r.consequent)}`">{{ r.consequent_uk }}</span>
          </td>
          <td class="c-t">{{ r.text_uk }}</td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.rules { table-layout: auto; }
.c-id { width: 1%; white-space: nowrap; font-weight: 500; }
.c-a  { width: 132px; }
.c-c  { width: 1%; white-space: nowrap; }
.c-t  { color: var(--ink-soft); line-height: 1.45; }

.alpha { display: flex; flex-direction: column; align-items: flex-end; gap: 3px; }
.alpha-v { font-weight: 600; font-size: var(--t-small); }
.alpha-bar {
  display: block; width: 100%; height: 3px; background: var(--rule); border-radius: 99px; overflow: hidden;
}
.alpha-bar i { display: block; height: 100%; border-radius: 99px; }
.t-long    { background: var(--long); }
.t-short   { background: var(--short); }
.t-neutral { background: var(--ink-faint); }

tbody tr:first-child .alpha-v { color: var(--accent); }

@media (max-width: 720px) {
  .c-t { display: none; }
}
</style>
