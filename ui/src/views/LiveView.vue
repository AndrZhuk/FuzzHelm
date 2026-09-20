<script setup lang="ts">
/**
 * LiveView — те, що бачить черговий оператор: ринок, шість детекторів, вихід нечіткого ядра,
 * режим ризику з журналом відхилених ордерів і здоров'я конвеєра. Події надходять по SSE.
 */
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useMarket } from '@/stores/market'
import { useRisk } from '@/stores/risk'
import { useAuth } from '@/stores/auth'
import CandleChart, { type TradeMarker } from '@/components/CandleChart.vue'
import DetectorGauge from '@/components/DetectorGauge.vue'
import RiskStatePanel from '@/components/RiskStatePanel.vue'
import RejectionLog from '@/components/RejectionLog.vue'
import DqPanel from '@/components/DqPanel.vue'
import { api, openStream, type StreamHandle } from '@/lib/api'
import { num, signed, ts } from '@/lib/format'
import type { DetectorOutput, Explain } from '@/lib/types'

const market = useMarket()
const risk = useRisk()
const auth = useAuth()
const { t } = useI18n()

const DET_COLORS = ['--det-trend-1', '--det-trend-2', '--det-rev-1', '--det-rev-2', '--det-rev-3', '--det-ctx-1']

const streamState = ref<'connecting' | 'open' | 'closed'>('connecting')
const streamError = ref<string | null>(null)
const events = ref<{ kind: string; at: number; summary: string }[]>([])
let handle: StreamHandle | null = null

/** Останнє рішення з потоку: воно живить панель детекторів і вихід FIS. */
const lastDecision = ref<{
  T: number; R: number; V: number; u_raw: number; kappa: number; u_final: number
  detectors: DetectorOutput[]; ts_ns: number; id: number | null
} | null>(null)

const notice = ref<string | null>(null)

/**
 * Маркери входів/виходів і лінія ліквідації (§8.2).
 * Виконання приходять подією `fill` з потоку — окремого маршруту «ордери прогону» в API немає,
 * тож на екрані видно ті угоди, що сталися при відкритій сторінці. Ціну ліквідації беремо з
 * того самого `/explain`, що живить розкладку детекторів.
 */
const fills = ref<TradeMarker[]>([])
const liquidation = ref<number | null>(null)

function summarise(kind: string, data: unknown): string {
  const d = data as Record<string, unknown>
  if (kind === 'decision') {
    const rule = d.top_rule ? ` · ${String(d.top_rule)} α=${num(d.alpha as number, 2)}` : ''
    return `№${d.id ?? '—'}: u_final ${signed(Number(d.u_final ?? 0), 3)}${rule}`
  }
  if (kind === 'risk') return `${String(d.state_from ?? '')} → ${String(d.state_to ?? '')} (${String(d.event ?? '')})`
  if (kind === 'rejection') {
    const items = (d.items as { rule: string }[]) ?? []
    return `відхилено: ${items.map((i) => i.rule).join(', ') || '—'}`
  }
  if (kind === 'fill') return `виконання ${Number(d.side) > 0 ? 'купівля' : 'продаж'} ${num(d.qty as string, 4)} @ ${num(d.price as string, 1)}`
  if (kind === 'candle') return `бар ${ts(Number(d.open_time_ns))} C ${num(d.c as string, 1)}`
  if (kind === 'equity') return `капітал ${num(d.equity as string, 2)}`
  if (kind === 'run') return `прогін ${String(d.status ?? '')}`
  return kind
}

/**
 * NOTIFY обмежений 7900 байтами, тож подія `decision` несе лише підсумок і `id`
 * (api/live.py: «publish a reference instead»). Деталі — T/R/V і шість детекторів —
 * добираємо одним запитом /decisions/{id}/explain, не частіше ніж раз на 1.5 с.
 */
let lastFetchAt = 0
let inflight = false

async function hydrate(id: number): Promise<void> {
  const now = Date.now()
  if (inflight || now - lastFetchAt < 1500) return
  inflight = true; lastFetchAt = now
  try {
    const ex = await api<Explain>(`/decisions/${id}/explain?mf_points=11`)
    lastDecision.value = {
      T: ex.inputs.T ?? 0, R: ex.inputs.R ?? 0, V: ex.inputs.V ?? 0,
      u_raw: ex.u_raw, kappa: ex.kappa, u_final: ex.u_final,
      detectors: ex.detector_outputs ?? [],
      ts_ns: ex.open_time_ns, id: ex.decision_id,
    }
    const liq = Number(ex.prices?.liq)
    liquidation.value = Number.isFinite(liq) && liq > 0 ? liq : null
  } catch { /* рішення ще не закомічене — наступна подія принесе свіже */ }
  finally { inflight = false }
}

