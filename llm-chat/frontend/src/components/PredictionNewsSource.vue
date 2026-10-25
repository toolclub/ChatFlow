<script setup lang="ts">
import { ref, watch, onBeforeUnmount } from 'vue'
import { fetchPredictionSource } from '../api'
import { useAdmin } from '../composables/useAdmin'
import type { NewsItem } from '../types'

const props = defineProps<{ predictionId: string; dimension: string; source: NewsItem; index: number }>()
const admin = useAdmin()
const opened = ref(false)
const loading = ref(false)
const body = ref<string | null>(null)
const error = ref('')
let request: AbortController | null = null

async function loadBody() {
  if (loading.value || body.value !== null) return
  error.value = ''
  if (props.source.full_content !== undefined) {
    body.value = props.source.full_content || props.source.snippet || '此来源未提供正文。'
    return
  }
  const controller = new AbortController()
  request = controller
  loading.value = true
  try {
    const item = await fetchPredictionSource(
      props.predictionId, props.dimension, props.source.source_index ?? props.index,
      props.source.url || '', admin.verifiedAdminKey.value, controller.signal,
    )
    if (!controller.signal.aborted) body.value = item.full_content || item.snippet || '此来源未提供正文。'
  } catch (e: unknown) {
    if (!controller.signal.aborted) error.value = e instanceof Error ? e.message : '读取失败，请重试'
  } finally {
    if (request === controller) { loading.value = false; request = null }
  }
}
function toggle(event: Event) {
  opened.value = (event.target as HTMLDetailsElement).open
  if (opened.value) void loadBody()
}
watch(() => [props.predictionId, props.dimension, props.source.url, props.source.source_index], () => {
  request?.abort()
  request = null
  loading.value = false
  body.value = null
  error.value = ''
  if (opened.value) void loadBody()
})
onBeforeUnmount(() => request?.abort())
</script>

<template>
  <details class="prediction-source" @toggle="toggle">
    <summary>{{ source.title || '未命名来源' }} <span>查看原文</span></summary>
    <div v-if="opened" class="source-detail">
      <a v-if="source.url" :href="source.url" target="_blank" rel="noopener noreferrer">打开来源网站 ↗</a>
      <p v-if="loading" role="status">正在读取完整原文…</p>
      <p v-else-if="error" role="alert">{{ error }} <button type="button" @click="loadBody">重试</button></p>
      <pre v-else>{{ body }}</pre>
    </div>
  </details>
</template>

<style scoped>
.prediction-source { margin: 8px 0; font-size: 13px; }
summary { cursor: pointer; line-height: 1.6; color: var(--el-color-primary); }
summary span { font-size: 11px; color: var(--el-text-color-secondary); margin-left: 6px; white-space: nowrap; }
.source-detail { padding: 8px 0; }
.source-detail pre { white-space: pre-wrap; overflow-wrap: anywhere; font: inherit; line-height: 1.7; max-height: 480px; overflow-y: auto; }
</style>
