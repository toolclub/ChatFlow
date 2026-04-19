<script setup lang="ts">
import type { ViewProps } from '../types'
const props = defineProps<ViewProps>()
const url = props.data.type === 'blobUrl' ? props.data.url : ''
</script>

<template>
  <div class="av-wrap">
    <div class="av-icon">🎵</div>
    <div class="av-name">{{ file.name }}</div>
    <audio :src="url" controls class="av-audio" />
  </div>
</template>

<style scoped>
.av-wrap {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 16px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FFF4F8 100%);
}
.av-icon { font-size: 64px; }
.av-name {
  font-size: 14px; color: #61666D;
  max-width: 70%; text-align: center;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.av-audio { width: 80%; max-width: 480px; }
</style>
