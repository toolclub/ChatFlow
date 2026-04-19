import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

export const videoRenderer: Renderer = {
  id: 'video',
  label: '视频',
  // 仅纳入浏览器原生能解码的容器；mov/avi/mkv 等走 unsupported
  extensions: ['mp4', 'webm', 'ogv', 'm4v'],
  source: 'blobUrl',
  component: defineAsyncComponent(() => import('../views/VideoView.vue')),
}
