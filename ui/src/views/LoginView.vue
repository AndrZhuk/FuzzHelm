<script setup lang="ts">
/**
 * Вхід за логіном і паролем; токен HS256 живе 8 годин.
 * На локальному стенді з FUZZHELM_DEMO_LOGIN=1 — ще й кнопки швидкого входу під демо-користувачами:
 * список і токени видає сервер, паролів у коді панелі немає.
 */
import { onMounted, ref } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/stores/auth'

const auth = useAuth()
const { t } = useI18n()
const login = ref('')
const password = ref('')

onMounted(() => { void auth.loadDemoUsers() })

async function submit(): Promise<void> {
  await auth.login(login.value, password.value)
  password.value = ''
}

async function quick(demoLogin: string): Promise<void> {
  await auth.demoLogin(demoLogin)
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

      <section v-if="auth.demoUsers.length > 0" class="quick" aria-labelledby="quick-title">
        <div class="quick-head">
          <h3 id="quick-title">{{ t('login.quick') }}</h3>
          <span class="quick-note">{{ t('login.quickNote') }}</span>
        </div>
        <ul class="quick-list">
          <li v-for="u in auth.demoUsers" :key="u.login">
            <button
              type="button" class="quick-btn" :data-role="u.role"
              :disabled="auth.pending" :aria-busy="auth.pendingLogin === u.login"
              @click="quick(u.login)"
            >
              <span class="q-role">{{ t(`login.roles.${u.role}`) }}</span>
              <span class="q-note">{{ t(`login.roleNote.${u.role}`) }}</span>
              <span class="q-login">
                {{ auth.pendingLogin === u.login ? t('login.quickPending') : u.login }}
                <svg viewBox="0 0 16 16" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.6" aria-hidden="true">
                  <path d="M3 8h10M9 4l4 4-4 4" stroke-linecap="round" stroke-linejoin="round" />
                </svg>
              </span>
            </button>
          </li>
        </ul>
        <div class="or" role="separator"><span>{{ t('login.or') }}</span></div>
      </section>

      <form class="form" @submit.prevent="submit">
        <div class="field">
          <label for="lg">{{ t('login.login') }}</label>
          <input
            id="lg" v-model="login" class="input" type="text" autocomplete="username" required
            :autofocus="auth.demoUsers.length === 0"
          >
        </div>
        <div class="field">
          <label for="pw">{{ t('login.password') }}</label>
          <input id="pw" v-model="password" class="input" type="password" autocomplete="current-password" required>
        </div>

        <p v-if="auth.error" class="err small" role="alert">{{ auth.error }}</p>

        <button class="btn btn--primary" type="submit" :disabled="auth.pending">
          {{ auth.pending && auth.pendingLogin === null ? t('login.pending') : t('login.submit') }}
        </button>
      </form>

      <p class="micro muted hint">{{ auth.demoUsers.length > 0 ? t('login.hintDemo') : t('login.hint') }}</p>
    </div>
  </main>
</template>

<style scoped>
.wrap { min-height: 100dvh; display: grid; place-items: center; padding: var(--s6); }
.card {
  width: min(440px, 100%); display: flex; flex-direction: column; gap: var(--s4);
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

/* ---- швидкий вхід демо-користувачами */
.quick { display: flex; flex-direction: column; gap: var(--s3); margin-top: var(--s2); }
.quick-head { display: flex; align-items: baseline; justify-content: space-between; gap: var(--s3); flex-wrap: wrap; }
.quick-head h3 { font-size: var(--t-small); font-weight: 600; }
.quick-note { font-size: var(--t-micro); color: var(--ink-soft); }
.quick-list { list-style: none; margin: 0; padding: 0; display: grid; gap: var(--s2); }
.quick-btn {
  width: 100%; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center;
  column-gap: var(--s3); row-gap: 2px; padding: var(--s3) var(--s4); text-align: left;
  border: 1px solid var(--rule-strong); border-radius: var(--radius);
  background: var(--paper-raised); color: var(--ink); font: inherit; cursor: pointer;
  transition: background 140ms var(--ease), border-color 140ms var(--ease);
}
.quick-btn:hover:not(:disabled) { background: var(--accent-soft); border-color: var(--accent); }
.quick-btn:hover:not(:disabled) .q-login { color: var(--accent); }
.quick-btn:active:not(:disabled) { background: oklch(89% 0.05 258); }
.quick-btn:disabled { cursor: default; opacity: 0.55; }
.quick-btn[aria-busy='true'] { opacity: 1; background: var(--accent-soft); border-color: var(--accent); }
.q-role { grid-column: 1; font-size: var(--t-small); font-weight: 600; }
.q-note { grid-column: 1; font-size: var(--t-micro); color: var(--ink-soft); }
.q-login {
  grid-column: 2; grid-row: 1 / span 2; display: inline-flex; align-items: center; gap: var(--s1);
  font-family: var(--font-mono); font-size: var(--t-micro); color: var(--ink-soft);
  transition: color 140ms var(--ease);
}
.or {
  display: flex; align-items: center; gap: var(--s3); margin-top: var(--s1);
  font-size: var(--t-micro); color: var(--ink-soft); letter-spacing: 0.05em; text-transform: uppercase;
}
.or::before, .or::after { content: ''; flex: 1; height: 1px; background: var(--rule); }

.form { display: flex; flex-direction: column; gap: var(--s4); margin-top: var(--s2); }
.err { color: var(--short); }
.hint { padding-top: var(--s3); border-top: 1px solid var(--rule); }

@media (max-width: 420px) {
  .card { padding: var(--s6) var(--s4); }
  .quick-btn { padding: var(--s3); }
}
</style>
