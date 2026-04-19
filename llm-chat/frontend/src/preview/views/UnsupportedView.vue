<script setup lang="ts">
defineProps<{ hint?: string }>()
</script>

<template>
  <div class="uv-empty">
    <div class="uv-kaomoji">(｡•́︿•̀｡)</div>
    <div class="uv-text">不支持在线预览</div>
    <div v-if="hint" class="uv-sub">{{ hint }}</div>
  </div>
</template>

<style scoped>
.uv-empty {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 12px;
  background: #F8F9FA;
}
.uv-kaomoji {
  font-size: 36px; color: #00AEEC; font-weight: 500;
  letter-spacing: 1px; user-select: none;
  animation: uv-bob 2.4s ease-in-out infinite;
}
@keyframes uv-bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-4px); } }
.uv-text { font-size: 15px; color: #61666D; font-weight: 500; }
.uv-sub { font-size: 12.5px; color: #9499A0; }
</style>
