<script setup lang="ts">
/** Оболонка застосунку: шапка стану, навігація трьома екранами, перемикач мови, вихід. */
import { onMounted, onBeforeUnmount, ref, computed } from 'vue'
import { useI18n } from 'vue-i18n'
import { useAuth } from '@/stores/auth'
import { useMarket } from '@/stores/market'
import { useRisk } from '@/stores/risk'
import { setLocale, type Locale } from '@/i18n'
import StatusBanner from '@/components/StatusBanner.vue'
import LoginView from '@/views/LoginView.vue'

const auth = useAuth()
const market = useMarket()
const risk = useRisk()
const { t, locale } = useI18n()
const booted = ref(false)

const ROLE_UK: Record<string, string> = {
  admin: 'адміністратор', operator: 'оператор', analyst: 'аналітик', auditor: 'аудитор',
}

function onUnauthorized(): void { auth.logout() }

onMounted(async () => {
  window.addEventListener('fuzzhelm:unauthorized', onUnauthorized)
  await auth.restore()
  if (auth.isAuthenticated) void Promise.all([market.loadHealth(), risk.loadState()])
  booted.value = true
})
onBeforeUnmount(() => window.removeEventListener('fuzzhelm:unauthorized', onUnauthorized))

function switchLocale(l: Locale): void { setLocale(l); locale.value = l }

const currentLocale = computed(() => locale.value as Locale)
</script>

<template>
  <div v-if="!booted" class="boot" aria-busy="true">
    <span class="skel boot-bar" />
  </div>

  <LoginView v-else-if="!auth.isAuthenticated" />

  <template v-else>
    <StatusBanner :health="market.health" :risk-state="risk.state?.state ?? null" />

    <header class="head no-print">
      <div class="head-in">
        <RouterLink to="/explain" class="brand">
          <span class="mark" aria-hidden="true">
            <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.7">
              <path d="M3 17c3.2 0 3.2-10 6.4-10S12.6 17 15.8 17s3.2-10 5.2-10" stroke-linecap="round" />
            </svg>
          </span>
          <span class="brand-text">
            <b>FuzzHelm</b>
            <i class="micro">{{ t('app.tagline') }}</i>
          </span>
        </RouterLink>

        <nav class="nav" :aria-label="t('app.name')">
          <RouterLink to="/explain">{{ t('app.nav.explain') }}</RouterLink>
          <RouterLink to="/live">{{ t('app.nav.live') }}</RouterLink>
          <RouterLink to="/backtest">{{ t('app.nav.backtest') }}</RouterLink>
        </nav>

        <div class="tools">
          <div class="lang" role="group" :aria-label="t('app.lang')">
            <button
              v-for="l in (['uk', 'en'] as Locale[])" :key="l"
              class="lang-btn" :class="{ 'is-on': currentLocale === l }"
              :aria-pressed="currentLocale === l" @click="switchLocale(l)"
            >{{ l.toUpperCase() }}</button>
          </div>

          <span class="who small">
            <b>{{ auth.me?.login }}</b>
            <i class="micro muted">{{ ROLE_UK[auth.role ?? ''] ?? auth.role }}</i>
          </span>

          <button class="btn btn--sm" @click="auth.logout()">{{ t('app.logout') }}</button>
        </div>
      </div>
    </header>

    <RouterView v-slot="{ Component }">
      <component :is="Component" />
    </RouterView>

    <footer class="foot no-print">
      <p class="micro muted">
        FuzzHelm · проєктно-технологічна практика · кафедра АСУ НУ «Львівська політехніка» ·
        паперовий режим, торгівля реальними коштами неможлива за побудовою
      </p>
    </footer>
  </template>
</template>

<style scoped>
.boot { min-height: 100dvh; display: grid; place-items: center; }
.boot-bar { width: 180px; height: 3px; }

.head { position: sticky; top: 0; z-index: 20; background: var(--paper); border-bottom: 1px solid var(--rule); }
.head-in {
  max-width: 1320px; margin: 0 auto; padding: var(--s3) var(--s6);
  display: flex; align-items: center; gap: var(--s6);
}
.brand { display: inline-flex; align-items: center; gap: var(--s2); color: inherit; text-decoration: none; }
.mark {
  display: grid; place-items: center; width: 30px; height: 30px; flex: 0 0 auto;
  border: 1px solid var(--rule-strong); border-radius: var(--radius);
  color: var(--accent); background: var(--accent-soft);
}
.brand-text { display: flex; flex-direction: column; line-height: 1.15; }
.brand-text b { font-size: var(--t-body); font-weight: 600; letter-spacing: -0.02em; }
.brand-text i { font-style: normal; color: var(--ink-faint); }

.nav { display: flex; gap: var(--s1); }
.nav a {
  padding: var(--s2) var(--s3); border-radius: var(--radius);
  color: var(--ink-soft); text-decoration: none; font-size: var(--t-small); font-weight: 500;
  border-bottom: 2px solid transparent;
  transition: color 140ms var(--ease), background 140ms var(--ease), border-color 140ms var(--ease);
}
.nav a:hover { color: var(--ink); background: var(--paper-sunk); }
.nav a.router-link-active { color: var(--accent); border-bottom-color: var(--accent); }

.tools { margin-left: auto; display: flex; align-items: center; gap: var(--s4); }
.lang { display: inline-flex; border: 1px solid var(--rule-strong); border-radius: var(--radius); overflow: hidden; }
.lang-btn {
  padding: 3px var(--s2); border: 0; background: transparent; color: var(--ink-faint);
  font-family: var(--font-mono); font-size: var(--t-micro); font-weight: 600; letter-spacing: 0.04em;
  transition: background 140ms var(--ease), color 140ms var(--ease);
}
.lang-btn:hover { background: var(--paper-sunk); color: var(--ink); }
.lang-btn.is-on { background: var(--accent); color: oklch(99% 0 0); }
.who { display: flex; flex-direction: column; line-height: 1.2; }
.who i { font-style: normal; }

.foot { border-top: 1px solid var(--rule); margin-top: var(--s16); }
.foot p { max-width: 1320px; margin: 0 auto; padding: var(--s5) var(--s6); }

@media (max-width: 900px) {
  .head-in { flex-wrap: wrap; gap: var(--s3); padding: var(--s3) var(--s4); }
  .nav { order: 3; width: 100%; overflow-x: auto; }
  .tools { gap: var(--s3); }
  .brand-text i { display: none; }
  .foot p { padding: var(--s5) var(--s4); }
}
</style>
