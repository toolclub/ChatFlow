import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const audioRenderer: Renderer = {
  id: 'audio',
  label: '音频',
  extensions: ['mp3', 'wav', 'ogg', 'flac', 'm4a', 'aac', 'opus'],
  source: 'blobUrl',
  component: defineAsyncComponent(() => import('../views/AudioView.vue')),
}
