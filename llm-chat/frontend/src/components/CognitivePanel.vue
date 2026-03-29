<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import type { CognitiveState, PlanStep } from '../types'
import { Edit, Loading, SuccessFilled, CircleCloseFilled, Plus, Delete } from '@element-plus/icons-vue'

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

// ── Drag-and-drop reordering ──────────────────────────────────────────────────
const dragSrcIdx  = ref(-1)   // which node is being dragged
const dragOverIdx = ref(-1)   // which node the cursor is over (drop target)

function canDrag(i: number) {
  return !props.loading && localPlan.value[i]?.status !== 'running'
}

function onDragStart(i: number, e: DragEvent) {
  if (!canDrag(i)) { e.preventDefault(); return }
  dragSrcIdx.value = i
  if (e.dataTransfer) {
    e.dataTransfer.effectAllowed = 'move'
    e.dataTransfer.setData('text/plain', String(i))
  }
}

function onDragOver(i: number, e: DragEvent) {
  if (dragSrcIdx.value === -1) return
  e.preventDefault()
  if (e.dataTransfer) e.dataTransfer.dropEffect = 'move'
  if (i !== dragSrcIdx.value) dragOverIdx.value = i
}

function onDragLeave(i: number) {
  if (dragOverIdx.value === i) dragOverIdx.value = -1
}

function onDrop(i: number, e: DragEvent) {
  e.preventDefault()
  const src = dragSrcIdx.value
  if (src === -1 || src === i) { onDragEnd(); return }

  const updated = [...localPlan.value]
  const [moved] = updated.splice(src, 1)
  updated.splice(i, 0, moved)
  localPlan.value = updated
  isDirty.value = true
  onDragEnd()
}

function onDragEnd() {
  dragSrcIdx.value  = -1
  dragOverIdx.value = -1
}

// ── Edit dialog ───────────────────────────────────────────────────────────────
const editDialogVisible = ref(false)
const editingIndex = ref(-1)
const editData = ref({ title: '', description: '' })
const insertMode = ref(false)

function onNodeClick(i: number) {
  if (props.loading) return
  if (dragSrcIdx.value !== -1) return   // was a drag, not a click
  const step = localPlan.value[i]
  if (!step || step.status === 'running') return
  editingIndex.value = i
  editData.value = { title: step.title, description: step.description }
  insertMode.value = false
  editDialogVisible.value = true
}

