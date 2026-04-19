<script setup lang="ts">
import type { ViewProps } from '../types'
const props = defineProps<ViewProps>()
const url = props.data.type === 'blobUrl' ? props.data.url : ''
</script>

<template>
  <iframe :src="url" class="pdf-frame" />
</template>

<style scoped>
.pdf-frame { flex: 1; width: 100%; border: none; background: #fff; }
</style>
