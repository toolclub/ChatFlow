<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import type { CognitiveState, PlanStep, ToolHistoryEvent } from '../types'
import { Delete } from '@element-plus/icons-vue'
import PlanFlowCanvas from './PlanFlowCanvas.vue'

const props = defineProps<{
  cognitive: CognitiveState
  loading: boolean
  userMessage?: string
}>()

const emit = defineEmits<{
  collapse: []
  modifyPlan: [plan: PlanStep[]]
}>()

// ── Local plan state ──────────────────────────────────────────────────────────
const localPlan = ref<PlanStep[]>([])
const isDirty = ref(false)

watch(
  () => props.cognitive.plan,
  (newPlan) => {
    if (!isDirty.value) {
      localPlan.value = newPlan.map(s => ({ ...s }))
    }
  },
  { deep: true, immediate: true }
)

function resetLocalPlan() {
  localPlan.value = props.cognitive.plan.map(s => ({ ...s }))
  isDirty.value = false
}

// ── Edit dialog ───────────────────────────────────────────────────────────────
const editDialogVisible = ref(false)
const editingIndex = ref(-1)
const editData = ref({ title: '', description: '' })
const insertMode = ref(false)


function saveEdit() {
  if (!editData.value.title.trim()) return
  const updated = [...localPlan.value]
  if (insertMode.value) {
    updated.splice(editingIndex.value + 1, 0, {
      id: `new-${Date.now()}`,
      title: editData.value.title.trim(),
      description: editData.value.description,
      status: 'pending',
      result: '',
    })
  } else {
    updated[editingIndex.value] = {
      ...updated[editingIndex.value],
      title: editData.value.title.trim(),
      description: editData.value.description,
    }
  }
  localPlan.value = updated
  isDirty.value = true
  editDialogVisible.value = false
}

function deleteStep() {
  if (localPlan.value.length <= 1) return
  localPlan.value = localPlan.value.filter((_, i) => i !== editingIndex.value)
  isDirty.value = true
  editDialogVisible.value = false
}

function onReexecute() {
  emit('modifyPlan', localPlan.value)
  isDirty.value = false
}

// ── Resizable trace section ───────────────────────────────────────────────────
const traceHeight = ref(160)
let traceResizing = false
let resizeStartY = 0
let resizeStartH = 0

function onResizeStart(e: MouseEvent) {
  traceResizing = true
  resizeStartY = e.clientY
  resizeStartH = traceHeight.value
  document.body.style.cursor = 'ns-resize'
  document.body.style.userSelect = 'none'
  document.addEventListener('mousemove', onResizeMove)
  document.addEventListener('mouseup', onResizeEnd)
}

function onResizeMove(e: MouseEvent) {
  if (!traceResizing) return
  const delta = resizeStartY - e.clientY
  traceHeight.value = Math.max(120, Math.min(500, resizeStartH + delta))
}

function onResizeEnd() {
  traceResizing = false
  document.body.style.cursor = ''
  document.body.style.userSelect = ''
  document.removeEventListener('mousemove', onResizeMove)
  document.removeEventListener('mouseup', onResizeEnd)
}

// ── Trace log ─────────────────────────────────────────────────────────────────
const traceLogEl = ref<HTMLDivElement>()
watch(
  () => props.cognitive.traceLog.length,
  async () => {
    await nextTick()
    if (traceLogEl.value) traceLogEl.value.scrollTop = traceLogEl.value.scrollHeight
  }
)

// ── Helpers ───────────────────────────────────────────────────────────────────
function traceIcon(type: string) {
  switch (type) {
    case 'tool_call':   return '🔧'
    case 'tool_result': return '✓'
    case 'reflection':  return '💭'
    case 'search_item': return '🔍'
    default:            return '•'
  }
}
function traceColor(type: string) {
  switch (type) {
    case 'tool_call':   return '#60a5fa'
    case 'tool_result': return '#34d399'
    case 'reflection':  return '#f59e0b'
    case 'search_item': return '#a78bfa'
    default:            return '#94a3b8'
  }
}

const doneCount = computed(() => localPlan.value.filter(s => s.status === 'done').length)

// ── Tool history helpers ──────────────────────────────────────────────────────
const HIST_TOOL_META: Record<string, { label: string; icon: string; color: string }> = {
  web_search:           { label: '搜索了网络',  icon: '🔍', color: '#6366f1' },
  fetch_webpage:        { label: '阅读了网页',  icon: '🌐', color: '#0ea5e9' },
  get_current_datetime: { label: '获取了时间',  icon: '🕐', color: '#0ea5e9' },
  calculator:           { label: '执行了计算',  icon: '🧮', color: '#10b981' },
}
function histToolMeta(name: string) {
  return HIST_TOOL_META[name] ?? { label: `调用了 ${name}`, icon: '⚙️', color: '#6b7280' }
}
function histToolDetail(ev: ToolHistoryEvent): string {
  const inp = ev.tool_input
  if (!inp || Object.keys(inp).length === 0) return ''
  const val = (inp.query ?? inp.url ?? inp.expression ?? inp.expr ?? inp.timezone ?? Object.values(inp)[0]) as unknown
  return String(val ?? '').slice(0, 60)
}
function histFormatTime(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}
// 有 live trace 时显示实时日志，否则显示历史工具记录
const showLiveTrace = computed(() => props.loading || props.cognitive.traceLog.length > 0)
</script>