function onInsertBetween(afterIndex: number) {
  if (props.loading) return
  editingIndex.value = afterIndex
  editData.value = { title: '', description: '' }
  insertMode.value = true
  editDialogVisible.value = true
}

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
function statusColor(status: PlanStep['status']): string {
  switch (status) {
    case 'running': return '#8b5cf6'
    case 'done':    return '#22c55e'
    case 'failed':  return '#ef4444'
    default:        return '#d1d5db'
  }
}
function statusBg(status: PlanStep['status']): string {
  switch (status) {
    case 'running': return 'rgba(139,92,246,0.05)'
    case 'done':    return 'rgba(34,197,94,0.04)'
    case 'failed':  return 'rgba(239,68,68,0.04)'
    default:        return '#ffffff'
  }
}
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

    <!-- Workflow nodes -->
    <div v-else class="nodes-scroll">
      <div class="nodes-wrap">

        <!-- Insert before first node -->
        <div class="connector-row connector-row--top" @click.stop="onInsertBetween(-1)">
          <div class="conn-track">
            <div class="conn-insert conn-insert--top">
              <el-icon style="font-size:8px"><Plus /></el-icon>
            </div>
          </div>
        </div>

        <template v-for="(step, i) in localPlan" :key="step.id">

          <!-- ── Node wrapper (drag target) ── -->
          <div
            class="node-item"
            :class="{
              'node-item--drag-src':  dragSrcIdx === i,
              'node-item--drag-over': dragOverIdx === i && dragSrcIdx !== i,
            }"
            :draggable="canDrag(i)"
            @dragstart="onDragStart(i, $event)"
            @dragover="onDragOver(i, $event)"
            @dragleave="onDragLeave(i)"
            @drop="onDrop(i, $event)"
            @dragend="onDragEnd"
          >
            <!-- Node card -->
            <div
              class="wf-node"
              :class="{
                'wf-node--clickable': !loading && step.status !== 'running',
                'wf-node--running':   step.status === 'running',
                'wf-node--done':      step.status === 'done',
                'wf-node--failed':    step.status === 'failed',
              }"
              :style="{ '--accent': statusColor(step.status), background: statusBg(step.status) }"
              @click="onNodeClick(i)"
            >
              <!-- Drag handle (6-dot grip) -->
              <div
                v-if="canDrag(i)"
                class="drag-handle"
                title="拖拽排序"
                @mousedown.stop
              >
                <svg width="8" height="12" viewBox="0 0 8 12" fill="currentColor">
                  <circle cx="2" cy="2" r="1.2"/><circle cx="6" cy="2" r="1.2"/>
                  <circle cx="2" cy="6" r="1.2"/><circle cx="6" cy="6" r="1.2"/>
                  <circle cx="2" cy="10" r="1.2"/><circle cx="6" cy="10" r="1.2"/>
                </svg>
              </div>

              <!-- Left accent -->
              <div class="node-accent"></div>

              <!-- Status icon -->
              <div class="node-icon">
                <el-icon v-if="step.status === 'running'" style="font-size:11px;color:#8b5cf6;animation:node-spin 1s linear infinite"><Loading /></el-icon>
                <el-icon v-else-if="step.status === 'done'" style="font-size:13px;color:#22c55e"><SuccessFilled /></el-icon>
                <el-icon v-else-if="step.status === 'failed'" style="font-size:13px;color:#ef4444"><CircleCloseFilled /></el-icon>
                <span v-else class="node-num">{{ i + 1 }}</span>
              </div>

              <!-- Content -->
              <div class="node-body">
                <div class="node-title">{{ step.title }}</div>
                <div v-if="step.description" class="node-desc">{{ step.description }}</div>
              </div>

              <!-- Edit hint -->
              <el-icon v-if="!loading && step.status !== 'running'" class="node-edit-hint"><Edit /></el-icon>
            </div>
          </div>

          <!-- Connector + insert between -->
          <div v-if="i < localPlan.length - 1" class="connector-row" @click.stop="onInsertBetween(i)">
            <div class="conn-track">
              <div class="conn-line"></div>
              <div class="conn-insert">
                <el-icon style="font-size:8px"><Plus /></el-icon>
              </div>
            </div>
          </div>

        </template>

        <!-- Add at end -->
        <div class="add-end-btn" @click="onInsertBetween(localPlan.length - 1)">
          <el-icon style="font-size:11px"><Plus /></el-icon>
          添加步骤
        </div>

      </div>

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

    <!-- Trace log -->
    <div class="trace-section">
      <div class="trace-hd">追踪日志</div>
      <div class="trace-body" ref="traceLogEl">
        <div v-if="!cognitive.traceLog.length" class="trace-empty">暂无记录</div>
        <div v-for="(e, i) in cognitive.traceLog" :key="i" class="trace-row">
          <span class="trace-ic" :style="{ color: traceColor(e.type) }">{{ traceIcon(e.type) }}</span>
          <span class="trace-txt">{{ e.content }}</span>
        </div>
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
  background: var(--cf-bg, #f8f9fb);
  border-left: 1px solid var(--cf-border-soft, #e5e7eb);
  overflow: hidden;
}

/* Header */
.panel-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  background: rgba(243,244,248,0.95);
  border-bottom: 1px solid #e5e7eb;
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

/* Scroll area */
.nodes-scroll {
  flex: 1; overflow-y: auto;
  display: flex; flex-direction: column; min-height: 0;
}
.nodes-wrap { padding: 6px 8px 6px; display: flex; flex-direction: column; }

/* ── Node item (drag wrapper) ── */
.node-item {
  display: flex; flex-direction: column;
  position: relative;
  transition: opacity 0.15s;
}

/* Drag source: fade out */
.node-item--drag-src { opacity: 0.35; }
.node-item--drag-src .wf-node { border-style: dashed !important; }

/* Drop target: blue top-line indicator */
.node-item--drag-over::before {
  content: '';
  display: block;
  height: 2px;
  background: #6366f1;
  border-radius: 1px;
  margin-bottom: 2px;
  animation: drop-pulse 0.7s ease-in-out infinite alternate;
}
@keyframes drop-pulse {
  from { opacity: 0.7; }
  to   { opacity: 1; box-shadow: 0 0 6px rgba(99,102,241,0.5); }
}

/* ── Compact AntV-style node card ── */
.wf-node {
  display: flex; align-items: center; gap: 7px;
  padding: 5px 7px 5px 0;
  border-radius: 6px;
  border: 1px solid #e5e7eb;
  background: #fff;
  cursor: default;
  transition: border-color 0.15s, background 0.15s, box-shadow 0.15s;
  position: relative; user-select: none; overflow: hidden;
}

/* Drag handle (6-dot grip icon) */
.drag-handle {
  width: 16px;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #d1d5db;
  cursor: grab;
  opacity: 0;
  transition: opacity 0.12s, color 0.12s;
  padding-left: 4px;
}
.wf-node:hover .drag-handle,
.node-item[draggable="true"]:hover .drag-handle {
  opacity: 1;
}
.drag-handle:hover { color: #6366f1; }
.drag-handle:active { cursor: grabbing; }

/* Left accent bar */
.node-accent {
  width: 3px; align-self: stretch; flex-shrink: 0;
  background: var(--accent, #e5e7eb);
  transition: background 0.2s;
}

.wf-node--clickable { cursor: pointer; }
.wf-node--clickable:hover {
  border-color: #c4b5fd;
  box-shadow: 0 1px 6px rgba(99,102,241,0.08);
}
.wf-node--clickable:hover .node-edit-hint { opacity: 1 !important; }
.wf-node--running { animation: node-pulse 2s ease-in-out infinite; }

/* Done: green */
.wf-node--done { border-color: #bbf7d0 !important; background: rgba(34,197,94,0.06) !important; }
.wf-node--done .node-icon { border-color: #86efac; background: #f0fdf4; }

/* Failed: red */
.wf-node--failed { border-color: #fecaca !important; background: rgba(239,68,68,0.04) !important; }

/* Node icon */
.node-icon {
  width: 20px; height: 20px; border-radius: 50%;
  border: 1.5px solid #e5e7eb; background: #f9fafb;
  display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.node-num { font-size: 9.5px; font-weight: 700; color: #6b7280; }

/* Content */
.node-body { flex: 1; min-width: 0; }
.node-title {
  font-size: 12px; font-weight: 500; color: #111827; line-height: 1.3;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.node-desc {
  font-size: 10.5px; color: #6b7280; line-height: 1.35; margin-top: 1px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}

/* Edit hint */
.node-edit-hint {
  font-size: 11px; color: #a78bfa; opacity: 0;
  transition: opacity 0.12s; flex-shrink: 0; margin-right: 4px;
}

/* ── Connector ── */
.connector-row {
  display: flex; justify-content: center;
  height: 10px; cursor: pointer; position: relative;
}
.connector-row--top { height: 14px; margin-bottom: 2px; }
.conn-track {
  display: flex; flex-direction: column; align-items: center;
  position: relative; height: 100%;
  padding-left: 28px;
}
.conn-line {
  width: 1.5px; height: 100%; background: #e5e7eb;
  border-radius: 1px; transition: background 0.15s;
}
.conn-insert {
  position: absolute; top: 50%; left: 20px;
  transform: translateY(-50%);
  width: 14px; height: 14px; border-radius: 50%;
  background: #fff; border: 1.5px dashed #d1d5db;
  display: flex; align-items: center; justify-content: center;
  color: #9ca3af; opacity: 0;
  transition: opacity 0.12s, border-color 0.12s;
}
.conn-insert--top {
  position: static; transform: none; margin: auto; opacity: 0.35;
}
.connector-row--top:hover .conn-insert--top {
  opacity: 1; border-color: #a5b4fc; color: #6366f1;
}
.connector-row:hover .conn-insert { opacity: 1; border-color: #a5b4fc; color: #6366f1; }
.connector-row:hover .conn-line { background: #c4b5fd; }

/* Add at end */
.add-end-btn {
  display: flex; align-items: center; gap: 4px;
  padding: 5px 8px; border: 1.5px dashed #e5e7eb;
  border-radius: 6px; background: transparent;
  color: #9ca3af; font-size: 11.5px; cursor: pointer;
  transition: all 0.12s; margin-top: 6px;
}
.add-end-btn:hover { border-color: #a5b4fc; color: #6366f1; background: rgba(99,102,241,0.03); }

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
  flex-shrink: 0; height: 132px;
  display: flex; flex-direction: column; border-top: 1px solid #e5e7eb;
}
.trace-hd { font-size: 10px; font-weight: 600; color: #9ca3af; padding: 5px 12px 2px; text-transform: uppercase; letter-spacing: 0.06em; }
.trace-body { flex: 1; overflow-y: auto; padding: 0 8px 6px; }
.trace-empty { font-size: 11px; color: #d1d5db; text-align: center; padding: 12px 0; }
.trace-row { display: flex; align-items: flex-start; gap: 4px; padding: 1.5px 3px; border-radius: 3px; }
.trace-row:hover { background: rgba(0,0,0,0.03); }
.trace-ic { font-size: 10px; flex-shrink: 0; line-height: 1.7; }
.trace-txt { font-size: 10.5px; color: #4b5563; line-height: 1.65; word-break: break-all; }

/* Dialog footer */
.dialog-ft { display: flex; align-items: center; justify-content: space-between; gap: 8px; }

/* Animations */
.banner-enter-active, .banner-leave-active { transition: all 0.22s ease; }
.banner-enter-from, .banner-leave-to { opacity: 0; transform: translateY(-6px); }
.fadebar-enter-active, .fadebar-leave-active { transition: opacity 0.22s; }
.fadebar-enter-from, .fadebar-leave-to { opacity: 0; }
</style>
