<script setup lang="ts">
import { ref, onMounted } from 'vue'
import type { ViewProps } from '../types'

const props = defineProps<ViewProps>()
const containerEl = ref<HTMLElement>()
const error = ref('')
const rendering = ref(true)

onMounted(async () => {
  if (props.data.type !== 'arrayBuffer' || !containerEl.value) return
  try {
    const docxPreview = await import('docx-preview')
    await docxPreview.renderAsync(props.data.buffer, containerEl.value, undefined, {
      className: 'docx-rendered',
      inWrapper: false,
      ignoreWidth: false,
      ignoreHeight: false,
      ignoreFonts: false,
      breakPages: true,
      experimental: false,
    })
  } catch (e: any) {
    error.value = e?.message || '解析失败'
  } finally {
    rendering.value = false
  }
})
</script>

<template>
  <div class="dv-wrap">
    <div v-if="rendering" class="dv-loading">渲染中...</div>
    <div v-else-if="error" class="dv-error">
      <div class="dv-kaomoji">(´；ω；`)</div>
      <div>{{ error }}</div>
    </div>
    <div ref="containerEl" class="dv-container" />
  </div>
</template>

<style scoped>
.dv-wrap {
  flex: 1; display: flex; flex-direction: column; overflow: auto;
  background: #F1F2F3; padding: 24px;
}
.dv-loading {
  text-align: center; color: #9499A0; font-size: 13px; padding: 40px;
}
.dv-error {
  text-align: center; color: #9499A0; font-size: 14px; padding: 40px;
}
.dv-kaomoji { font-size: 36px; color: #00AEEC; margin-bottom: 12px; }
.dv-container { display: flex; flex-direction: column; align-items: center; gap: 16px; }
</style>

<style>
/* docx-preview 输出的页面节点 */
.docx-rendered section.docx {
  background: #fff;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08);
  border-radius: 4px;
  padding: 32px 40px !important;
  margin: 0 auto;
}
</style>
