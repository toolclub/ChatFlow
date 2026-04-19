<script setup lang="ts">
import { computed } from 'vue'
import hljs from 'highlight.js/lib/common'
import type { ViewProps } from '../types'
import UnsupportedView from './UnsupportedView.vue'

const props = defineProps<ViewProps>()
const text = props.data.type === 'text' ? props.data.text : ''

const TEXT_RENDER_LIMIT = 500 * 1024
const truncated = text.length > TEXT_RENDER_LIMIT
const safeText = truncated ? text.slice(0, TEXT_RENDER_LIMIT) : text

/** 兜底嗅探：扩展名归为代码但内容其实是二进制（如 .txt 里塞了 binary）→ 转 unsupported */
const isBinary = computed(() => {
  const sample = safeText.slice(0, 4096)
  if (!sample) return false
  let nonPrintable = 0
  for (let i = 0; i < sample.length; i++) {
    const c = sample.charCodeAt(i)
    if (c === 0) return true
    if (c < 9 || (c > 13 && c < 32) || c === 127) nonPrintable++
  }
  return nonPrintable / sample.length > 0.1
})

function pickLang(filename: string): string {
  const ext = filename.lastIndexOf('.') >= 0 ? filename.slice(filename.lastIndexOf('.') + 1).toLowerCase() : ''
  const aliases: Record<string, string> = {
    htm: 'html', mjs: 'javascript', cjs: 'javascript',
    yml: 'yaml', tsx: 'typescript', jsx: 'javascript',
    sh: 'bash', zsh: 'bash', fish: 'bash',
    h: 'cpp', hpp: 'cpp', cc: 'cpp', cxx: 'cpp',
    pl: 'perl', pm: 'perl',
    dockerfile: 'dockerfile', makefile: 'makefile',
  }
  return aliases[ext] || ext
}

const highlightedHtml = computed(() => {
  if (!safeText) return ''
  const lang = pickLang(props.file.name)
  try {
    if (lang && hljs.getLanguage(lang)) {
      return hljs.highlight(safeText, { language: lang, ignoreIllegals: true }).value
    }
    return hljs.highlightAuto(safeText).value
  } catch {
    return safeText.replace(/[&<>]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string))
  }
})
</script>

<template>
  <UnsupportedView v-if="isBinary" hint="检测到二进制内容" />
  <div v-else class="cv-wrap">
    <div v-if="truncated" class="cv-tip">
      文件较大，仅渲染前 {{ Math.floor(TEXT_RENDER_LIMIT / 1024) }} KB
    </div>
    <pre class="cv-code"><code class="hljs" v-html="highlightedHtml"></code></pre>
  </div>
</template>

<style scoped>
.cv-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden; background: #fff; }
.cv-tip {
  padding: 6px 14px; font-size: 12px; color: #FB7299;
  background: #FFF4F8; border-bottom: 1px solid #FFE0EC;
}
.cv-code {
  flex: 1; margin: 0; padding: 16px; overflow: auto;
  background: #FAFBFC; font-size: 12.5px; line-height: 1.6;
  font-family: 'Consolas', 'Monaco', 'Courier New', monospace;
}
.cv-code code { background: transparent; }
</style>
