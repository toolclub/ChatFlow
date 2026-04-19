import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const markdownRenderer: Renderer = {
  id: 'markdown',
  label: 'Markdown',
  extensions: ['md', 'markdown'],
  source: 'text',
  component: defineAsyncComponent(() => import('../views/MarkdownView.vue')),
}
