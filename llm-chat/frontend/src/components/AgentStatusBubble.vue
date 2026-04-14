<script setup lang="ts">
import { computed } from 'vue'
import { Check, Loading, Close } from '@element-plus/icons-vue'
import type { AgentStatus, CognitiveState } from '../types'

const props = defineProps<{
  status: AgentStatus
  cognitive: CognitiveState
}>()

interface PhaseConfig {
  label: string
  desc: string | ((s: AgentStatus) => string)
  color: string
  bg: string
  pulse: string
}

const PHASE: Record<string, PhaseConfig> = {
  vision_analyze: {
    label: '图像解析',
    desc:  '正在解析图像内容，理解视觉信息...',
    color: '#00AEEC', bg: 'rgba(0,174,236,0.06)', pulse: '#33C1F0',
  },
  routing:    {
    label: '分析意图',
    desc:  '识别问题类型，匹配最优策略',
    color: '#00AEEC', bg: 'rgba(0,174,236,0.06)', pulse: '#66D3F5',
  },
  planning:   {
    label: '制定计划',
    desc:  '分解任务，规划执行步骤',
    color: '#FB7299', bg: 'rgba(251,114,153,0.06)', pulse: '#FCA0B8',
  },
  tool:       {
    label: '执行工具',
    desc:  s => `正在调用 ${s.tool || '工具'}`,
    color: '#FF9736', bg: 'rgba(255,151,54,0.06)',  pulse: '#FFBC73',
  },
  thinking:   {
    label: '推理生成',
    desc:  '模型正在回答...',
    color: '#00AEEC', bg: 'rgba(0,174,236,0.06)',  pulse: '#33C1F0',
  },
  reflecting: {
    label: '反思评估',
    desc:  '评估执行结果，决定下一步',
    color: '#00B578', bg: 'rgba(0,181,120,0.06)',  pulse: '#3DDC84',
  },
  saving:     {
    label: '保存记录',
    desc:  '整理并保存本次对话',
    color: '#9499A0', bg: 'rgba(148,153,160,0.06)', pulse: '#C9CCD0',
  },
}

const cfg = computed(() => PHASE[props.status.state] ?? null)

const desc = computed(() => {
  if (!cfg.value) return ''
  // thinking 状态 + 无执行计划 → 直达回答模式
  if (props.status.state === 'thinking' && props.cognitive.plan.length === 0) {
    return '模型正在回答，请稍候...'
  }
  const d = cfg.value.desc
  return typeof d === 'function' ? d(props.status) : d
})

const hasPlan = computed(() => props.cognitive.plan.length > 0)
const planSteps = computed(() => props.cognitive.plan)
</script>

<template>
  <transition name="bubble-slide" appear>
    <div v-if="cfg" class="status-bubble-wrap">
      <el-card class="status-bubble-card" shadow="hover" :body-style="{ padding: 0 }">
        <!-- 左侧彩色条 -->
        <div class="accent-bar" :style="{ background: cfg.color }" />

        <div class="bubble-body">
          <!-- 阶段行 -->
          <div class="phase-row">
            <span class="pulse-dot" :style="{ background: cfg.pulse }" />
            <el-tag
              :color="cfg.bg"
              :style="{ color: cfg.color, borderColor: cfg.color + '30' }"
              size="small"
              effect="plain"
              class="phase-tag"
            >
              {{ cfg.label }}
            </el-tag>
            <span class="phase-dot-sep">·</span>
            <span class="phase-desc">{{ desc }}</span>
            <el-icon class="phase-spin" :style="{ color: cfg.color }"><Loading /></el-icon>
          </div>

          <!-- 计划步骤（有计划时显示） -->
          <div v-if="hasPlan" class="plan-steps-wrap">
            <el-steps direction="vertical" :active="-1" class="bili-steps">
              <el-step
                v-for="(step, i) in planSteps"
                :key="step.id"
                :title="step.title"
                :description="step.status === 'running' ? step.description : ''"
                :status="
                  step.status === 'done' ? 'finish' :
                  step.status === 'running' ? 'process' :
                  step.status === 'failed' ? 'error' :
                  'wait'
                "
                :class="['bili-step', `bili-step--${step.status}`]"
              >
                <template #icon>
                  <div class="step-icon-circle" :class="step.status">
                    <el-icon v-if="step.status === 'done'" class="step-icon-done"><Check /></el-icon>
                    <el-icon v-else-if="step.status === 'running'" class="step-icon-spin"><Loading /></el-icon>
                    <el-icon v-else-if="step.status === 'failed'" class="step-icon-fail"><Close /></el-icon>
                    <span v-else class="step-num">{{ i + 1 }}</span>
                  </div>
                </template>
              </el-step>
            </el-steps>
          </div>
        </div>
      </el-card>
    </div>
  </transition>
</template>

