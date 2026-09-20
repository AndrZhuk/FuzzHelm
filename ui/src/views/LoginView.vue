<script setup lang="ts">
/** Вхід за логіном і паролем; токен HS256 живе 8 годин. */
import { ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/stores/auth'

const auth = useAuth()
const { t } = useI18n()
const login = ref('')
const password = ref('')

async function submit(): Promise<void> {
  await auth.login(login.value, password.value)
  password.value = ''
}
</script>

<template>
  <main class="wrap">
    <div class="card">
      <div class="brand">
        <span class="mark" aria-hidden="true">
          <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.6">
            <path d="M3 17c3.2 0 3.2-10 6.4-10S12.6 17 15.8 17s3.2-10 5.2-10" stroke-linecap="round" />
          </svg>
        </span>
        <div>
          <h1>FuzzHelm</h1>
          <p class="micro muted">{{ t('app.tagline') }}</p>
        </div>
      </div>

      <h2 class="title">{{ t('login.title') }}</h2>
      <p class="small muted sub">{{ t('login.sub') }}</p>

      <form class="form" @submit.prevent="submit">
        <div class="field">
          <label for="lg">{{ t('login.login') }}</label>
          <input id="lg" v-model="login" class="input" type="text" autocomplete="username" required autofocus>
        </div>
        <div class="field">
          <label for="pw">{{ t('login.password') }}</label>
          <input id="pw" v-model="password" class="input" type="password" autocomplete="current-password" required>
        </div>

        <p v-if="auth.error" class="err small" role="alert">{{ auth.error }}</p>

        <button class="btn btn--primary" type="submit" :disabled="auth.pending">
          {{ auth.pending ? t('login.pending') : t('login.submit') }}
        </button>
      </form>

      <p class="micro muted hint">{{ t('login.hint') }}</p>
    </div>
  </main>
</template>

<style scoped>
.wrap { min-height: 100dvh; display: grid; place-items: center; padding: var(--s6); }
.card {
  width: min(420px, 100%); display: flex; flex-direction: column; gap: var(--s4);
  padding: var(--s8); background: var(--paper-raised);
  border: 1px solid var(--rule); border-radius: var(--radius-lg); box-shadow: var(--shadow-2);
}
.brand { display: flex; align-items: center; gap: var(--s3); padding-bottom: var(--s4); border-bottom: 1px solid var(--rule); }
.brand h1 { font-size: var(--t-h2); }
.mark {
  display: grid; place-items: center; width: 38px; height: 38px; flex: 0 0 auto;
  border: 1px solid var(--rule-strong); border-radius: var(--radius); color: var(--accent);
  background: var(--accent-soft);
}
.title { font-size: var(--t-h3); }
.sub { margin-top: calc(var(--s2) * -1); max-width: 46ch; }
.form { display: flex; flex-direction: column; gap: var(--s4); margin-top: var(--s2); }
.err { color: var(--short); }
.hint { padding-top: var(--s3); border-top: 1px solid var(--rule); }
</style>
