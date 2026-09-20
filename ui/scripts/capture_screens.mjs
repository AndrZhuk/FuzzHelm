/**
 * Екранограми веб-панелі для звіту (брифінг §12, gate фази 9: 14 екранограм).
 *
 * Запуск (потрібні піднятий API на 8000 і Vite на 5173):
 *   FUZZHELM_UI_LOGIN=demo_admin FUZZHELM_UI_PASSWORD=… node ui/scripts/capture_screens.mjs
 *
 * Скрипт детермінований: фіксована ширина вікна, deviceScaleFactor 2 (для друку),
 * вимкнені анімації, очікування на готовність полотен ECharts. Креденшли — лише з
 * оточення, ніколи не в коді (§0.2).
 */
import { chromium } from 'playwright'
import { mkdir } from 'node:fs/promises'
import path from 'node:path'

const UI = process.env.FUZZHELM_UI_URL ?? 'http://localhost:5173'
const LOGIN = process.env.FUZZHELM_UI_LOGIN
const PASSWORD = process.env.FUZZHELM_UI_PASSWORD
const DECISION = process.env.FUZZHELM_UI_DECISION ?? '8'
const OUT = path.resolve(process.env.FUZZHELM_SHOTS_DIR ?? 'docs/figures/screens')

if (!LOGIN || !PASSWORD) {
  console.error('Потрібні FUZZHELM_UI_LOGIN і FUZZHELM_UI_PASSWORD в оточенні.')
  process.exit(2)
}

/** 14 екранограм: назва файлу, маршрут, селектор (або null — уся сторінка), підпис для звіту. */
const SHOTS = [
  ['01_explain_full',        `/explain/${DECISION}`, null,                        'ExplainView — повне виведення рішення згори вниз'],
  ['02_explain_passport',    `/explain/${DECISION}`, '[data-shot=explain-passport]',   'Паспорт рішення і звірка перерахунку з журналом'],
  ['03_explain_inputs',      `/explain/${DECISION}`, '[data-shot=explain-inputs]',     'Крок 1 — входи T/R/V і функції належності з активними термами'],
  ['04_explain_rules',       `/explain/${DECISION}`, '[data-shot=explain-rules]',      'Крок 2 — спрацьовані правила, відсортовані за спаданням α'],
  ['05_explain_aggregate',   `/explain/${DECISION}`, '[data-shot=explain-aggregate]',  'Крок 3 — агрегована фігура μ_agg(u) і центроїд'],
  ['06_explain_kappa',       `/explain/${DECISION}`, '[data-shot=explain-kappa]',      'Крок 4 — u_raw × κ = u_final і показники узгодженості'],
  ['07_explain_sizer',       `/explain/${DECISION}`, '[data-shot=explain-sizer]',      'Крок 5 — розкладка сайзера і binding_constraint'],
  ['08_explain_risk',        `/explain/${DECISION}`, '[data-shot=explain-risk]',       'Крок 6 — ризик-ланцюг: запитано → затверджено'],
  ['09_explain_conclusion',  `/explain/${DECISION}`, '[data-shot=explain-conclusion]', 'Крок 7 — україномовне трасування і підсумок'],
  ['10_live_market',         '/live',                '[data-shot=live-market]',        'LiveView — свічки з лінією ліквідації і маркерами'],
  ['11_live_detectors',      '/live',                '[data-shot=live-detectors]',     'LiveView — шість детекторів: сила s і довіра c'],
  ['12_live_fis_risk',       '/live',                null,                             'LiveView — вихід нечіткого ядра, режим ризику і потік подій'],
  ['13_backtest_equity',     '/backtest',            '[data-shot=backtest-equity]',    'BacktestView — крива капіталу з просадкою і смугами режимів'],
  ['14_backtest_passport',   '/backtest',            '[data-shot=backtest-passport]',  'BacktestView — паспорт відтворюваності прогону'],
]

/** Чекаємо, доки всі полотна ECharts у кадрі намальовані (інакше в кадр потрапить порожнеча). */
async function waitForCharts(page, selector) {
  const scope = selector ?? 'body'
  await page.waitForFunction((sel) => {
    const root = document.querySelector(sel)
    if (!root) return false
    const frames = root.querySelectorAll('.canvas')
    return [...frames].every((f) => {
      const c = f.querySelector('canvas')
      return c !== null && c.width > 0 && c.height > 0
    })
  }, scope, { timeout: 20_000 }).catch(() => {})
  await page.waitForTimeout(700)
}

const browser = await chromium.launch()
const ctx = await browser.newContext({
  viewport: { width: 1440, height: 960 },
  deviceScaleFactor: 2,          // різкі екранограми для друкованого звіту
  locale: 'uk-UA',
  timezoneId: 'Europe/Kyiv',
  reducedMotion: 'reduce',
})
const page = await ctx.newPage()

await mkdir(OUT, { recursive: true })

// Вхід: через форму, щоб токен і стан сторів були справжні.
// networkidle не годиться: LiveView тримає відкритий SSE-потік і «тиші» в мережі не буде.
await page.goto(`${UI}/explain/${DECISION}`, { waitUntil: 'domcontentloaded' })
await page.fill('#lg', LOGIN)
await page.fill('#pw', PASSWORD)
await page.click('button[type=submit]')
await page.waitForSelector('nav.nav', { timeout: 15_000 })

const captions = []
let current = null

for (const [name, route, selector, caption] of SHOTS) {
  if (route !== current) {
    await page.goto(`${UI}${route}`, { waitUntil: 'domcontentloaded' })
    current = route
    if (route === '/live') {
      // LiveView наповнюється з потоку подій: чекаємо, доки прийде перше рішення і
      // підтягнеться розкладка детекторів (див. hydrate() у LiveView).
      await page.waitForFunction(
        () => document.querySelectorAll('[data-shot=live-detectors] .det').length > 0,
        undefined, { timeout: 90_000 },
      ).catch(() => console.warn('  ! рішення з потоку не дочекались — запустіть воркер реплею'))
      await page.waitForTimeout(1500)
    } else {
      await page.waitForTimeout(1800)
    }
  }
  await waitForCharts(page, selector)

  const file = path.join(OUT, `${name}.png`)
  if (selector === null) {
    await page.screenshot({ path: file, fullPage: true })
  } else {
    const el = page.locator(selector).first()
    await el.scrollIntoViewIfNeeded()
    await page.waitForTimeout(400)
    await el.screenshot({ path: file })
  }
  captions.push(`| ${name}.png | ${caption} |`)
  console.log(`✓ ${name}.png`)
}

// Підписи до рисунків — готовий фрагмент для звіту.
const md = [
  '# Екранограми веб-панелі',
  '',
  `Знято ${new Date().toISOString().slice(0, 10)} скриптом \`ui/scripts/capture_screens.mjs\``,
  '(1440×960, deviceScaleFactor 2, локаль uk-UA). Перезняти: `make screens`.',
  '',
  '| Файл | Що на екранограмі |',
  '|---|---|',
  ...captions,
  '',
].join('\n')
await (await import('node:fs/promises')).writeFile(path.join(OUT, 'README.md'), md, 'utf8')

await browser.close()
console.log(`\n${SHOTS.length} екранограм у ${OUT}`)
