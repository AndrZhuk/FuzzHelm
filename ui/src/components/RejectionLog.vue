<script setup lang="ts">
/** Журнал вердиктів ризику: що саме обмежило ордер, яке спостереження і який ліміт.
    Порожній журнал — це добра новина, тож і формулювання позитивне. */
import { useI18n } from 'vue-i18n'
import { num, ts } from '@/lib/format'
import type { RiskEvent } from '@/lib/types'

defineProps<{ events: RiskEvent[]; limit?: number; emptyText?: string }>()
const { t } = useI18n()

const RULE_UK: Record<string, string> = {
  max_position_notional: 'номінал позиції',
  max_gross_leverage: 'валове плече',
  max_daily_loss: 'денний збиток',
  max_drawdown_halt: 'просадка → стоп',
  liquidation_buffer: 'буфер ліквідації',
  stale_data: 'застарілі дані',
  risk_mode: 'режим ризику',
}
</script>

<template>
  <div v-if="events.length > 0" class="tbl-scroll">
    <table class="tbl">
      <thead>
        <tr>
          <th>{{ t('explain.riskrule') }}</th>
          <th>{{ t('explain.verdict') }}</th>
          <th class="r">{{ t('explain.observed') }}</th>
          <th class="r">{{ t('explain.limit') }}</th>
          <th class="r">{{ t('explain.factor') }}</th>
          <th class="r">час</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in (limit ? events.slice(0, limit) : events)" :key="e.id">
          <td>{{ RULE_UK[e.rule] ?? e.rule }}</td>
          <td><span class="badge" :class="`badge--${(e.verdict ?? 'neutral').toLowerCase()}`">{{ e.verdict ?? '—' }}</span></td>
          <td class="r num">{{ num(e.observed, 4) }}</td>
          <td class="r num">{{ num(e.limit_value, 4) }}</td>
          <td class="r num">{{ num(e.factor, 3) }}</td>
          <td class="r num muted">{{ ts(e.ts_ns) }}</td>
        </tr>
      </tbody>
    </table>
  </div>
  <p v-else class="muted small">{{ emptyText ?? t('live.norejections') }}</p>
</template>
