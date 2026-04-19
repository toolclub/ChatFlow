import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const pdfRenderer: Renderer = {
  id: 'pdf',
  label: 'PDF',
  extensions: ['pdf'],
  source: 'blobUrl',
  component: defineAsyncComponent(() => import('../views/PdfView.vue')),
}