function onEvent(kind: string, data: unknown): void {
  events.value.unshift({ kind, at: Date.now(), summary: summarise(kind, data) })
  if (events.value.length > 40) events.value.length = 40

  const d = data as Record<string, unknown>
  if (kind === 'decision') {
    const id = Number(d.id)
    // Показуємо підсумок одразу, деталі підвантажуємо слідом.
    lastDecision.value = {
      T: lastDecision.value?.T ?? 0, R: lastDecision.value?.R ?? 0, V: lastDecision.value?.V ?? 0,
      u_raw: Number(d.u_raw ?? 0), kappa: Number(d.kappa ?? 1), u_final: Number(d.u_final ?? 0),
      detectors: lastDecision.value?.detectors ?? [],
      ts_ns: Number(d.open_time_ns ?? 0), id: Number.isFinite(id) ? id : null,
    }
    if (Number.isFinite(id)) void hydrate(id)
  }
  if (kind === 'fill') {
    const side = Number(d.side) >= 0 ? 1 : -1
    const price = Number(d.price)
    const qty = Math.abs(Number(d.qty))
    if (Number.isFinite(price) && price > 0) {
      fills.value.push({
        ts_ns: Number(d.ts_ns), price, side,
        // Вхід збільшує позицію, вихід — зменшує; напрям угоди вже в `side`.
        kind: 'entry',
        label: `${side > 0 ? 'купівля' : 'продаж'} ${num(qty, 4)} @ ${num(price, 1)}`,
      })
      if (fills.value.length > 60) fills.value.splice(0, fills.value.length - 60)
    }
  }
  if (kind === 'candle') void market.loadCandles()
  if (kind === 'risk' || kind === 'rejection') void Promise.all([risk.loadState(), risk.loadEvents()])
}

async function release(reason: string): Promise<void> {
  const r = await risk.release(reason)
  notice.value = r.message
  window.setTimeout(() => { notice.value = null }, 8000)
}

const uTone = computed(() => {
  const u = lastDecision.value?.u_final ?? 0
  return u > 0.05 ? 'long' : u < -0.05 ? 'short' : 'neutral'
})

onMounted(async () => {
  await Promise.all([market.refresh(), risk.refresh()])
  handle = openStream(onEvent, {
    onOpen: () => { streamState.value = 'open'; streamError.value = null },
    onError: (m) => { streamState.value = 'closed'; streamError.value = m },
  })
})
onBeforeUnmount(() => handle?.close())
</script>

