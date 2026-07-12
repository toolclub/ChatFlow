<script setup lang="ts">
/**
 * 预测免责声明横幅 —— 醒目红色，复用于预测记录列表顶部 + 交互页顶部。
 * 不含 props，纯静态。
 */
</script>

<template>
  <div class="prediction-disclaimer">
    <span class="disclaimer-icon">⚠️</span>
    <span class="disclaimer-text">
      预测结果仅供参考，不构成投资建议。基于公开行情与资讯的统计建模，存在重大不确定性，实际走势可能大幅偏离。投资有风险，决策需独立判断。
    </span>
  </div>
</template>

<style scoped>
.prediction-disclaimer {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 10px 14px;
  border-radius: 10px;
  background: rgba(242, 93, 89, 0.08);
  border: 1px solid rgba(242, 93, 89, 0.35);
  color: #c0392b;
  font-size: 12.5px;
  line-height: 1.6;
  font-weight: 600;
}
.disclaimer-icon {
  font-size: 16px;
  flex-shrink: 0;
  line-height: 1.4;
}
.disclaimer-text {
  flex: 1;
}
body.dark .prediction-disclaimer {
  background: rgba(244, 114, 109, 0.1);
  border-color: rgba(244, 114, 109, 0.4);
  color: #f5a8a4;
}
</style>