<template>
  <div class="cognitive-panel">

    <!-- Header -->
    <div class="panel-hd">
      <div class="hd-left">
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none">
          <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="#8b5cf6"/>
        </svg>
        <span class="hd-title">执行计划</span>
        <span v-if="loading && cognitive.plan.length > 0" class="hd-progress">
          {{ doneCount }}/{{ cognitive.plan.length }}
        </span>
      </div>
      <el-button size="small" text style="padding:4px;color:#9ca3af" @click="$emit('collapse')">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M11 8L6 3v10l5-5z"/></svg>
      </el-button>
    </div>

    <!-- Goal bar -->
    <div v-if="userMessage" class="goal-bar">
      <span class="goal-chip">目标</span>
      <span class="goal-text">{{ userMessage.slice(0, 80) }}{{ userMessage.length > 80 ? '…' : '' }}</span>
    </div>

    <!-- Empty -->
    <div v-if="localPlan.length === 0 && !loading" class="empty-state">
      <svg width="24" height="24" viewBox="0 0 48 48" fill="none" style="opacity:0.15">
        <path d="M24 4C24 4 26.5 17 36 22C26.5 27 24 40 24 40C24 40 21.5 27 12 22C21.5 17 24 4 24 4Z" fill="#6366f1"/>
      </svg>
      <p>搜索/分析任务执行时，计划节点将在此展示</p>
    </div>

    <!-- Skeleton -->
    <div v-else-if="localPlan.length === 0 && loading" class="skel-area">
      <div v-for="n in 3" :key="n" class="skel-node" :style="`opacity:${1-(n-1)*0.25}`" />
    </div>

    <!-- ── AntV X6 流程图画布 ── -->
    <div v-else class="flow-canvas-section">
      <PlanFlowCanvas
        :plan="localPlan"
        :loading="loading"
        @reorder="(p) => { localPlan = p; isDirty = true }"
        @edit-node="(step, idx) => { editingIndex = idx; editData = { title: step.title, description: step.description }; insertMode = false; editDialogVisible = true }"
        @add-node="(afterIdx) => { editingIndex = afterIdx; editData = { title: '', description: '' }; insertMode = true; editDialogVisible = true }"
        @delete-node="(idx) => { if (localPlan.length > 1) { localPlan = localPlan.filter((_, i) => i !== idx); isDirty = true } }"
      />

      <!-- Dirty banner -->
      <transition name="banner">
        <div v-if="isDirty" class="dirty-banner">
          <div class="dirty-left">
            <span class="dirty-dot"></span>
            <span class="dirty-label">已修改 · {{ localPlan.length }} 步</span>
          </div>
          <div class="dirty-right">
            <button class="dirty-undo" @click="resetLocalPlan">撤销</button>
            <button class="dirty-run" @click="onReexecute">
              <svg width="9" height="9" viewBox="0 0 10 10" fill="currentColor"><path d="M2 1.5l6 3.5-6 3.5V1.5z"/></svg>
              重新执行
            </button>
          </div>
        </div>
      </transition>
    </div>

    <!-- Reflection bar -->
    <transition name="fadebar">
      <div v-if="cognitive.reflection" class="ref-bar">
        <span>💭</span>
        <span class="ref-text">{{ cognitive.reflection }}</span>
        <el-tag v-if="cognitive.reflectorDecision" size="small" effect="light" round
          :type="{ done:'success', continue:'info', retry:'warning' }[cognitive.reflectorDecision] as any">
          {{ { done:'完成', continue:'继续', retry:'重试' }[cognitive.reflectorDecision as 'done'|'continue'|'retry'] || cognitive.reflectorDecision }}
        </el-tag>
      </div>
    </transition>

    <!-- 底部面板：实时追踪日志 或 历史工具调用 -->
    <div class="trace-section" :style="{ height: traceHeight + 'px' }">
      <!-- 拖拽调整手柄 -->
      <div class="trace-resize-handle" @mousedown.prevent="onResizeStart">
        <div class="trace-resize-bar"></div>
      </div>
      <div class="trace-hd">
        <span v-if="showLiveTrace">追踪日志</span>
        <span v-else-if="cognitive.historyEvents.length > 0">工具调用历史</span>
        <span v-else>追踪日志</span>
      </div>
      <div class="trace-body" ref="traceLogEl">
        <!-- 实时追踪（流式推理中） -->
        <template v-if="showLiveTrace">
          <div v-if="!cognitive.traceLog.length" class="trace-empty">暂无记录</div>
          <div v-for="(e, i) in cognitive.traceLog" :key="i" class="trace-row">
            <span class="trace-ic" :style="{ color: traceColor(e.type) }">{{ traceIcon(e.type) }}</span>
            <span class="trace-txt">{{ e.content }}</span>
          </div>
        </template>
        <!-- 历史工具事件（刷新后从 DB 加载） -->
        <template v-else>
          <div v-if="!cognitive.historyEvents.length" class="trace-empty">暂无历史记录</div>
          <div v-for="ev in cognitive.historyEvents" :key="ev.id" class="hist-row">
            <span class="hist-icon">{{ histToolMeta(ev.tool_name).icon }}</span>
            <div class="hist-body">
              <span class="hist-name" :style="{ color: histToolMeta(ev.tool_name).color }">
                {{ histToolMeta(ev.tool_name).label }}
              </span>
              <span v-if="histToolDetail(ev)" class="hist-detail">{{ histToolDetail(ev) }}</span>
            </div>
            <span class="hist-time">{{ histFormatTime(ev.created_at) }}</span>
          </div>
        </template>
      </div>
    </div>

  </div>

  <!-- Edit / Insert dialog -->
  <el-dialog
    v-model="editDialogVisible"
    :title="insertMode ? '插入新步骤' : `编辑步骤 ${editingIndex + 1}`"
    width="400px"
    align-center
    destroy-on-close
  >
    <el-form label-position="top" size="default">
      <el-form-item label="步骤标题 *">
        <el-input v-model="editData.title" placeholder="简短清晰的标题" maxlength="40" show-word-limit autofocus />
      </el-form-item>
      <el-form-item label="执行描述（可选）">
        <el-input v-model="editData.description" type="textarea" :rows="3" placeholder="告诉 Agent 具体做什么" resize="none" />
      </el-form-item>
    </el-form>
    <template #footer>
      <div class="dialog-ft">
        <el-button v-if="!insertMode && localPlan.length > 1" size="small" type="danger" plain :icon="Delete" @click="deleteStep">删除步骤</el-button>
        <span v-else style="flex:1"></span>
        <div style="display:flex;gap:8px">
          <el-button @click="editDialogVisible = false">取消</el-button>
          <el-button type="primary" :disabled="!editData.title.trim()" @click="saveEdit">
            {{ insertMode ? '插入' : '保存' }}
          </el-button>
        </div>
      </div>
    </template>
  </el-dialog>
