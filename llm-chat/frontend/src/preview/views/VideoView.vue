<script setup lang="ts">
import type { ViewProps } from '../types'
const props = defineProps<ViewProps>()
const url = props.data.type === 'blobUrl' ? props.data.url : ''
</script>

<template>
  <div class="vv-wrap">
    <video :src="url" controls class="vv-video" />
  </div>
</template>

<style scoped>
.vv-wrap {
  flex: 1; display: flex; align-items: center; justify-content: center;
  background: #000; padding: 20px;
}
.vv-video { max-width: 100%; max-height: 100%; outline: none; }
</style>
