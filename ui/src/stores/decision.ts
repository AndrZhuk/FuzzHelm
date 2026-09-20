import { defineStore } from 'pinia'
import { ref } from 'vue'
import { api, ApiError, qs } from '@/lib/api'
import type { Explain } from '@/lib/types'

/** Стан ExplainView: одне рішення і його повне виведення. */
export const useDecision = defineStore('decision', () => {
  const explain = ref<Explain | null>(null)
  const decisionId = ref<number | null>(null)
  const pending = ref(false)
  const error = ref<string | null>(null)
  const notFound = ref(false)

  async function load(id: number, mfPoints = 101): Promise<void> {
    decisionId.value = id
    pending.value = true; error.value = null; notFound.value = false
    try {
      explain.value = await api<Explain>(`/decisions/${id}/explain${qs({ mf_points: mfPoints })}`)
    } catch (e) {
      explain.value = null
      if (e instanceof ApiError && e.status === 404) notFound.value = true
      else error.value = e instanceof ApiError ? e.message : 'Невідома помилка.'
    } finally { pending.value = false }
  }

  function clear(): void { explain.value = null; decisionId.value = null; error.value = null; notFound.value = false }

  return { explain, decisionId, pending, error, notFound, load, clear }
})