</template>

<style scoped>
@keyframes node-spin { to { transform: rotate(360deg); } }
@keyframes node-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(139,92,246,0.18); }
  50%       { box-shadow: 0 0 0 4px rgba(139,92,246,0); }
}
@keyframes sk { 0% { background-position: 200% 0; } 100% { background-position: -200% 0; } }

.cognitive-panel {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border-radius: var(--cf-radius-lg, 16px);
  border: 1px solid var(--cf-border-soft, #EBEEF5);
  box-shadow: var(--cf-shadow-sm, 0 4px 16px rgba(0,0,0,0.08));
  overflow: hidden;
}

/* Header */
.panel-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px;
  background: rgba(250,250,252,0.92);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border-bottom: 1px solid #e8eaf2;
  flex-shrink: 0;
}
.hd-left { display: flex; align-items: center; gap: 6px; }
.hd-title { font-size: 12.5px; font-weight: 600; color: #111827; }
.hd-progress {
  font-size: 10.5px; font-weight: 600; color: #8b5cf6;
  background: rgba(139,92,246,0.1); padding: 1px 7px; border-radius: 10px;
}

/* Goal */
.goal-bar {
  display: flex; align-items: flex-start; gap: 6px;
  padding: 5px 12px;
  background: rgba(139,92,246,0.03);
  border-bottom: 1px solid rgba(139,92,246,0.07);
  flex-shrink: 0;
}
.goal-chip {
  font-size: 10px; font-weight: 600; color: #8b5cf6;
  background: rgba(139,92,246,0.1); padding: 1px 5px;
  border-radius: 4px; flex-shrink: 0; margin-top: 1px;
}
.goal-text { font-size: 11px; color: #374151; line-height: 1.4; }

/* Empty */
.empty-state {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 8px; padding: 20px; text-align: center;
}
.empty-state p { font-size: 11px; color: #9ca3af; line-height: 1.6; max-width: 180px; }

/* Skeleton */
.skel-area {
  flex: 1; display: flex; flex-direction: column;
  gap: 6px; padding: 12px 10px; align-items: stretch;
}
.skel-node {
  height: 34px; border-radius: 6px;
  background: linear-gradient(90deg, #e2e8f0 25%, #f1f5f9 50%, #e2e8f0 75%);
  background-size: 200% 100%; animation: sk 1.4s infinite;
}

/* ── AntV X6 画布容器 ── */
.flow-canvas-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

/* ── Dirty banner ── */
.dirty-banner {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 10px;
  background: rgba(99,102,241,0.05); border: 1px solid rgba(99,102,241,0.18);
  border-radius: 8px; margin: 4px 8px 8px; gap: 8px;
}
.dirty-left { display: flex; align-items: center; gap: 6px; }
.dirty-dot {
  width: 6px; height: 6px; border-radius: 50%; background: #6366f1;
  animation: blink 1.2s ease-in-out infinite; flex-shrink: 0;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.dirty-label { font-size: 11.5px; color: #4f46e5; font-weight: 500; }
.dirty-right { display: flex; align-items: center; gap: 6px; flex-shrink: 0; }
.dirty-undo {
  font-size: 11.5px; color: #6b7280; background: none; border: none;
  cursor: pointer; padding: 3px 6px; border-radius: 5px; font-family: inherit;
  transition: background 0.12s;
}
.dirty-undo:hover { background: rgba(0,0,0,0.05); color: #374151; }
.dirty-run {
  display: flex; align-items: center; gap: 5px;
  font-size: 11.5px; font-weight: 600; color: #fff;
  background: #6366f1; border: none; cursor: pointer;
  padding: 4px 10px; border-radius: 6px; font-family: inherit;
  transition: background 0.12s, transform 0.1s;
}
.dirty-run:hover { background: #4f46e5; transform: translateY(-1px); }
.dirty-run:active { transform: translateY(0); }

/* Reflection bar */
.ref-bar {
  display: flex; align-items: center; gap: 6px;
  padding: 6px 12px; background: rgba(245,158,11,0.05);
  border-top: 1px solid rgba(245,158,11,0.1); flex-shrink: 0;
}
.ref-text { flex: 1; font-size: 10.5px; color: #92400e; line-height: 1.4; min-width: 0; }

/* Trace */
.trace-section {
  flex-shrink: 0;
  display: flex; flex-direction: column;
  border-top: 1px solid #e5e7eb;
  min-height: 120px;
  max-height: 500px;
  overflow: hidden;
}
/* 拖拽调整手柄 */
.trace-resize-handle {
  height: 10px;
  cursor: ns-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: transparent;
  transition: background 0.15s;
}
.trace-resize-handle:hover { background: rgba(99,102,241,0.04); }
.trace-resize-bar {
  width: 36px;
  height: 3px;
  background: #e2e5f0;
  border-radius: 99px;
  transition: background 0.2s, width 0.2s;
}
.trace-resize-handle:hover .trace-resize-bar {
  background: #6366f1;
  width: 48px;
}
.trace-hd { font-size: 10px; font-weight: 600; color: #9ca3af; padding: 2px 12px 3px; text-transform: uppercase; letter-spacing: 0.06em; }
.trace-body { flex: 1; overflow-y: auto; padding: 0 8px 6px; }
.trace-empty { font-size: 11px; color: #d1d5db; text-align: center; padding: 12px 0; }
.trace-row { display: flex; align-items: flex-start; gap: 4px; padding: 1.5px 3px; border-radius: 3px; }
.trace-row:hover { background: rgba(0,0,0,0.03); }
.trace-ic { font-size: 10px; flex-shrink: 0; line-height: 1.7; }
.trace-txt { font-size: 10.5px; color: #4b5563; line-height: 1.65; word-break: break-all; }

/* Dialog footer */
.dialog-ft { display: flex; align-items: center; justify-content: space-between; gap: 8px; }

/* Tool history rows */
.hist-row {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 3px 4px;
  border-radius: 5px;
  transition: background 0.1s;
}
.hist-row:hover { background: rgba(99,102,241,0.04); }
.hist-icon { font-size: 12px; flex-shrink: 0; line-height: 1.7; }
.hist-body {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 1px;
}
.hist-name {
  font-size: 11px;
  font-weight: 500;
  line-height: 1.4;
}
.hist-detail {
  font-size: 10px;
  color: #9ca3af;
  line-height: 1.3;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.hist-time {
  font-size: 9.5px;
  color: #d1d5db;
  flex-shrink: 0;
  line-height: 1.8;
}

/* Animations */
.banner-enter-active, .banner-leave-active { transition: all 0.22s ease; }
.banner-enter-from, .banner-leave-to { opacity: 0; transform: translateY(-6px); }
.fadebar-enter-active, .fadebar-leave-active { transition: opacity 0.22s; }
.fadebar-enter-from, .fadebar-leave-to { opacity: 0; }
</style>
