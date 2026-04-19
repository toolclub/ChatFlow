import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const spreadsheetRenderer: Renderer = {
  id: 'spreadsheet',
  label: '电子表格',
  extensions: ['csv', 'tsv', 'xlsx', 'xls'],
  source: 'arrayBuffer',
  component: defineAsyncComponent(() => import('../views/SpreadsheetView.vue')),
}
