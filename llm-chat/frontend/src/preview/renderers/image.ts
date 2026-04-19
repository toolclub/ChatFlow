import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const imageRenderer: Renderer = {
  id: 'image',
  label: '图片',
  extensions: ['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'svg'],
  source: 'blobUrl',
  component: defineAsyncComponent(() => import('../views/ImageView.vue')),
}
