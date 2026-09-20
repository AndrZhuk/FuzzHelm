<script setup lang="ts">
/**
 * ExplainView — кульмінація захисту (§8.2): повне формальне виведення одного рішення.
 * Сторінка читається згори вниз як розв'язок у підручнику: входи → правила → агрегація →
 * дефазифікація → κ → сайзер → ризик → висновок. Кожен крок пронумеровано, бо комісія
 * питає саме «звідки взялося це число».
 */
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { useI18n } from 'vue-i18n'
import { useDecision } from '@/stores/decision'
import MembershipPlot from '@/components/MembershipPlot.vue'
import AggregatePlot from '@/components/AggregatePlot.vue'
import RuleTable from '@/components/RuleTable.vue'
import DetectorGauge from '@/components/DetectorGauge.vue'
import { money, num, pct, shortHash, signed, tsFull } from '@/lib/format'

const store = useDecision()
const route = useRoute()
const router = useRouter()
const { t } = useI18n()

const idInput = ref<number>(Number(route.params.id) || 8)

const DET_COLORS = [
  '--det-trend-1', '--det-trend-2', '--det-rev-1', '--det-rev-2', '--det-rev-3', '--det-ctx-1',
]

const e = computed(() => store.explain)
/** Порядок змінних у виведенні фіксований: тренд, реверсія, волатильність. */
const inputVars = computed(() => ['T', 'R', 'V'].map((k) => e.value?.variables[k]).filter((v) => v !== undefined))

const uTone = computed(() => {
  const u = e.value?.u_final ?? 0
  return u > 0.05 ? 'long' : u < -0.05 ? 'short' : 'neutral'
})

const sideUk = computed(() => {
  const s = e.value?.target.side ?? 0
  return s > 0 ? 'ЛОНГ' : s < 0 ? 'ШОРТ' : 'БЕЗ ПОЗИЦІЇ'
})

const BINDING_UK: Record<string, string> = {
  VOL_TARGET: 'таргет волатильності',
  ATR_RISK: 'ризик на угоду (ATR)',
  LEVERAGE: 'обмеження плеча',
  MIN_NOTIONAL: 'мінімальний номінал',
  STEP_SIZE: 'крок лота',
}

async function load(): Promise<void> {
  const id = Number(idInput.value)
  if (!Number.isFinite(id) || id < 1) return
  await router.replace({ name: 'explain', params: { id: String(id) } })
  await store.load(id)
}

onMounted(load)
watch(() => route.params.id, (v) => {
  const id = Number(v)
  if (Number.isFinite(id) && id >= 1 && id !== store.decisionId) { idInput.value = id; void store.load(id) }
})
</script>

