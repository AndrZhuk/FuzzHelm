<script setup lang="ts">
/** Редактор бази правил: два YAML-документи (membership + rules) з миттєвою перевіркою
    синтаксису в браузері та семантичною перевіркою на сервері при збереженні.
    Версіонування веде API: кожне збереження створює нову версію стратегії. */
import { computed, ref, watch } from 'vue'
import { useI18n } from 'vue-i18n'
import yaml from 'js-yaml'

const props = defineProps<{
  membership: string
  rules: string
  busy?: boolean
  canEdit: boolean
}>()
const emit = defineEmits<{
  save: [payload: { name: string; membership_yaml: string; rules_yaml: string; activate: boolean }]
}>()
const { t } = useI18n()

const name = ref('')
const membershipText = ref(props.membership)
const rulesText = ref(props.rules)
const activate = ref(true)

watch(() => props.membership, (v) => { membershipText.value = v })
watch(() => props.rules, (v) => { rulesText.value = v })

interface Check { ok: boolean; message: string; rules?: number }

/** Синтаксис + мінімальна структура: далі семантику перевіряє сервер. */
function check(text: string, kind: 'membership' | 'rules'): Check {
  if (text.trim() === '') return { ok: false, message: 'Документ порожній.' }
  try {
    const doc = yaml.load(text) as Record<string, unknown> | null
    if (doc === null || typeof doc !== 'object') return { ok: false, message: 'Очікувався YAML-словник верхнього рівня.' }
    if (kind === 'rules') {
      const list = (doc.rules ?? doc) as unknown
      if (!Array.isArray(list)) return { ok: false, message: 'Немає списку rules:.' }
      if (list.length === 0) return { ok: false, message: 'Список правил порожній.' }
      return { ok: true, message: `${t('backtest.valid')}: ${list.length} правил.`, rules: list.length }
    }
    const vars = Object.keys(doc.variables ?? doc)
    if (vars.length === 0) return { ok: false, message: 'Немає жодної змінної.' }
    return { ok: true, message: `${t('backtest.valid')}: змінні ${vars.join(', ')}.` }
  } catch (e) {
    const msg = e instanceof Error ? e.message.split('\n')[0] : String(e)
    return { ok: false, message: `${t('backtest.invalid')}: ${msg}` }
  }
}

const mCheck = computed(() => check(membershipText.value, 'membership'))
const rCheck = computed(() => check(rulesText.value, 'rules'))
const nameOk = computed(() => /^[A-Za-z0-9_.-]{1,64}$/.test(name.value))
const canSave = computed(() => props.canEdit && nameOk.value && mCheck.value.ok && rCheck.value.ok && !props.busy)

function submit(): void {
  if (!canSave.value) return
  emit('save', {
    name: name.value,
    membership_yaml: membershipText.value,
    rules_yaml: rulesText.value,
    activate: activate.value,
  })
}
</script>

<template>
  <form class="editor" @submit.prevent="submit">
    <div class="editor-head">
      <div class="field name-field">
        <label for="strategy-name">{{ t('backtest.name') }}</label>
        <input
          id="strategy-name" v-model="name" class="input" type="text"
          :disabled="!canEdit" placeholder="напр.: mamdani_base"
          pattern="[A-Za-z0-9_.\-]+" maxlength="64"
        >
        <span v-if="name !== '' && !nameOk" class="micro err">
          Дозволені лише латиниця, цифри та символи . _ -
        </span>
      </div>
      <label class="activate small">
        <input v-model="activate" type="checkbox" :disabled="!canEdit">
        зробити версію активною
      </label>
    </div>

    <div class="grid-2 docs">
      <div class="doc">
        <div class="doc-head">
          <label for="ed-membership" class="doc-title">{{ t('backtest.membership') }}</label>
          <span class="check" :class="mCheck.ok ? 'is-ok' : 'is-bad'">{{ mCheck.message }}</span>
        </div>
        <textarea
          id="ed-membership" v-model="membershipText" class="textarea"
          rows="16" spellcheck="false" :disabled="!canEdit"
        />
      </div>

      <div class="doc">
        <div class="doc-head">
          <label for="ed-rules" class="doc-title">{{ t('backtest.rules') }}</label>
          <span class="check" :class="rCheck.ok ? 'is-ok' : 'is-bad'">{{ rCheck.message }}</span>
        </div>
        <textarea
          id="ed-rules" v-model="rulesText" class="textarea"
          rows="16" spellcheck="false" :disabled="!canEdit"
        />
      </div>
    </div>

    <div class="row">
      <button type="submit" class="btn btn--primary" :disabled="!canSave">
        {{ busy ? t('common.loading') : t('backtest.save') }}
      </button>
      <p v-if="!canEdit" class="micro muted">
        Змінювати стратегії може роль «оператор» або вище — ваша роль лише читає.
      </p>
    </div>
  </form>
</template>

<style scoped>
.editor { display: flex; flex-direction: column; gap: var(--s5); }
.editor-head { display: flex; align-items: flex-end; gap: var(--s6); flex-wrap: wrap; }
.name-field { min-width: 260px; }
.activate { display: inline-flex; align-items: center; gap: var(--s2); color: var(--ink-soft); }
.docs { gap: var(--s5); }
.doc { display: flex; flex-direction: column; gap: var(--s2); min-width: 0; }
.doc-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--s3); flex-wrap: wrap; }
.doc-title {
  font-family: var(--font-mono); font-size: var(--t-small); font-weight: 600;
}
.check { font-size: var(--t-micro); }
.is-ok  { color: var(--long); }
.is-bad { color: var(--short); }
.err { color: var(--short); }
.textarea { min-height: 280px; font-size: var(--t-small); }
</style>
