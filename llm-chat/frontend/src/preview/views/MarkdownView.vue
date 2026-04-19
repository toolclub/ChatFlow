<script setup lang="ts">
import { ref, computed } from 'vue'
import { Marked } from 'marked'
import hljs from 'highlight.js/lib/common'
import type { ViewProps } from '../types'

const props = defineProps<ViewProps>()
const text = props.data.type === 'text' ? props.data.text : ''

const TEXT_RENDER_LIMIT = 500 * 1024
const truncated = text.length > TEXT_RENDER_LIMIT
const safeText = truncated ? text.slice(0, TEXT_RENDER_LIMIT) : text

const mode = ref<'rendered' | 'source'>('rendered')
const marked = new Marked({ gfm: true, breaks: false })

const renderedHtml = computed(() => {
  try { return marked.parse(safeText) as string } catch { return '' }
})
const highlightedSource = computed(() => {
  try { return hljs.highlight(safeText, { language: 'markdown', ignoreIllegals: true }).value }
  catch { return safeText.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string)) }
})
</script>

<template>
  <div class="mv-wrap">
    <div class="mv-toolbar">
      <div class="mv-toggle">
        <button class="mv-btn" :class="{ active: mode === 'rendered' }" @click="mode = 'rendered'">渲染</button>
        <button class="mv-btn" :class="{ active: mode === 'source' }" @click="mode = 'source'">源码</button>
      </div>
      <span v-if="truncated" class="mv-tip">仅显示前 {{ Math.floor(TEXT_RENDER_LIMIT / 1024) }} KB</span>
    </div>
    <div v-if="mode === 'rendered'" class="mv-body" v-html="renderedHtml" />
    <pre v-else class="mv-code"><code class="hljs" v-html="highlightedSource"></code></pre>
  </div>
</template>

<style scoped>
.mv-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: #fff; }
.mv-toolbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 8px 14px; border-bottom: 1px solid #E3E5E7; background: #FAFBFC;
}
.mv-toggle { display: inline-flex; padding: 2px; background: #F1F2F3; border-radius: 6px; }
.mv-btn {
  border: none; background: transparent; cursor: pointer;
  padding: 3px 12px; font-size: 12px; color: #61666D; border-radius: 4px;
  transition: all 0.15s;
}
.mv-btn:hover { color: #00AEEC; }
.mv-btn.active { background: #fff; color: #00AEEC; font-weight: 600; }
.mv-tip { font-size: 12px; color: #FB7299; }

.mv-body {
  flex: 1; padding: 24px 32px; overflow: auto;
  font-size: 14px; line-height: 1.75; color: #18191C;
}
.mv-body :deep(h1),
.mv-body :deep(h2),
.mv-body :deep(h3) { margin: 18px 0 12px; line-height: 1.35; }
.mv-body :deep(h1) { font-size: 24px; border-bottom: 1px solid #E3E5E7; padding-bottom: 8px; }
.mv-body :deep(h2) { font-size: 20px; }
.mv-body :deep(h3) { font-size: 16px; }
.mv-body :deep(p) { margin: 10px 0; }
.mv-body :deep(ul), .mv-body :deep(ol) { padding-left: 24px; margin: 10px 0; }
.mv-body :deep(li) { margin: 4px 0; }
.mv-body :deep(code) {
  padding: 2px 6px; background: #F1F2F3; border-radius: 4px;
  font-size: 13px; font-family: 'Consolas', 'Monaco', monospace;
}
.mv-body :deep(pre) {
  background: #FAFBFC; padding: 12px 14px; border-radius: 6px;
  overflow: auto; margin: 12px 0;
}
.mv-body :deep(pre code) { padding: 0; background: transparent; }
.mv-body :deep(blockquote) {
  border-left: 4px solid #00AEEC; padding: 4px 14px;
  background: #F4FAFD; color: #61666D; margin: 12px 0;
}
.mv-body :deep(table) { border-collapse: collapse; margin: 12px 0; font-size: 13px; }
.mv-body :deep(th), .mv-body :deep(td) { border: 1px solid #E3E5E7; padding: 6px 12px; }
.mv-body :deep(a) { color: #00AEEC; text-decoration: none; }
.mv-body :deep(a:hover) { text-decoration: underline; }
.mv-body :deep(img) { max-width: 100%; height: auto; }

.mv-code {
  flex: 1; margin: 0; padding: 16px; overflow: auto;
  background: #FAFBFC; font-size: 12.5px; line-height: 1.6;
  font-family: 'Consolas', 'Monaco', monospace;
}
.mv-code code { background: transparent; }
</style>
