/** Форматування чисел і часу. Усе — з фіксованою локаллю, щоб екранограми були відтворювані. */

const LOCALE = 'uk-UA'

export function num(v: number | string | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = typeof v === 'string' ? Number(v) : v
  if (!Number.isFinite(n)) return '—'
  return n.toLocaleString(LOCALE, { minimumFractionDigits: digits, maximumFractionDigits: digits })
}

/** Підпис із знаком: для s-компонент детекторів, u_raw, u_final. */
export function signed(v: number | string | null | undefined, digits = 3): string {
  if (v === null || v === undefined || v === '') return '—'
  const n = typeof v === 'string' ? Number(v) : v
  if (!Number.isFinite(n)) return '—'
  return (n > 0 ? '+' : '') + num(n, digits)
}

export function pct(v: number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || !Number.isFinite(v)) return '—'
  return `${num(v * 100, digits)} %`
}

export function money(v: string | number | null | undefined, digits = 2): string {
  if (v === null || v === undefined || v === '') return '—'
  return num(v, digits)
}

/** Наносекунди епохи → «18.09, 14:32:00» (демо і звіт дивляться на хвилини). */
export function ts(ns: number | null | undefined, withSeconds = true): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return '—'
  const d = new Date(Number(ns) / 1e6)
  return d.toLocaleString(LOCALE, {
    day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit',
    ...(withSeconds ? { second: '2-digit' } : {}),
  })
}

export function tsFull(ns: number | null | undefined): string {
  if (ns === null || ns === undefined || !Number.isFinite(ns)) return '—'
  return new Date(Number(ns) / 1e6).toLocaleString(LOCALE, {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  })
}

/** Тривалість у людських одиницях: лаг конвеєра буває і 30 год. */
export function duration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds)) return '—'
  const s = Math.abs(seconds)
  if (s < 90) return `${num(s, 1)} с`
  if (s < 5400) return `${num(s / 60, 1)} хв`
  if (s < 172800) return `${num(s / 3600, 1)} год`
  return `${num(s / 86400, 1)} дн`
}

/** Хеш паспорта: показуємо перші 8 символів, повний — у title. */
export function shortHash(h: string | null | undefined, n = 8): string {
  if (!h) return '—'
  return h.length <= n ? h : `${h.slice(0, n)}…`
}

export const RISK_TONE: Record<string, string> = {
  NORMAL: 'normal', WARNING: 'warning', COOLDOWN: 'cooldown', HALTED: 'halted',
}
export const VERDICT_TONE: Record<string, string> = {
  ALLOW: 'allow', SHRINK: 'shrink', VETO: 'veto',
}

/** Колір терму виходу U за знаком: ЛОНГ зелений, ШОРТ червоний, УТРИМАННЯ нейтральний. */
export function consequentTone(consequent: string): 'long' | 'short' | 'neutral' {
  if (consequent.includes('LONG')) return 'long'
  if (consequent.includes('SHORT')) return 'short'
  return 'neutral'
}

export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim()
}
