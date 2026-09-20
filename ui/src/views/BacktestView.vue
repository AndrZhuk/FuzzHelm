<script setup lang="ts">
/**
 * BacktestView — конструктор правил, запуск прогону і результати: крива капіталу
 * з просадкою і смугами режимів, таблиця метрик, паспорт відтворюваності.
 * Паспорт тут головний: він доводить, що результат можна повторити побайтово.
 */
import { computed, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useBacktest } from '@/stores/backtest'
import { useAuth } from '@/stores/auth'
import EquityCurve from '@/components/EquityCurve.vue'
import MetricsTable from '@/components/MetricsTable.vue'
import RulesEditor from '@/components/RulesEditor.vue'
import { num, shortHash, tsFull } from '@/lib/format'
import MEMBERSHIP_YAML from '../../../config/membership.yaml?raw'
import RULES_YAML from '../../../config/rules_mamdani.yaml?raw'
import type { Run } from '@/lib/types'

const store = useBacktest()
const auth = useAuth()
const { t } = useI18n()

const notice = ref<{ ok: boolean; text: string } | null>(null)
const tab = ref<'results' | 'editor'>('results')

/**
 * Редактор стартує з робочих config/*.yaml, імпортованих як текст. Копії бази правил у коді
 * немає: інакше вона розійшлася б із тією, за якою реально рахують прогони, а сервер відхиляє
 * неповну базу (потрібні всі 5x3x3 = 45 комбінацій антецедента).
 */
const form = ref({
  symbol: 'BTC-USDT-PERP',
  engine: 'mamdani',
  days: 7,
})

const passportRows = computed(() => {
  const r = store.selected
  if (r === null) return []
  return [
    { k: t('backtest.kind'), v: r.kind ?? '—', mono: true },
    { k: t('backtest.status'), v: r.status ?? '—', mono: true },
    { k: t('backtest.engine'), v: r.engine ?? '—', mono: true },
    { k: t('backtest.seed'), v: r.seed === null ? '—' : String(r.seed), mono: true },
    { k: t('backtest.from'), v: tsFull(r.ts_from_ns), mono: true },
    { k: t('backtest.to'), v: tsFull(r.ts_to_ns), mono: true },
    { k: t('backtest.started'), v: tsFull(r.started_at_ns), mono: true },
    { k: t('backtest.finished'), v: tsFull(r.finished_at_ns), mono: true },
    { k: t('backtest.githash'), v: r.git_sha, mono: true, hash: true },
    { k: t('backtest.confighash'), v: r.config_hash, mono: true, hash: true },
    { k: t('backtest.datahash'), v: r.dataset_hash, mono: true, hash: true },
    { k: t('backtest.equityhash'), v: r.equity_hash, mono: true, hash: true },
    { k: t('backtest.journalhash'), v: r.journal_head_hash, mono: true, hash: true },
  ]
})

async function pick(r: Run): Promise<void> { await store.select(r) }

async function launch(): Promise<void> {
  const to = Date.now() * 1e6
  const from = to - form.value.days * 86400 * 1e9
  const r = await store.launch({
    symbol: form.value.symbol,
    tf: '1m',
    ts_from_ns: Math.trunc(from),
    ts_to_ns: Math.trunc(to),
    engine: form.value.engine,
  })
  notice.value = { ok: r.ok, text: r.message }
}

async function saveStrategy(payload: {
  name: string; membership_yaml: string; rules_yaml: string; activate: boolean
}): Promise<void> {
  const r = await store.createStrategy(payload)
  notice.value = { ok: r.ok, text: r.message }
}

onMounted(async () => {
  await Promise.all([store.loadRuns(), store.loadStrategies()])
  // Спершу завершений бектест (там повний паспорт і 17 метрик), і лише потім будь-який інший
  // прогін: короткий реплей воркера інакше витісняв би його як найсвіжіший.
  const first = store.doneRuns.find((r) => r.kind === 'backtest') ?? store.doneRuns[0] ?? store.runs[0]
  if (first) await store.select(first)
})
</script>

