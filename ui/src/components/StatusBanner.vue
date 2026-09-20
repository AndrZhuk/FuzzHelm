<script setup lang="ts">
/** Шапка стану (§8.2): режим, джерело даних, відсутність ключів mainnet, seed і поточний Q.
    Це перше, що бачить комісія: система свідомо не має доступу до реальних грошей. */
import { computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { num } from '@/lib/format'
import type { Health, RiskStateName } from '@/lib/types'

const props = defineProps<{
  health: Health | null
  riskState?: RiskStateName | null
  seed?: number | null
  streamState?: 'connecting' | 'open' | 'closed'
}>()
const { t } = useI18n()

/** Q беремо як найгірший серед інструментів: шапка має показувати найслабшу ланку. */
const q = computed(() => {
  const scores = (props.health?.instruments ?? [])
    .map((i) => i.latest_q?.score)
    .filter((s): s is number => typeof s === 'number')
  return scores.length > 0 ? Math.min(...scores) : null
})

const qTone = computed(() => {
  if (q.value === null) return 'neutral'
  if (q.value >= 0.9) return 'ok'
  if (q.value >= 0.7) return 'warn'
  return 'bad'
})
</script>

<template>
  <div class="banner" role="status">
    <span class="cell">
      <i>{{ t('banner.mode') }}</i><b>{{ t('banner.paper') }}</b>
    </span>
    <span class="sep" aria-hidden="true">·</span>
    <span class="cell">
      <i>{{ t('banner.feed') }}</i><b>{{ t('banner.replay') }}</b>
    </span>
    <span class="sep" aria-hidden="true">·</span>
    <span class="cell cell--guard">
      <b>{{ t('banner.nomainnet') }}</b>
    </span>
    <span class="sep" aria-hidden="true">·</span>
    <span class="cell">
      <i>{{ t('banner.seed') }}</i><b class="num">{{ seed ?? 20260918 }}</b>
    </span>
    <span class="sep" aria-hidden="true">·</span>
    <span class="cell">
      <i>{{ t('banner.q') }}</i>
      <b class="num" :class="`q-${qTone}`">{{ q === null ? '—' : num(q, 3) }}</b>
    </span>

    <span v-if="streamState" class="stream" :class="`s-${streamState}`">
      <span class="dot" aria-hidden="true" />
      {{ streamState === 'open' ? t('live.connected')
         : streamState === 'connecting' ? t('live.connecting') : t('live.disconnected') }}
    </span>
  </div>
</template>

<style scoped>
.banner {
  display: flex; align-items: center; gap: var(--s2); flex-wrap: wrap;
  padding: var(--s2) var(--s4);
  background: var(--ink); color: oklch(96% 0.004 260);
  font-family: var(--font-mono); font-size: var(--t-micro); letter-spacing: 0.04em;
}
.cell { display: inline-flex; align-items: baseline; gap: var(--s2); }
.cell i { font-style: normal; opacity: 0.58; }
.cell b { font-weight: 600; }
/* Гарантія «без mainnet» — єдине місце в шапці, яке має право на колір. */
.cell--guard b {
  color: oklch(88% 0.16 145);
  border: 1px solid oklch(88% 0.16 145 / 0.4);
  padding: 1px var(--s2); border-radius: 2px;
}
.sep { opacity: 0.32; }

.q-ok   { color: oklch(88% 0.16 145); }
.q-warn { color: oklch(86% 0.15 85); }
.q-bad  { color: oklch(76% 0.17 25); }

.stream { margin-left: auto; display: inline-flex; align-items: center; gap: var(--s2); opacity: 0.8; }
.dot { width: 6px; height: 6px; border-radius: 99px; background: currentColor; }
.s-open      { color: oklch(88% 0.16 145); }
.s-connecting{ color: oklch(86% 0.15 85); }
.s-closed    { color: oklch(76% 0.17 25); }
.s-open .dot { animation: pulse 2.4s var(--ease) infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

@media (max-width: 720px) {
  .stream { margin-left: 0; width: 100%; }
}
</style>
