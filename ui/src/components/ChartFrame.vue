<script setup lang="ts">
/** Оболонка ECharts: життєвий цикл, ресайз, порожній стан і доступний підпис.
    Усі графіки панелі монтуються через неї, тож поведінка всюди однакова. */
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import { echarts, type EChartsOption } from '@/lib/chart'

const props = withDefaults(defineProps<{
  option: EChartsOption | null
  height?: number
  /** Текстовий опис графіка для зчитувача екрана і для читача звіту. */
  caption?: string
  empty?: boolean
  emptyText?: string
}>(), { height: 260, empty: false })

const host = ref<HTMLDivElement | null>(null)
const chart = shallowRef<echarts.ECharts | null>(null)
let ro: ResizeObserver | null = null

function render(): void {
  const el = host.value
  if (el === null || props.option === null || props.empty) return
  // Вузол щойно з'явився в DOM і ще не має розмірів — ECharts намалював би нульове полотно.
  if (el.clientWidth === 0 || el.clientHeight === 0) return
  if (chart.value === null) chart.value = echarts.init(el, undefined, { renderer: 'canvas' })
  chart.value.setOption(props.option, { notMerge: true })
}

/** Спостерігач стежить і за розміром: перший ненульовий розмір — момент першого малювання. */
function attachObserver(): void {
  if (ro !== null || host.value === null) return
  ro = new ResizeObserver(() => {
    if (chart.value === null) render()
    else chart.value.resize()
  })
  ro.observe(host.value)
}

onMounted(() => { render(); attachObserver() })

// flush: 'post' — інакше спостерігач спрацював би до того, як v-if створить вузол.
watch(() => [props.option, props.empty], () => {
  if (props.empty) { ro?.disconnect(); ro = null; chart.value?.dispose(); chart.value = null; return }
  render()
  attachObserver()
}, { deep: false, flush: 'post' })

onBeforeUnmount(() => { ro?.disconnect(); chart.value?.dispose(); chart.value = null })
</script>

<template>
  <figure class="frame">
    <div
      v-if="!empty"
      ref="host"
      class="canvas"
      :style="{ height: `${height}px` }"
      role="img"
      :aria-label="caption"
    />
    <p v-else class="empty" :style="{ height: `${height}px` }">
      {{ emptyText ?? 'Немає даних для побудови' }}
    </p>
    <figcaption v-if="caption" class="cap">{{ caption }}</figcaption>
  </figure>
</template>

<style scoped>
.frame { margin: 0; }
.canvas { width: 100%; }
.empty {
  display: flex; align-items: center; justify-content: center;
  border: 1px dashed var(--rule-strong); border-radius: var(--radius);
  background: var(--paper-sunk); color: var(--ink-faint); font-size: var(--t-small);
}
.cap {
  margin-top: var(--s2); font-size: var(--t-micro); color: var(--ink-faint);
  line-height: 1.45; max-width: 74ch;
}
</style>
