<script setup lang="ts">
import type { ViewProps } from '../types'
const props = defineProps<ViewProps>()
const url = props.data.type === 'blobUrl' ? props.data.url : ''
</script>

<template>
  <div class="iv-wrap">
    <el-image :src="url" fit="contain" :preview-src-list="[url]" class="iv-img" />
  </div>
</template>

<style scoped>
.iv-wrap {
  flex: 1; display: flex; align-items: center; justify-content: center;
  background: #2B2D31; padding: 20px;
}
.iv-img { max-width: 100%; max-height: 100%; }
</style>
