import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const docxRenderer: Renderer = {
  id: 'docx',
  label: 'Word',
  // 仅 .docx；.doc 是完全不同的二进制格式，docx-preview 不支持
  extensions: ['docx'],
  source: 'arrayBuffer',
  component: defineAsyncComponent(() => import('../views/DocxView.vue')),
}
