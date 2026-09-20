/** Клієнт REST/SSE. Один шлях помилки: ApiError з кодом і україномовним текстом. */

export class ApiError extends Error {
  constructor(readonly status: number, message: string, readonly detail?: unknown) {
    super(message)
    this.name = 'ApiError'
  }
}

const TOKEN_KEY = 'fuzzhelm.token'

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY) } catch { return null }
}
export function setToken(t: string | null): void {
  try { t === null ? localStorage.removeItem(TOKEN_KEY) : localStorage.setItem(TOKEN_KEY, t) } catch { /* приватне вікно */ }
}

/** 401 гасить сесію один раз і сповіщає застосунок, а не кожен виклик окремо. */
function onUnauthorized(): void {
  setToken(null)
  window.dispatchEvent(new CustomEvent('fuzzhelm:unauthorized'))
}

function messageFor(status: number, detail: unknown): string {
  if (typeof detail === 'string' && detail.trim() !== '') return detail
  if (Array.isArray(detail) && detail.length > 0) {
    const first = detail[0] as { msg?: string; loc?: unknown[] }
    if (first?.msg) return `${first.msg}${first.loc ? ` (${first.loc.join('.')})` : ''}`
  }
  switch (status) {
    case 401: return 'Сесія недійсна або завершилась — увійдіть ще раз.'
    case 403: return 'Ваша роль не має права на цю дію.'
    case 404: return 'Ресурс не знайдено.'
    case 409: return 'Конфлікт стану: дані змінились під час запиту.'
    case 422: return 'Сервер відхилив параметри запиту.'
    case 0:   return 'Немає зв’язку з API. Перевірте, що бекенд запущено.'
    default:  return `Помилка запиту (HTTP ${status}).`
  }
}

export async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (init.body !== undefined && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (token) headers.set('Authorization', `Bearer ${token}`)

  let res: Response
  try {
    res = await fetch(`/api${path}`, { ...init, headers })
  } catch {
    throw new ApiError(0, messageFor(0, null))
  }

  if (res.status === 401) { onUnauthorized(); throw new ApiError(401, messageFor(401, null)) }
  if (res.status === 204) return undefined as T

  const text = await res.text()
  let body: unknown = null
  if (text !== '') { try { body = JSON.parse(text) } catch { body = text } }

  if (!res.ok) {
    const detail = (body as { detail?: unknown } | null)?.detail ?? body
    throw new ApiError(res.status, messageFor(res.status, detail), detail)
  }
  return body as T
}

export function qs(params: Record<string, string | number | boolean | null | undefined>): string {
  const u = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) if (v !== null && v !== undefined && v !== '') u.set(k, String(v))
  const s = u.toString()
  return s ? `?${s}` : ''
}

/**
 * Потік SSE. EventSource не вміє заголовка Authorization, а бекенд свідомо не приймає
 * токен у query-рядку (він осідає в журналах проксі — docs/security.md). Тому читаємо
 * fetch + ReadableStream і самі розбираємо кадри SSE. Розрив відновлюємо з Last-Event-ID.
 */
export interface StreamHandle { close(): void }

export function openStream(
  onEvent: (kind: string, data: unknown, id: string | null) => void,
  opts: { kinds?: string[]; onError?: (message: string) => void; onOpen?: () => void } = {},
): StreamHandle {
  const ctrl = new AbortController()
  let lastId: string | null = null
  let stopped = false
  let attempt = 0

  const dispatch = (raw: string): void => {
    let kind = 'message'
    let id: string | null = null
    const dataLines: string[] = []
    for (const line of raw.split('\n')) {
      if (line.startsWith(':')) continue
      const sep = line.indexOf(':')
      const field = sep === -1 ? line : line.slice(0, sep)
      const value = sep === -1 ? '' : line.slice(sep + 1).replace(/^ /, '')
      if (field === 'event') kind = value
      else if (field === 'data') dataLines.push(value)
      else if (field === 'id') { id = value; lastId = value }
    }
    if (dataLines.length === 0) return
    const text = dataLines.join('\n')
    let parsed: unknown = text
    try { parsed = JSON.parse(text) } catch { /* не-JSON лишаємо рядком */ }
    onEvent(kind, parsed, id)
  }

  const run = async (): Promise<void> => {
    while (!stopped) {
      try {
        const headers = new Headers({ Accept: 'text/event-stream' })
        const token = getToken()
        if (token) headers.set('Authorization', `Bearer ${token}`)
        if (lastId !== null) headers.set('Last-Event-ID', lastId)

        const query = qs({ kinds: opts.kinds?.join(',') })
        const res = await fetch(`/api/stream/live${query}`, { headers, signal: ctrl.signal })
        if (res.status === 401) { onUnauthorized(); opts.onError?.(messageFor(401, null)); return }
        if (!res.ok || res.body === null) { throw new ApiError(res.status, messageFor(res.status, null)) }

        attempt = 0
        opts.onOpen?.()

        const reader = res.body.pipeThrough(new TextDecoderStream()).getReader()
        let buffer = ''
        const SEP = /\r?\n\r?\n/
        for (;;) {
          const { done, value } = await reader.read()
          if (done) break
          buffer += value
          // Кадр SSE завершує порожній рядок; \r\n\r\n — той самий роздільник.
          for (;;) {
            const m = SEP.exec(buffer)
            if (m === null) break
            const frame = buffer.slice(0, m.index)
            buffer = buffer.slice(m.index + m[0].length)
            if (frame.trim() !== '') dispatch(frame)
          }
        }
      } catch (e) {
        if (stopped || ctrl.signal.aborted) return
        opts.onError?.(e instanceof ApiError ? e.message : 'Потік подій обірвано — перепідключення…')
      }
      if (stopped) return
      // Експоненційна витримка з межею: 1, 2, 4, 8, максимум 15 с.
      attempt += 1
      const waitMs = Math.min(1000 * 2 ** (attempt - 1), 15_000)
      await new Promise((r) => setTimeout(r, waitMs))
    }
  }

  void run()
  return { close: () => { stopped = true; ctrl.abort() } }
}
