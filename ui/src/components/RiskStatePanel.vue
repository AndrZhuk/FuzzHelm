<script setup lang="ts">
/** Режим ризик-автомата, капітал, просадка і кнопка зняття HALTED (лише admin).
    Кнопка веде через явне підтвердження з причиною — вона йде в audit_log. */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { money, pct, num, tsFull } from '@/lib/format'
import type { RiskState } from '@/lib/types'

defineProps<{
  state: RiskState | null
  canRelease: boolean
  releasing: boolean
}>()
const emit = defineEmits<{ release: [reason: string] }>()
const { t } = useI18n()

const confirming = ref(false)
const reason = ref('')

const TONE: Record<string, string> = {
  NORMAL: 'normal', WARNING: 'warning', COOLDOWN: 'cooldown', HALTED: 'halted',
}
const STATE_UK: Record<string, string> = {
  NORMAL: 'ШТАТНИЙ', WARNING: 'ЗАСТЕРЕЖЕННЯ', COOLDOWN: 'ВИТРИМКА', HALTED: 'АВАРІЙНИЙ СТОП',
}

function submit(): void {
  if (reason.value.trim().length < 3) return
  emit('release', reason.value.trim())
  confirming.value = false
  reason.value = ''
}
</script>

<template>
  <div class="risk">
    <div v-if="state" class="risk-state" :class="`is-${TONE[state.state] ?? 'neutral'}`">
      <span class="risk-label micro">{{ t('live.state') }}</span>
      <strong class="risk-name">{{ STATE_UK[state.state] ?? state.state }}</strong>
      <span class="risk-src micro muted">{{ state.state_source }}</span>
    </div>
    <p v-else class="muted small">{{ t('common.loading') }}</p>

    <dl v-if="state" class="facts">
      <div class="fact">
        <dt>{{ t('live.equity') }}</dt>
        <dd>{{ money(state.equity) }}</dd>
      </div>
      <div class="fact">
        <dt>{{ t('live.drawdown') }}</dt>
        <dd :class="{ neg: (state.drawdown ?? 0) > 0 }">{{ pct(state.drawdown, 3) }}</dd>
      </div>
      <div class="fact">
        <dt>{{ t('live.kappamode') }}</dt>
        <dd>{{ num(state.kappa_mode, 2) }}</dd>
      </div>
      <div class="fact">
        <dt>{{ t('live.since') }}</dt>
        <dd>{{ tsFull(state.equity_ts_ns) }}</dd>
      </div>
    </dl>

    <div v-if="state?.state === 'HALTED'" class="halt-box">
      <p class="small">
        Ризик-автомат зупинив торгівлю. Зняти засувку може лише адміністратор;
        дія пишеться в журнал аудиту з before/after.
      </p>

      <button v-if="canRelease && !confirming" class="btn btn--danger btn--sm" @click="confirming = true">
        {{ t('live.release') }}
      </button>
      <p v-else-if="!canRelease" class="micro muted">Ваша роль не може знімати аварійний стоп.</p>

      <form v-if="confirming" class="confirm" @submit.prevent="submit">
        <p class="small">{{ t('live.release_confirm') }}</p>
        <div class="field">
          <label for="release-reason">{{ t('live.release_reason') }}</label>
          <input
            id="release-reason" v-model="reason" class="input" type="text"
            minlength="3" maxlength="500" required autofocus
            placeholder="напр.: дані відновлено, причину просадки усунуто"
          >
        </div>
        <div class="row">
          <button type="submit" class="btn btn--danger btn--sm" :disabled="releasing || reason.trim().length < 3">
            {{ releasing ? t('live.releasing') : t('common.confirm') }}
          </button>
          <button type="button" class="btn btn--sm" @click="confirming = false; reason = ''">
            {{ t('common.cancel') }}
          </button>
        </div>
      </form>
    </div>
  </div>
</template>

<style scoped>
.risk { display: flex; flex-direction: column; gap: var(--s4); }

.risk-state {
  display: flex; align-items: baseline; gap: var(--s3); flex-wrap: wrap;
  padding: var(--s3) var(--s4); border-radius: var(--radius);
  border: 1px solid var(--rule-strong); background: var(--paper-sunk);
}
.risk-label { letter-spacing: 0.08em; text-transform: uppercase; color: var(--ink-faint); }
.risk-name {
  font-family: var(--font-mono); font-size: var(--t-h3); font-weight: 600; letter-spacing: -0.01em;
}
.is-normal   { background: var(--long-soft);  border-color: oklch(48% 0.125 152 / 0.3); }
.is-normal   .risk-name { color: var(--long); }
.is-warning  { background: var(--warn-soft);  border-color: oklch(58% 0.135 72 / 0.34); }
.is-warning  .risk-name { color: var(--warn); }
.is-cooldown { background: var(--cool-soft);  border-color: oklch(52% 0.105 300 / 0.3); }
.is-cooldown .risk-name { color: var(--cool); }
.is-halted   { background: var(--halt-soft);  border-color: oklch(44% 0.195 20 / 0.36); }
.is-halted   .risk-name { color: var(--halt); }

.facts dd.neg { color: var(--short); }

.halt-box {
  display: flex; flex-direction: column; gap: var(--s3);
  padding: var(--s4); border: 1px solid oklch(44% 0.195 20 / 0.3);
  border-radius: var(--radius); background: var(--halt-soft); color: var(--halt);
}
.confirm { display: flex; flex-direction: column; gap: var(--s3); }
</style>