<template>
  <main class="page">
    <div class="head">
      <div class="head-text">
        <h1>{{ t('backtest.title') }}</h1>
        <p class="lead">{{ t('backtest.lead') }}</p>
      </div>

      <div class="tabs no-print" role="tablist">
        <button
          class="tab" :class="{ 'is-on': tab === 'results' }" role="tab"
          :aria-selected="tab === 'results'" @click="tab = 'results'"
        >{{ t('backtest.runs') }}</button>
        <button
          class="tab" :class="{ 'is-on': tab === 'editor' }" role="tab"
          :aria-selected="tab === 'editor'" @click="tab = 'editor'"
        >{{ t('backtest.editor') }}</button>
      </div>
    </div>

    <p v-if="notice" class="notice" :class="notice.ok ? 'is-ok' : 'is-bad'" role="status">{{ notice.text }}</p>

    <!-- ------- Результати ------- -->
    <template v-if="tab === 'results'">
      <div class="cols">
        <aside class="runs" data-shot="backtest-runs">
          <div class="section-head">
            <h2>{{ t('backtest.runs') }}</h2>
            <span class="section-note num">{{ store.runs.length }}</span>
          </div>

          <form class="launch no-print" @submit.prevent="launch">
            <div class="field">
              <label for="bt-symbol">{{ t('backtest.symbol') }}</label>
              <input id="bt-symbol" v-model="form.symbol" class="input num" type="text">
            </div>
            <div class="launch-row">
              <div class="field">
                <label for="bt-engine">{{ t('backtest.engine') }}</label>
                <select id="bt-engine" v-model="form.engine" class="select">
                  <option value="mamdani">mamdani</option>
                  <option value="linear">linear</option>
                </select>
              </div>
              <div class="field">
                <label for="bt-days">днів</label>
                <input id="bt-days" v-model.number="form.days" class="input num" type="number" min="1" max="45">
              </div>
            </div>
            <button class="btn btn--primary btn--sm" type="submit" :disabled="store.pendingRun">
              {{ store.pendingRun ? t('backtest.launching') : t('backtest.launch') }}
            </button>
          </form>

          <ul v-if="store.runs.length > 0" class="run-list">
            <li v-for="r in store.runs" :key="r.id">
              <button
                class="run" :class="{ 'is-on': store.selected?.id === r.id }"
                :aria-current="store.selected?.id === r.id" @click="pick(r)"
              >
                <span class="run-id num">{{ shortHash(r.id, 8) }}</span>
                <span class="run-meta">
                  <span class="badge" :class="r.status === 'DONE' ? 'badge--normal' : 'badge--neutral'">{{ r.status }}</span>
                  <span class="micro muted">{{ r.engine }} · {{ r.kind }}</span>
                </span>
                <span class="run-time micro muted num">{{ tsFull(r.started_at_ns) }}</span>
              </button>
            </li>
          </ul>
          <p v-else class="muted small">{{ t('backtest.noruns') }}</p>
        </aside>

        <div class="results">
          <template v-if="store.selected">
            <section class="section section--first" data-shot="backtest-equity">
              <div class="section-head">
                <h2>{{ t('backtest.equity') }}</h2>
                <span class="section-note">
                  {{ store.equity ? `${store.equity.n_total} точок, крок ${store.equity.stride}` : t('common.loading') }}
                </span>
              </div>
              <EquityCurve :points="store.equity?.items ?? []" :height="360" />
            </section>

            <section class="section" data-shot="backtest-passport">
              <div class="section-head">
                <h2>{{ t('backtest.passport') }}</h2>
                <span class="section-note">той самий вхід і seed дають побайтово той самий вихід</span>
              </div>
              <dl class="facts passport">
                <div v-for="row in passportRows" :key="row.k" class="fact">
                  <dt>{{ row.k }}</dt>
                  <dd :title="row.hash ? (row.v ?? '') : undefined">
                    {{ row.hash ? shortHash(row.v, 12) : row.v }}
                  </dd>
                </div>
              </dl>
            </section>

            <section class="section" data-shot="backtest-metrics">
              <div class="section-head">
                <h2>{{ t('backtest.metrics') }}</h2>
                <span class="section-note num">
                  {{ store.metrics ? Object.keys(store.metrics.metrics).length : 0 }}
                </span>
              </div>
              <MetricsTable :metrics="store.metrics?.metrics ?? null" />
            </section>
          </template>

          <div v-else class="state">
            <h3>{{ t('backtest.norun') }}</h3>
            <p>Прогони зберігаються в базі з повним паспортом — оберіть будь-який зі списку зліва.</p>
          </div>
        </div>
      </div>
    </template>

    <!-- ------- Редактор правил ------- -->
    <template v-else>
      <section class="section section--first" data-shot="backtest-editor">
        <div class="section-head">
          <h2>{{ t('backtest.editor') }}</h2>
          <span class="section-note">
            {{ store.strategies.length > 0
               ? `стратегій у базі: ${store.strategies.length}`
               : t('backtest.nostrategies') }}
          </span>
        </div>

        <RulesEditor
          :membership="MEMBERSHIP_YAML"
          :rules="RULES_YAML"
          :can-edit="auth.can('strategy:write') || auth.role === 'operator' || auth.isAdmin"
          :busy="store.pending"
          @save="saveStrategy"
        />
      </section>

      <section v-if="store.strategies.length > 0" class="section">
        <div class="section-head"><h2>Стратегії</h2></div>
        <table class="tbl">
          <thead>
            <tr><th>{{ t('backtest.name') }}</th><th class="r">{{ t('backtest.version') }}</th><th class="r">версій</th></tr>
          </thead>
          <tbody>
            <tr v-for="s in store.strategies" :key="s.name">
              <td class="num">{{ s.name }}</td>
              <td class="r num">{{ s.active_version ?? '—' }}</td>
              <td class="r num">{{ num(s.versions, 0) }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  </main>
</template>

<style scoped>
.head { display: flex; align-items: flex-start; justify-content: space-between; gap: var(--s8); flex-wrap: wrap; margin-bottom: var(--s8); }
.head-text { max-width: 68ch; }
.head h1 { margin-bottom: var(--s2); }

.tabs { display: inline-flex; border: 1px solid var(--rule-strong); border-radius: var(--radius); overflow: hidden; }
.tab {
  padding: var(--s2) var(--s4); border: 0; background: transparent;
  font-size: var(--t-small); font-weight: 500; color: var(--ink-soft);
  transition: background 140ms var(--ease), color 140ms var(--ease);
}
.tab:hover { background: var(--paper-sunk); color: var(--ink); }
.tab.is-on { background: var(--accent); color: oklch(99% 0 0); }

.notice {
  padding: var(--s3) var(--s4); margin-bottom: var(--s6);
  border-radius: var(--radius); font-size: var(--t-small); border: 1px solid transparent;
}
.notice.is-ok  { background: var(--long-soft); color: var(--long); border-color: oklch(48% 0.125 152 / 0.3); }
.notice.is-bad { background: var(--short-soft); color: var(--short); border-color: oklch(50% 0.175 27 / 0.3); }

.cols { display: grid; grid-template-columns: minmax(250px, 310px) minmax(0, 1fr); gap: var(--s12); }
@media (max-width: 1080px) { .cols { grid-template-columns: minmax(0, 1fr); gap: var(--s8); } }
.runs, .results { min-width: 0; }
.section--first { margin-top: 0; }

.launch {
  display: flex; flex-direction: column; gap: var(--s3);
  padding: var(--s4); margin-bottom: var(--s5);
  background: var(--paper-sunk); border: 1px solid var(--rule); border-radius: var(--radius);
}
.launch-row { display: grid; grid-template-columns: 1fr 88px; gap: var(--s3); }

.run-list { list-style: none; margin: 0; padding: 0; max-height: 520px; overflow-y: auto; }
.run {
  display: flex; flex-direction: column; gap: 3px; width: 100%; text-align: left;
  padding: var(--s3); border: 0; border-bottom: 1px solid var(--rule); background: transparent;
  border-left: 2px solid transparent;
  transition: background 140ms var(--ease), border-color 140ms var(--ease);
}
.run:hover { background: var(--paper-sunk); }
.run.is-on { background: var(--accent-soft); border-left-color: var(--accent); }
.run-id { font-size: var(--t-small); font-weight: 600; }
.run-meta { display: flex; align-items: center; gap: var(--s2); }

.passport { grid-template-columns: repeat(2, minmax(0, 1fr)); column-gap: var(--s8); }
@media (max-width: 900px) { .passport { grid-template-columns: minmax(0, 1fr); } }
.passport dd { font-size: var(--t-micro); }
</style>