<template>
  <main class="page">
    <div class="head">
      <div class="head-text">
        <h1>{{ t('live.title') }}</h1>
        <p class="lead">{{ t('live.lead') }}</p>
      </div>
      <p class="stream-chip" :class="`s-${streamState}`">
        <span class="dot" aria-hidden="true" />
        {{ streamState === 'open' ? t('live.connected')
           : streamState === 'connecting' ? t('live.connecting') : t('live.disconnected') }}
      </p>
    </div>

    <p v-if="notice" class="notice" role="status">{{ notice }}</p>

    <div class="cols">
      <div class="col-main">
        <!-- Ринок -->
        <section class="section" data-shot="live-market">
          <div class="section-head">
            <h2>{{ t('live.candles') }}</h2>
            <span class="section-note num">
              {{ market.symbol }} · {{ market.tf }}
              <template v-if="fills.length > 0"> · угод у потоці: {{ fills.length }}</template>
            </span>
          </div>
          <CandleChart
            :candles="market.candles"
            :markers="fills"
            :liquidation="liquidation"
            :height="340"
          />
          <p v-if="market.error" class="small err">{{ market.error }}</p>
        </section>

        <!-- Детектори -->
        <section class="section" data-shot="live-detectors">
          <div class="section-head">
            <h2>{{ t('live.detectors') }}</h2>
            <span class="section-note">
              {{ lastDecision ? `бар ${ts(lastDecision.ts_ns)}` : 'очікуємо перше рішення з потоку' }}
            </span>
          </div>

          <div v-if="lastDecision && lastDecision.detectors.length > 0" class="dets">
            <DetectorGauge
              v-for="(d, i) in lastDecision.detectors" :key="d.name"
              :detector="d" :color-var="DET_COLORS[i % DET_COLORS.length]"
            />
          </div>
          <p v-else class="muted small">
            {{ lastDecision ? 'Добираємо розкладку детекторів…' : t('live.nodata') }}
          </p>
        </section>

        <!-- Консенсус і вихід FIS -->
        <section class="section" data-shot="live-fis">
          <div class="section-head">
            <h2>{{ t('live.fis') }}</h2>
            <span class="section-note">{{ t('live.consensus') }} T / R / V → u_raw × κ → u_final</span>
          </div>

          <div v-if="lastDecision" class="fis">
            <div class="trv">
              <!-- T і R знакові (шкала [-1;1], нуль посередині), V — лише додатна [0;1]. -->
              <div v-for="k in (['T', 'R', 'V'] as const)" :key="k" class="trv-col">
                <span class="trv-label">{{ k }}</span>
                <span class="trv-bar" :class="{ 'is-unipolar': k === 'V' }" aria-hidden="true">
                  <i
                    :style="k === 'V'
                      ? { height: `${Math.min(1, Math.max(0, lastDecision.V)) * 100}%`, bottom: '0%', background: 'var(--det-ctx-1)' }
                      : {
                          height: `${Math.abs(lastDecision[k]) * 50}%`,
                          bottom: lastDecision[k] >= 0 ? '50%' : `${50 - Math.abs(lastDecision[k]) * 50}%`,
                          background: lastDecision[k] >= 0 ? 'var(--long)' : 'var(--short)',
                        }"
                  />
                </span>
                <span class="trv-val num">{{ k === 'V' ? num(lastDecision.V, 3) : signed(lastDecision[k], 3) }}</span>
              </div>
            </div>

            <div class="fis-out">
              <div class="fo"><i>u_raw</i><b class="num">{{ signed(lastDecision.u_raw, 4) }}</b></div>
              <div class="fo"><i>κ</i><b class="num">{{ num(lastDecision.kappa, 4) }}</b></div>
              <div class="fo fo--main" :class="`is-${uTone}`">
                <i>u_final</i><b class="num">{{ signed(lastDecision.u_final, 4) }}</b>
              </div>
              <RouterLink
                v-if="lastDecision.id"
                class="btn btn--sm" :to="`/explain/${lastDecision.id}`"
              >Повне виведення →</RouterLink>
            </div>
          </div>
          <p v-else class="muted small">{{ t('live.nodata') }}</p>
        </section>

        <!-- Здоров'я конвеєра -->
        <section class="section" data-shot="live-health">
          <div class="section-head">
            <h2>{{ t('live.health') }}</h2>
            <span class="section-note">{{ t('live.gaps') }}: {{ market.health?.open_gaps_total ?? '—' }}</span>
          </div>
          <DqPanel :health="market.health" :dq="market.dq" />
        </section>
      </div>

      <aside class="col-side">
        <!-- Ризик -->
        <section class="section section--first" data-shot="live-risk">
          <div class="section-head">
            <h2>{{ t('live.risk') }}</h2>
          </div>
          <RiskStatePanel
            :state="risk.state"
            :can-release="auth.isAdmin"
            :releasing="risk.releasing"
            @release="release"
          />
        </section>

        <!-- Відхилені ордери -->
        <section class="section" data-shot="live-rejections">
          <div class="section-head">
            <h2>{{ t('live.rejections') }}</h2>
            <span class="section-note num">{{ risk.rejections.length }}</span>
          </div>
          <RejectionLog :events="risk.rejections" :limit="12" />
        </section>

        <!-- Потік -->
        <section class="section" data-shot="live-stream">
          <div class="section-head">
            <h2>{{ t('live.stream') }}</h2>
            <span class="section-note" :class="streamState === 'open' ? 'ok' : 'warn'">
              {{ streamState === 'open' ? t('live.connected')
                 : streamState === 'connecting' ? t('live.connecting') : t('live.disconnected') }}
            </span>
          </div>
          <p v-if="streamError" class="micro err">{{ streamError }}</p>
          <ol v-if="events.length > 0" class="feed">
            <li v-for="(ev, i) in events.slice(0, 14)" :key="`${ev.at}-${i}`">
              <span class="feed-kind">{{ ev.kind }}</span>
              <span class="feed-text">{{ ev.summary }}</span>
            </li>
          </ol>
          <p v-else class="muted small">{{ t('live.noevents') }}</p>
        </section>
      </aside>
    </div>
  </main>
