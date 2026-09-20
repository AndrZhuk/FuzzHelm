import { createI18n } from 'vue-i18n'
import uk from './uk.json'
import en from './en.json'

const KEY = 'fuzzhelm.locale'
export type Locale = 'uk' | 'en'

function initial(): Locale {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved === 'uk' || saved === 'en') return saved
  } catch { /* приватне вікно */ }
  return 'uk' // інтерфейс українською за замовчуванням (§8.2)
}

export const i18n = createI18n({
  legacy: false,
  locale: initial(),
  fallbackLocale: 'uk',
  messages: { uk, en },
})

export function setLocale(l: Locale): void {
  i18n.global.locale.value = l
  document.documentElement.lang = l
  try { localStorage.setItem(KEY, l) } catch { /* приватне вікно */ }
}
