<script setup lang="ts">
</script>

<template>
  <div class="lv-wrap">
    <svg class="lv-svg" width="80" height="64" viewBox="0 0 80 64">
      <rect x="12" y="2" width="56" height="38" rx="4" fill="#E3F6FD" stroke="#00AEEC" stroke-width="2"/>
      <circle cx="32" cy="18" r="3" fill="#00AEEC"><animate attributeName="r" values="3;2;3" dur="1.5s" repeatCount="indefinite"/></circle>
      <circle cx="48" cy="18" r="3" fill="#00AEEC"><animate attributeName="r" values="3;2;3" dur="1.5s" begin="0.1s" repeatCount="indefinite"/></circle>
      <path d="M35 27 Q40 32 45 27" fill="none" stroke="#00AEEC" stroke-width="1.5" stroke-linecap="round"/>
      <rect x="28" y="40" width="24" height="4" rx="1" fill="#00AEEC"/>
      <rect x="22" y="44" width="36" height="3" rx="1.5" fill="#00AEEC"/>
    </svg>
    <div class="lv-text">正在加载预览<span class="lv-dot-anim"></span></div>
  </div>
</template>

<style scoped>
.lv-wrap {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 14px;
  background: #F8F9FA;
}
.lv-svg { animation: lv-bob 1.4s ease-in-out infinite; }
@keyframes lv-bob { 0%, 100% { transform: translateY(0); } 50% { transform: translateY(-4px); } }
.lv-text { font-size: 13px; color: #00AEEC; font-weight: 500; }
.lv-dot-anim::after {
  content: '...'; display: inline-block; width: 18px; text-align: left;
  animation: lv-dots 1.2s steps(4) infinite;
}
@keyframes lv-dots { 0% { content: ''; } 33% { content: '.'; } 66% { content: '..'; } 100% { content: '...'; } }
</style>