<style scoped>
/* -- Slide-in from right + fade -- */
.bubble-slide-enter-active {
  transition: opacity 0.35s cubic-bezier(0.34,1.56,0.64,1),
              transform 0.35s cubic-bezier(0.34,1.56,0.64,1);
}
.bubble-slide-leave-active {
  transition: opacity 0.2s ease, transform 0.2s ease;
}
.bubble-slide-enter-from {
  opacity: 0;
  transform: translateX(20px);
}
.bubble-slide-leave-to {
  opacity: 0;
  transform: translateX(10px);
}

/* -- Outer wrap -- */
.status-bubble-wrap {
  margin: 2px 0 6px;
  max-width: 520px;
  width: fit-content;
}

/* -- Card override -- */
.status-bubble-card {
  border-radius: 14px !important;
  border: 1px solid var(--cf-border-soft, #EBF0F5) !important;
  overflow: hidden;
  transition: box-shadow 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.status-bubble-card .el-card__body) {
  display: flex;
  align-items: stretch;
  padding: 0 !important;
}

.accent-bar {
  width: 3px;
  flex-shrink: 0;
  transition: background 0.3s cubic-bezier(0.34,1.56,0.64,1);
}

.bubble-body {
  flex: 1;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

/* -- Phase row -- */
.phase-row {
  display: flex;
  align-items: center;
  gap: 7px;
  flex-wrap: wrap;
}

.pulse-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  flex-shrink: 0;
  animation: pulse-ring 1.8s ease-in-out infinite;
}
@keyframes pulse-ring {
  0%, 100% { opacity: 1;   transform: scale(1); box-shadow: 0 0 0 0 currentColor; }
  50%       { opacity: .5; transform: scale(.85); box-shadow: 0 0 0 4px transparent; }
}

.phase-tag {
  font-size: 12px !important;
  font-weight: 700 !important;
  letter-spacing: 0.3px;
  height: 22px !important;
  line-height: 20px !important;
  padding: 0 8px !important;
  border-radius: 11px !important;
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}

.phase-dot-sep {
  color: #d1d5db;
  font-size: 12px;
}
.phase-desc {
  font-size: 12px;
  color: #6b7280;
  flex: 1;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.phase-spin {
  font-size: 13px;
  animation: spin 1.1s linear infinite;
  flex-shrink: 0;
}
@keyframes spin { to { transform: rotate(360deg); } }

/* -- Plan steps with el-steps -- */
.plan-steps-wrap {
  padding-left: 2px;
}

.bili-steps {
  --el-color-primary: #00AEEC;
  --el-color-success: #00B578;
  --el-color-danger: #F25D59;
}

:deep(.bili-steps .el-step__head) {
  padding-right: 8px;
}

:deep(.bili-steps .el-step__icon) {
  width: 22px;
  height: 22px;
  font-size: 10px;
  border: none !important;
  background: transparent !important;
}

:deep(.bili-steps .el-step__line) {
  top: 24px;
  left: 10px;
  width: 1.5px !important;
  background: #e5e7eb;
  transition: background 0.3s cubic-bezier(0.34,1.56,0.64,1);
}

:deep(.bili-steps .el-step.is-vertical:not(:last-of-type) .el-step__line) {
  min-height: 8px;
}

/* Override finished step line color */
:deep(.bili-step--done + .bili-step .el-step__line),
:deep(.bili-step--done .el-step__line) {
  background: #8AE0C0 !important;
}

:deep(.bili-steps .el-step__title) {
  font-size: 12.5px;
  font-weight: 500;
  color: #374151;
  line-height: 1.4;
  transition: color 0.25s cubic-bezier(0.34,1.56,0.64,1);
  padding-bottom: 4px;
}

:deep(.bili-step--done .el-step__title) {
  color: #9499A0 !important;
  text-decoration: line-through;
}
:deep(.bili-step--running .el-step__title) {
  color: #00AEEC !important;
  font-weight: 600;
}
:deep(.bili-step--failed .el-step__title) {
  color: #F25D59 !important;
}

:deep(.bili-steps .el-step__description) {
  font-size: 11px;
  color: #9ca3af;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  padding-bottom: 6px;
}

/* -- Custom step icon circles -- */
.step-icon-circle {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  flex-shrink: 0;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
.step-icon-circle.pending  { background: #F1F2F3; color: #9499A0; border: 1.5px solid #E3E5E7; }
.step-icon-circle.running  {
  background: #E3F6FD; color: #00AEEC; border: 1.5px solid #B8E6F9;
  animation: step-pulse 2s ease-in-out infinite;
}
.step-icon-circle.done     { background: #D5F5E8; color: #00B578; border: 1.5px solid #8AE0C0; }
.step-icon-circle.failed   { background: #FDE8E7; color: #F25D59; border: 1.5px solid #F9ADAB; }

/* Breathing pulse for running steps */
@keyframes step-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(0,174,236,0.25); }
  50%      { box-shadow: 0 0 0 5px rgba(0,174,236,0); }
}

.step-icon-done { font-size: 11px; }
.step-icon-spin { font-size: 11px; animation: spin 1.1s linear infinite; }
.step-icon-fail { font-size: 11px; }
.step-num       { font-size: 10px; line-height: 1; }
</style>
