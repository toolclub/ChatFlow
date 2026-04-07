<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { buildIframeSrcdoc } from '../utils/codePreviewBuilders'

const props = defineProps<{
  modelValue: boolean
  code: string
  lang: string
}>()
const emit = defineEmits<{ 'update:modelValue': [boolean] }>()

const visible = computed({
  get: () => props.modelValue,
  set: v => emit('update:modelValue', v)
})

const loading = ref(false)
watch(() => props.modelValue, v => { if (v) loading.value = true })

const LANG_LABELS: Record<string, string> = {
  html: 'HTML', svg: 'SVG', css: 'CSS',
  javascript: 'JavaScript', js: 'JavaScript',
  typescript: 'TypeScript', ts: 'TypeScript',
  vue: 'Vue',
  jsx: 'React JSX', tsx: 'React TSX', react: 'React',
}

const title = computed(() => {
  const label = LANG_LABELS[props.lang] || props.lang.toUpperCase()
  return `${label} 预览`
})

const needsCDN = computed(() =>
  ['typescript', 'ts', 'vue', 'jsx', 'tsx', 'react'].includes(props.lang)
)

const srcdoc = computed(() => buildIframeSrcdoc(props.code, props.lang))
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="title"
    width="92%"
    top="3vh"
    destroy-on-close
    class="preview-dialog"
  >
    <template #header>
      <div class="preview-dialog-header">
        <span class="preview-lang-badge">{{ props.lang }}</span>
        <span class="preview-dialog-title">{{ title }}</span>
        <span v-if="needsCDN" class="preview-cdn-note">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none" style="flex-shrink:0">
            <circle cx="8" cy="8" r="6.5" stroke="#00AEEC" stroke-width="1.4"/>
            <path d="M8 5v3.5L10 10" stroke="#00AEEC" stroke-width="1.4" stroke-linecap="round"/>
          </svg>
          需要联网加载运行时
        </span>
      </div>
    </template>

    <div class="preview-body">
      <div v-if="loading" class="preview-loading">
        <svg class="spin-icon" width="28" height="28" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="#e4e6ef" stroke-width="2.5"/>
          <path d="M12 2a10 10 0 0 1 10 10" stroke="#00AEEC" stroke-width="2.5" stroke-linecap="round"/>
        </svg>
        <span>渲染中…</span>
      </div>
      <iframe
        :srcdoc="srcdoc"
        class="preview-frame"
        :class="{ hidden: loading }"
        sandbox="allow-scripts allow-forms allow-modals allow-popups"
        @load="loading = false"
      />
    </div>
  </el-dialog>
</template>

<style scoped>
.preview-dialog-header {
  display: flex;
  align-items: center;
  gap: 10px;
}
.preview-lang-badge {
  padding: 2px 8px;
  background: #E3F6FD;
  color: #0095CC;
  border-radius: 5px;
  font-size: 11.5px;
  font-weight: 700;
  font-family: 'Fira Code', Consolas, monospace;
  border: 1px solid #D0EEF9;
}
.preview-dialog-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}
.preview-cdn-note {
  margin-left: auto;
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  color: #00AEEC;
  background: #E3F6FD;
  padding: 3px 10px;
  border-radius: 20px;
  border: 1px solid #D0EEF9;
}

.preview-body {
  position: relative;
  height: 76vh;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #e4e6ef;
  background: #fff;
}
.preview-frame {
  width: 100%;
  height: 100%;
  border: none;
  display: block;
  background: #fff;
  transition: opacity 0.25s;
}
.preview-frame.hidden { opacity: 0; }

.preview-loading {
  position: absolute;
  inset: 0;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 12px;
  background: #fff;
  color: #6b7280;
  font-size: 13px;
  z-index: 1;
}

@keyframes spin { to { transform: rotate(360deg); } }
.spin-icon { animation: spin 0.9s linear infinite; }
</style>

<style>
.preview-dialog .el-dialog__body  { padding: 14px !important; }
.preview-dialog .el-dialog__header {
  padding: 14px 20px 12px !important;
  border-bottom: 1px solid #e4e6ef;
  margin-bottom: 0 !important;
}
</style>