</template>

<style scoped>
.head { display: flex; flex-direction: column; gap: var(--s4); margin-bottom: var(--s8); }
.head-text { max-width: 68ch; }
.head h1 { margin-bottom: var(--s2); }
.stream-chip {
  display: inline-flex; align-items: center; gap: var(--s2); align-self: flex-start;
  padding: var(--s1) var(--s3); border: 1px solid var(--rule-strong); border-radius: var(--radius);
  background: var(--paper-sunk); font-size: var(--t-micro); font-family: var(--font-mono);
}
.stream-chip .dot { width: 6px; height: 6px; border-radius: 99px; background: currentColor; }
.stream-chip.s-open       { color: var(--long);  border-color: oklch(48% 0.125 152 / 0.3); background: var(--long-soft); }
.stream-chip.s-connecting { color: var(--warn);  border-color: oklch(58% 0.135 72 / 0.3);  background: var(--warn-soft); }
.stream-chip.s-closed     { color: var(--short); border-color: oklch(50% 0.175 27 / 0.3);  background: var(--short-soft); }
.stream-chip.s-open .dot  { animation: pulse 2.4s var(--ease) infinite; }
@keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

.notice {
  padding: var(--s3) var(--s4); margin-bottom: var(--s6);
  background: var(--long-soft); color: var(--long);
  border: 1px solid oklch(48% 0.125 152 / 0.3); border-radius: var(--radius); font-size: var(--t-small);
}

.cols { display: grid; grid-template-columns: minmax(0, 1fr) minmax(280px, 340px); gap: var(--s12); }
@media (max-width: 1080px) { .cols { grid-template-columns: minmax(0, 1fr); gap: var(--s8); } }
.col-main, .col-side { min-width: 0; }
.section--first { margin-top: 0; }
.col-side .section { margin-top: var(--s8); }
.col-side .section--first { margin-top: 0; }

.dets { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: var(--s2) var(--s8); }
@media (max-width: 900px) { .dets { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 560px) { .dets { grid-template-columns: minmax(0, 1fr); } }

.fis { display: flex; gap: var(--s10); align-items: flex-end; flex-wrap: wrap; }
.trv { display: flex; gap: var(--s5); }
.trv-col { display: flex; flex-direction: column; align-items: center; gap: var(--s2); }
.trv-label { font-family: var(--font-mono); font-size: var(--t-small); font-weight: 600; color: var(--ink-soft); }
/* Стовпчик росте від середини вгору або вниз — знак читається без підпису. */
.trv-bar {
  position: relative; width: 26px; height: 92px;
  background: var(--paper-sunk); border: 1px solid var(--rule); border-radius: 2px;
}
.trv-bar::after {
  content: ''; position: absolute; left: 0; right: 0; top: 50%; height: 1px; background: var(--rule-strong);
}
/* У V нуль — це дно шкали, тож середньої лінії там немає. */
.trv-bar.is-unipolar::after { top: auto; bottom: 0; }
.trv-bar i { position: absolute; left: 3px; right: 3px; border-radius: 1px; }
.trv-val { font-size: var(--t-micro); font-variant-numeric: tabular-nums; }

.fis-out { display: flex; align-items: flex-end; gap: var(--s5); flex-wrap: wrap; }
.fo { display: flex; flex-direction: column; gap: 1px; }
.fo i { font-style: normal; font-family: var(--font-mono); font-size: var(--t-micro); color: var(--ink-faint); }
.fo b { font-size: var(--t-h3); font-weight: 600; }
.fo--main b { font-size: var(--t-num); line-height: 1.05; letter-spacing: -0.02em; }
.fo--main.is-long b { color: var(--long); }
.fo--main.is-short b { color: var(--short); }

.feed { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; }
.feed li {
  display: flex; gap: var(--s3); align-items: baseline;
  padding: var(--s1) 0; border-bottom: 1px solid var(--rule); font-size: var(--t-micro);
}
.feed li:last-child { border-bottom: 0; }
.feed-kind {
  flex: 0 0 62px; font-family: var(--font-mono); color: var(--accent); font-weight: 500;
}
.feed-text { color: var(--ink-soft); }

.err { color: var(--short); }
.ok { color: var(--long); }
.warn { color: var(--warn); }
</style>
