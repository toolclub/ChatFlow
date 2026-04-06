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
    color: '#0891b2', bg: 'rgba(8,145,178,0.08)', pulse: '#22d3ee',
  },
  routing:    {
    label: '分析意图',
    desc:  '识别问题类型，匹配最优策略',
    color: '#6366f1', bg: 'rgba(99,102,241,0.08)', pulse: '#818cf8',
  },
  planning:   {
    label: '制定计划',
    desc:  '分解任务，规划执行步骤',
    color: '#7c3aed', bg: 'rgba(124,58,237,0.08)', pulse: '#a78bfa',
  },
  tool:       {
    label: '执行工具',
    desc:  s => `正在调用 ${s.tool || '工具'}`,
    color: '#d97706', bg: 'rgba(217,119,6,0.08)',  pulse: '#fbbf24',
  },
  thinking:   {
    label: '推理生成',
    desc:  '正在思考...',
    color: '#2563eb', bg: 'rgba(37,99,235,0.08)',  pulse: '#60a5fa',
  },
  reflecting: {
    label: '反思评估',
    desc:  '评估执行结果，决定下一步',
    color: '#059669', bg: 'rgba(5,150,105,0.08)',  pulse: '#34d399',
  },
  saving:     {
    label: '保存记录',
    desc:  '整理并保存本次对话',
    color: '#6b7280', bg: 'rgba(107,114,128,0.08)', pulse: '#9ca3af',
  },
}

const cfg = computed(() => PHASE[props.status.state] ?? null)

const desc = computed(() => {
  if (!cfg.value) return ''
  const d = cfg.value.desc
  return typeof d === 'function' ? d(props.status) : d
})

const hasPlan = computed(() => props.cognitive.plan.length > 0)
const planSteps = computed(() => props.cognitive.plan)
</script>

<template>
  <transition name="bubble-fade" appear>
    <div v-if="cfg" class="status-bubble">

      <!-- 左侧彩色条 -->
      <div class="accent-bar" :style="{ background: cfg.color }" />

      <div class="bubble-body">

        <!-- 阶段行 -->
        <div class="phase-row">
          <span class="pulse-dot" :style="{ background: cfg.pulse }" />
          <span class="phase-label" :style="{ color: cfg.color }">{{ cfg.label }}</span>
          <span class="phase-dot-sep">·</span>
          <span class="phase-desc">{{ desc }}</span>
          <el-icon class="phase-spin" :style="{ color: cfg.color }"><Loading /></el-icon>
        </div>

        <!-- 计划步骤（有计划时显示） -->
        <div v-if="hasPlan" class="plan-list">
          <div
            v-for="(step, i) in planSteps"
            :key="step.id"
            class="plan-item"
            :class="step.status"
          >
            <!-- 连接线 -->
            <div class="step-line-wrap">
              <div class="step-circle" :class="step.status">
                <el-icon v-if="step.status === 'done'" class="step-icon-done"><Check /></el-icon>
                <el-icon v-else-if="step.status === 'running'" class="step-icon-spin"><Loading /></el-icon>
                <el-icon v-else-if="step.status === 'failed'" class="step-icon-fail"><Close /></el-icon>
                <span v-else class="step-num">{{ i + 1 }}</span>
              </div>
              <div v-if="i < planSteps.length - 1" class="step-connector" :class="{ passed: step.status === 'done' }" />
            </div>

            <!-- 步骤内容 -->
            <div class="step-content">
              <span class="step-title" :class="step.status">{{ step.title }}</span>
              <span v-if="step.description && step.status === 'running'" class="step-desc">
                {{ step.description }}
              </span>
            </div>
          </div>
        </div>

      </div>
    </div>
  </transition>
</template>

<style scoped>
/* ── 入场动画 ── */
.bubble-fade-enter-active { transition: opacity .25s, transform .25s; }
.bubble-fade-leave-active { transition: opacity .15s; }
.bubble-fade-enter-from  { opacity: 0; transform: translateY(6px); }
.bubble-fade-leave-to    { opacity: 0; }

/* ── 外层卡片 ── */
.status-bubble {
  display: flex;
  align-items: stretch;
  background: var(--cf-bg, #F5F7FA);
  border: 1px solid var(--cf-border, #DCDFE6);
  border-radius: 12px;
  box-shadow: 0 2px 10px rgba(0,0,0,0.05);
  overflow: hidden;
  margin: 2px 0 6px;
  max-width: 520px;
  width: fit-content;
}

.accent-bar {
  width: 3px;
  flex-shrink: 0;
  border-radius: 0;
  transition: background .3s;
}

.bubble-body {
  flex: 1;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  min-width: 0;
}

/* ── 阶段行 ── */
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
  0%, 100% { opacity: 1;   transform: scale(1); }
  50%       { opacity: .5; transform: scale(.85); }
}

.phase-label {
  font-size: 13px;
  font-weight: 700;
  letter-spacing: .2px;
  transition: color .3s;
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

/* ── 计划步骤列表 ── */
.plan-list {
  display: flex;
  flex-direction: column;
  padding-left: 2px;
}

.plan-item {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

/* 左侧圆圈 + 连接线 */
.step-line-wrap {
  display: flex;
  flex-direction: column;
  align-items: center;
  flex-shrink: 0;
  width: 22px;
}

.step-circle {
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 700;
  flex-shrink: 0;
  transition: all .25s;
}
.step-circle.pending  { background: #f3f4f6; color: #9ca3af; border: 1.5px solid #e5e7eb; }
.step-circle.running  { background: #dbeafe; color: #2563eb; border: 1.5px solid #93c5fd; }
.step-circle.done     { background: #d1fae5; color: #059669; border: 1.5px solid #6ee7b7; }
.step-circle.failed   { background: #fee2e2; color: #dc2626; border: 1.5px solid #fca5a5; }

.step-icon-done { font-size: 11px; }
.step-icon-spin { font-size: 11px; animation: spin 1.1s linear infinite; }
.step-icon-fail { font-size: 11px; }
.step-num       { font-size: 10px; line-height: 1; }

.step-connector {
  width: 1.5px;
  flex: 1;
  min-height: 8px;
  background: #e5e7eb;
  margin: 2px 0;
  transition: background .3s;
}
.step-connector.passed { background: #6ee7b7; }

/* 步骤文字 */
.step-content {
  display: flex;
  flex-direction: column;
  padding: 2px 0 8px;
  min-width: 0;
}

.step-title {
  font-size: 12.5px;
  font-weight: 500;
  color: #374151;
  line-height: 1.4;
  transition: color .2s;
}
.step-title.done    { color: #9ca3af; text-decoration: line-through; }
.step-title.running { color: #1d4ed8; font-weight: 600; }
.step-title.failed  { color: #dc2626; }

.step-desc {
  font-size: 11px;
  color: #9ca3af;
  margin-top: 2px;
  line-height: 1.4;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
</style>