<template>
  <main class="page">
    <!-- Заголовок і вибір рішення -->
    <div class="head">
      <div class="head-text">
        <h1>{{ t('explain.title') }}</h1>
        <p class="lead">{{ t('explain.lead') }}</p>
      </div>

      <form class="picker no-print" @submit.prevent="load">
        <div class="field">
          <label for="decision-id">{{ t('explain.pick') }}</label>
          <input
            id="decision-id" v-model.number="idInput" class="input num"
            type="number" min="1" step="1" inputmode="numeric"
          >
        </div>
        <button class="btn btn--primary" type="submit" :disabled="store.pending">
          {{ store.pending ? t('common.loading') : t('explain.open') }}
        </button>
      </form>
    </div>

    <!-- Стани -->
    <div v-if="store.pending && !e" class="loading">
      <span class="skel" style="height: 22px; width: 42%" />
      <span class="skel" style="height: 200px" />
      <span class="skel" style="height: 140px; width: 70%" />
    </div>

    <div v-else-if="store.notFound" class="state">
      <h3>{{ t('explain.notfound') }}</h3>
      <p>Номери рішень ідуть підряд від 1. Спробуйте менший номер або відкрийте прогін у розділі «Бектест».</p>
    </div>

    <div v-else-if="store.error" class="state state--error" role="alert">
      <h3>{{ t('common.error') }}</h3>
      <p>{{ store.error }}</p>
      <button class="btn btn--sm" @click="load">{{ t('common.retry') }}</button>
    </div>

    <template v-else-if="e">
      <!-- Паспорт рішення -->
      <div class="passport" data-shot="explain-passport">
        <span class="pp"><i>{{ t('explain.run') }}</i><b class="num" :title="e.run_id">{{ shortHash(e.run_id, 8) }}</b></span>
        <span class="pp"><i>{{ t('explain.engine') }}</i><b class="num">{{ e.engine }}</b></span>
        <span class="pp"><i>{{ t('explain.bar') }}</i><b class="num">{{ tsFull(e.open_time_ns) }}</b></span>
        <span class="pp"><i>№</i><b class="num">{{ e.decision_id }}</b></span>
        <span class="pp pp--check" :class="e.consistency.ok ? 'is-ok' : 'is-bad'">
          <b>{{ e.consistency.ok ? '✓' : '✗' }} {{ t('explain.consistency') }}</b>
          <i class="micro">{{ e.consistency.ok ? t('explain.consistency_ok') : t('explain.consistency_bad') }}</i>
        </span>
      </div>

      <!-- 1. Входи -->
      <section class="section" data-shot="explain-inputs">
        <div class="section-head">
          <span class="section-step">Крок 1</span>
          <h2>{{ t('explain.s1') }}</h2>
          <span class="section-note">{{ t('explain.s1note') }}</span>
        </div>

        <div class="grid-3 mfs">
          <MembershipPlot v-for="v in inputVars" :key="v!.name" :variable="v!" :height="190" />
        </div>

        <details class="details">
          <summary>{{ t('explain.detectors') }} — шість джерел, з яких зібрано T, R і V</summary>
          <div class="dets">
            <DetectorGauge
              v-for="(d, i) in e.detector_outputs" :key="d.name"
              :detector="d" :color-var="DET_COLORS[i % DET_COLORS.length]"
            />
          </div>
          <p class="micro muted">
            Консенсус зважує силу s кожного детектора його довірою c; V береться окремо з детектора
            режиму волатильності ({{ e.v_source }}).
          </p>
        </details>
      </section>

      <!-- 2. Правила -->
      <section class="section" data-shot="explain-rules">
        <div class="section-head">
          <span class="section-step">Крок 2</span>
          <h2>{{ t('explain.s2') }}</h2>
          <span class="section-note">
            {{ e.n_fired }} {{ t('explain.nfired') }} · {{ t('explain.s2note') }}
          </span>
        </div>
        <RuleTable :rules="e.fired_rules" />
      </section>

      <!-- 3. Агрегація і дефазифікація -->
      <section class="section" data-shot="explain-aggregate">
        <div class="section-head">
          <span class="section-step">Крок 3</span>
          <h2>{{ t('explain.s3') }}</h2>
          <span class="section-note">{{ t('explain.s3note', { nodes: e.aggregate.nodes }) }}</span>
        </div>

        <div class="agg">
          <AggregatePlot :aggregate="e.aggregate" :u-final="e.u_final" :height="240" />

          <div class="agg-side">
            <h3 class="sub">{{ t('explain.strengths') }}</h3>
            <table class="tbl">
              <tbody>
                <tr v-for="s in e.aggregate.strengths" :key="s.term">
                  <td>{{ s.term_uk }}</td>
                  <td class="r num">{{ num(s.beta, 3) }}</td>
                </tr>
              </tbody>
            </table>
            <dl class="facts">
              <div class="fact"><dt>{{ t('explain.area') }}</dt><dd>{{ num(e.aggregate.area, 4) }}</dd></div>
              <div class="fact"><dt>{{ t('explain.height') }}</dt><dd>{{ num(e.aggregate.height, 4) }}</dd></div>
              <div class="fact"><dt>схема</dt><dd>{{ e.aggregate.scheme }}</dd></div>
            </dl>
          </div>
        </div>
      </section>

      <!-- 4. κ і підсумкове u -->
      <section class="section" data-shot="explain-kappa">
        <div class="section-head">
          <span class="section-step">Крок 4</span>
          <h2>{{ t('explain.s4') }}</h2>
          <span class="section-note">Узгодженість детекторів стискає сигнал, а не змінює його знак.</span>
        </div>

        <div class="chain">
          <div class="chain-term">
            <span class="ct-label">{{ t('explain.uraw') }}</span>
            <span class="ct-value num">{{ signed(e.u_raw, 4) }}</span>
            <span class="ct-note micro muted">центроїд μ_agg</span>
          </div>
          <span class="chain-op" aria-hidden="true">×</span>
          <div class="chain-term">
            <span class="ct-label">{{ t('explain.kappa') }}</span>
            <span class="ct-value num">{{ num(e.kappa, 4) }}</span>
            <span class="ct-note micro muted">A_g = {{ num(e.agreement.A_g, 3) }}</span>
          </div>
          <span class="chain-op" aria-hidden="true">=</span>
          <div class="chain-term chain-term--result" :class="`is-${uTone}`">
            <span class="ct-label">{{ t('explain.ufinal') }}</span>
            <span class="ct-value ct-value--big num">{{ signed(e.u_final, 4) }}</span>
            <span class="ct-note micro">{{ sideUk }}</span>
          </div>
        </div>

        <dl class="facts facts--row">
          <div class="fact"><dt>{{ t('explain.entropy') }}</dt><dd>{{ num(e.agreement.H, 4) }}</dd></div>
          <div class="fact"><dt>{{ t('explain.agreement') }}</dt><dd>{{ num(e.agreement.A_g, 4) }}</dd></div>
          <div class="fact"><dt>{{ t('explain.mass') }}</dt><dd>{{ num(e.agreement.mass, 4) }}</dd></div>
          <div class="fact"><dt>{{ t('explain.kappamin') }}</dt><dd>{{ num(e.agreement.kappa_min, 2) }}</dd></div>
          <div class="fact"><dt>{{ t('explain.pplus') }}</dt><dd>{{ num(e.agreement.p_plus, 4) }}</dd></div>
          <div class="fact"><dt>{{ t('explain.pminus') }}</dt><dd>{{ num(e.agreement.p_minus, 4) }}</dd></div>
          <div class="fact"><dt>{{ t('explain.pzero') }}</dt><dd>{{ num(e.agreement.p_zero, 4) }}</dd></div>
        </dl>
      </section>

      <!-- 5. Сайзер -->
      <section class="section" data-shot="explain-sizer">
        <div class="section-head">
          <span class="section-step">Крок 5</span>
          <h2>{{ t('explain.s5') }}</h2>
          <span class="section-note">
            {{ t('explain.binding') }}:
            <b class="binding">{{ BINDING_UK[e.sizing.binding_constraint] ?? e.sizing.binding_constraint }}</b>
          </span>
        </div>

        <div class="grid-2">
          <dl class="facts">
            <div class="fact"><dt>{{ t('explain.qvt') }}</dt><dd>{{ num(e.sizing.q_vt, 4) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.qatr') }}</dt><dd>{{ num(e.sizing.q_atr, 4) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.qlev') }}</dt><dd>{{ num(e.sizing.q_lev, 4) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.sigma') }}</dt><dd>{{ pct(e.sizing.sigma_ann, 2) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.stopdist') }}</dt><dd>{{ num(e.sizing.stop_distance, 2) }}</dd></div>
          </dl>
          <dl class="facts">
            <div class="fact"><dt>{{ t('explain.qty') }}</dt><dd>{{ num(e.sizing.qty, 4) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.notional') }}</dt><dd>{{ money(e.sizing.notional) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.stop') }}</dt><dd>{{ money(e.prices.stop, 1) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.tp') }}</dt><dd>{{ money(e.prices.tp, 1) }}</dd></div>
            <div class="fact"><dt>{{ t('explain.liq') }}</dt><dd class="liq">{{ money(e.prices.liq, 1) }}</dd></div>
          </dl>
        </div>
      </section>

      <!-- 6. Ризик -->
      <section class="section" data-shot="explain-risk">
        <div class="section-head">
          <span class="section-step">Крок 6</span>
          <h2>{{ t('explain.s6') }}</h2>
          <span class="section-note">
            Ланцюг ніколи не збільшує експозицію: кожне правило може лише зменшити кількість.
          </span>
        </div>

        <div class="verdict-row">
          <span class="badge" :class="`badge--${e.risk.verdict.toLowerCase()}`">{{ e.risk.verdict }}</span>
          <span class="vr-item"><i>{{ t('explain.requested') }}</i><b class="num">{{ num(e.risk.requested_qty, 4) }}</b></span>
          <span class="vr-arrow" aria-hidden="true">→</span>
          <span class="vr-item"><i>{{ t('explain.approved') }}</i><b class="num">{{ num(e.risk.approved_qty, 4) }}</b></span>
          <span class="vr-item"><i>множник</i><b class="num">{{ num(e.risk.factor, 4) }}</b></span>
          <span class="badge" :class="`badge--${(e.risk.state || 'normal').toLowerCase()}`">{{ e.risk.state }}</span>
        </div>

        <div class="tbl-scroll">
          <table class="tbl">
            <thead>
              <tr>
                <th>{{ t('explain.riskrule') }}</th>
                <th>{{ t('explain.verdict') }}</th>
                <th class="r">{{ t('explain.observed') }}</th>
                <th class="r">{{ t('explain.limit') }}</th>
                <th class="r">{{ t('explain.factor') }}</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in e.risk.records" :key="r.rule">
                <td>{{ r.rule }}</td>
                <td><span class="badge" :class="`badge--${r.verdict.toLowerCase()}`">{{ r.verdict }}</span></td>
                <td class="r num">{{ num(r.observed, 4) }}</td>
                <td class="r num">{{ num(r.limit, 4) }}</td>
                <td class="r num">{{ num(r.factor, 3) }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>

      <!-- 7. Висновок -->
      <section class="section" data-shot="explain-conclusion">
        <div class="section-head">
          <span class="section-step">Крок 7</span>
          <h2>{{ t('explain.s7') }}</h2>
          <span class="section-note">{{ t('explain.narrative') }} · джерело: {{ e.narrative_source }}</span>
        </div>

        <blockquote class="narrative">{{ e.narrative_uk }}</blockquote>

        <div class="outcome">
          <div class="outcome-main" :class="`is-${uTone}`">
            <span class="om-label">{{ t('explain.side') }}</span>
            <strong class="om-value">{{ sideUk }}</strong>
            <span class="om-qty num">{{ num(e.target.qty, 4) }}</span>
          </div>
          <p class="small muted outcome-note">
            Затверджена кількість — результат усього ланцюга: нечіткий вивід дав напрям і силу,
            κ стиснув сигнал за неузгодженістю детекторів, сайзер переклав його в лоти,
            а ризик-ланцюг зменшив до
            <b class="num">{{ num(e.risk.approved_qty, 4) }}</b>
            за правилом «{{ BINDING_UK[e.sizing.binding_constraint] ?? e.sizing.binding_constraint }}».
          </p>
        </div>
      </section>
    </template>

    <div v-else class="state">
      <h3>{{ t('explain.empty') }}</h3>
    </div>
  </main>
</template>

<style scoped>
.head {
  display: flex; align-items: flex-start; justify-content: space-between;
  gap: var(--s8); flex-wrap: wrap; margin-bottom: var(--s8);
}
.head-text { max-width: 68ch; }
.head h1 { margin-bottom: var(--s2); }
.picker { display: flex; align-items: flex-end; gap: var(--s3); }
.picker .input { width: 120px; }

.loading { display: flex; flex-direction: column; gap: var(--s4); }

/* Паспорт — службовий рядок, тож моноширинний і тихий. */
.passport {
  display: flex; flex-wrap: wrap; gap: var(--s2) var(--s6);
  padding: var(--s3) var(--s4); margin-bottom: var(--s8);
  background: var(--paper-sunk); border: 1px solid var(--rule); border-radius: var(--radius);
}
.pp { display: inline-flex; align-items: baseline; gap: var(--s2); font-size: var(--t-small); }
.pp i { font-style: normal; color: var(--ink-faint); font-size: var(--t-micro); text-transform: uppercase; letter-spacing: 0.06em; }
.pp b { font-weight: 600; }
.pp--check { flex-direction: column; align-items: flex-start; gap: 0; margin-left: auto; }
.pp--check.is-ok b { color: var(--long); }
.pp--check.is-bad b { color: var(--short); }

.mfs { gap: var(--s6) var(--s8); }

.details { margin-top: var(--s6); border-top: 1px solid var(--rule); padding-top: var(--s3); }
.details summary {
  cursor: pointer; font-size: var(--t-small); color: var(--ink-soft);
  padding: var(--s1) 0; list-style-position: inside;
}
.details summary:hover { color: var(--ink); }
.dets {
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: var(--s2) var(--s8); margin: var(--s4) 0;
}
@media (max-width: 900px) { .dets { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
@media (max-width: 600px) { .dets { grid-template-columns: minmax(0, 1fr); } }

.agg { display: grid; grid-template-columns: minmax(0, 2.1fr) minmax(210px, 1fr); gap: var(--s8); align-items: start; }
@media (max-width: 900px) { .agg { grid-template-columns: minmax(0, 1fr); gap: var(--s6); } }
.sub { font-size: var(--t-small); color: var(--ink-faint); text-transform: uppercase; letter-spacing: 0.06em; margin-bottom: var(--s2); }
.agg-side .facts { margin-top: var(--s4); }

/* Ланцюг u_raw × κ = u_final — головна формула екрана. */
.chain {
  display: flex; align-items: stretch; gap: var(--s4); flex-wrap: wrap;
  padding: var(--s5) 0;
}
.chain-term {
  display: flex; flex-direction: column; gap: 2px; min-width: 140px;
  padding: var(--s3) var(--s4); border: 1px solid var(--rule); border-radius: var(--radius);
  background: var(--paper-raised);
}
.ct-label { font-family: var(--font-mono); font-size: var(--t-micro); color: var(--ink-faint); letter-spacing: 0.04em; }
.ct-value { font-size: var(--t-h2); font-weight: 600; letter-spacing: -0.02em; }
.ct-value--big { font-size: var(--t-num); line-height: 1.05; }
.ct-note { }
.chain-op {
  align-self: center; font-family: var(--font-mono); font-size: var(--t-h2);
  color: var(--ink-faint); font-weight: 400;
}
.chain-term--result { border-width: 1px; box-shadow: var(--shadow-1); }
.chain-term--result.is-long    { border-color: oklch(48% 0.125 152 / 0.4); background: var(--long-soft); }
.chain-term--result.is-long .ct-value, .chain-term--result.is-long .ct-note { color: var(--long); }
.chain-term--result.is-short   { border-color: oklch(50% 0.175 27 / 0.4); background: var(--short-soft); }
.chain-term--result.is-short .ct-value, .chain-term--result.is-short .ct-note { color: var(--short); }
.chain-term--result.is-neutral { border-color: var(--rule-strong); background: var(--paper-sunk); }

.facts--row { display: flex; flex-wrap: wrap; gap: 0 var(--s8); }
.facts--row .fact { border-bottom: 0; padding: var(--s1) 0; }

.binding { font-family: var(--font-mono); color: var(--accent); font-weight: 600; }
.liq { color: var(--halt); }

.verdict-row {
  display: flex; align-items: center; gap: var(--s4); flex-wrap: wrap;
  padding: var(--s3) var(--s4); margin-bottom: var(--s5);
  background: var(--paper-sunk); border: 1px solid var(--rule); border-radius: var(--radius);
}
.vr-item { display: inline-flex; align-items: baseline; gap: var(--s2); font-size: var(--t-small); }
.vr-item i { font-style: normal; color: var(--ink-faint); font-size: var(--t-micro); }
.vr-item b { font-weight: 600; }
.vr-arrow { color: var(--ink-faint); }

.narrative {
  margin: 0 0 var(--s6); padding: var(--s5) var(--s6);
  border-left: 1px solid var(--accent); background: var(--accent-soft);
  border-radius: 0 var(--radius) var(--radius) 0;
  font-size: var(--t-lead); line-height: 1.5; color: var(--ink); max-width: 76ch;
}

.outcome { display: flex; gap: var(--s8); align-items: flex-start; flex-wrap: wrap; }
.outcome-main {
  display: flex; flex-direction: column; gap: 2px; min-width: 200px;
  padding: var(--s4) var(--s5); border: 1px solid var(--rule-strong); border-radius: var(--radius);
}
.outcome-main.is-long  { border-color: oklch(48% 0.125 152 / 0.4); background: var(--long-soft); color: var(--long); }
.outcome-main.is-short { border-color: oklch(50% 0.175 27 / 0.4); background: var(--short-soft); color: var(--short); }
.outcome-main.is-neutral { background: var(--paper-sunk); color: var(--ink-soft); }
.om-label { font-size: var(--t-micro); text-transform: uppercase; letter-spacing: 0.06em; opacity: 0.75; }
.om-value { font-size: var(--t-h2); font-weight: 600; letter-spacing: -0.02em; }
.om-qty { font-size: var(--t-body); font-weight: 500; }
.outcome-note { flex: 1 1 320px; max-width: 64ch; }
</style>
