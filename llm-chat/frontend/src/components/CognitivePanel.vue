<script setup lang="ts">
import { ref, computed, watch, nextTick } from 'vue'
import type { CognitiveState, PlanStep, ToolHistoryEvent, FileArtifact } from '../types'
import { isPreviewable } from '../types'
import { Delete } from '@element-plus/icons-vue'
import PlanFlowCanvas from './PlanFlowCanvas.vue'
import hljs from 'highlight.js/lib/common'
import { buildIframeSrcdoc } from '../utils/codePreviewBuilders'

const props = defineProps<{
  cognitive: CognitiveState
  loading: boolean
  userMessage?: string
  selectedFile?: FileArtifact | null
}>()

const emit = defineEmits<{
  collapse: []
  modifyPlan: [plan: PlanStep[]]
  closeFile: []
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
  let changeIdx: number
  if (insertMode.value) {
    changeIdx = editingIndex.value + 1
    updated.splice(changeIdx, 0, {
      id: `new-${Date.now()}`,
      title: editData.value.title.trim(),
      description: editData.value.description,
      status: 'pending',
      result: '',
    })
  } else {
    changeIdx = editingIndex.value
    updated[changeIdx] = {
      ...updated[changeIdx],
      title: editData.value.title.trim(),
      description: editData.value.description,
      status: 'pending',   // 编辑过的步骤重置为 pending
      result: '',
    }
  }
  // 从修改点开始，后续所有步骤重置为 pending（需要重新执行）
  for (let i = changeIdx + 1; i < updated.length; i++) {
    updated[i] = { ...updated[i], status: 'pending', result: '' }
  }
  localPlan.value = updated
  isDirty.value = true
  editDialogVisible.value = false
}

function deleteStep() {
  if (localPlan.value.length <= 1) return
  const delIdx = editingIndex.value
  const updated = localPlan.value.filter((_, i) => i !== delIdx)
  // 删除点之后的步骤重置为 pending
  for (let i = delIdx; i < updated.length; i++) {
    updated[i] = { ...updated[i], status: 'pending', result: '' }
  }
  localPlan.value = updated
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
  web_search:           { label: '搜索了网络',  icon: '🔍', color: '#00AEEC' },
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

// ── 文件预览 ─────────────────────────────────────────────────────────────────
// 当前面板 tab：plan（执行计划） / file（文件预览）
// 初始值：如果组件挂载时已有 selectedFile，直接切到文件 tab
const activeTab = ref<'plan' | 'file'>(props.selectedFile ? 'file' : 'plan')

// 当 selectedFile 变化时自动切到文件 tab（immediate 处理组件首次挂载）
watch(() => props.selectedFile, (f) => {
  if (f) activeTab.value = 'file'
}, { immediate: true })

// 文件预览模式
const fileViewMode = ref<'code' | 'preview'>('code')

// 语法高亮后的 HTML
const highlightedCode = computed(() => {
  if (!props.selectedFile) return ''
  const lang = props.selectedFile.language
  try {
    if (hljs.getLanguage(lang)) {
      return hljs.highlight(props.selectedFile.content, { language: lang, ignoreIllegals: true }).value
    }
    return hljs.highlightAuto(props.selectedFile.content).value
  } catch {
    return props.selectedFile.content
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
  }
})

// iframe srcdoc for HTML preview
const previewSrcdoc = computed(() => {
  if (!props.selectedFile) return ''
  return buildIframeSrcdoc(props.selectedFile.content, props.selectedFile.language)
})

const canPreview = computed(() => props.selectedFile ? isPreviewable(props.selectedFile.language) : false)

const fileSizeKb = computed(() => {
  if (!props.selectedFile) return '0'
  return (props.selectedFile.content.length / 1024).toFixed(1)
})

function downloadFile() {
  if (!props.selectedFile) return
  const blob = new window.Blob([props.selectedFile.content], { type: 'text/plain;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = props.selectedFile.name
  a.click()
  URL.revokeObjectURL(url)
}

const fileCopied = ref(false)
function copyFileContent() {
  if (!props.selectedFile) return
  navigator.clipboard.writeText(props.selectedFile.content).catch(() => {})
  fileCopied.value = true
  setTimeout(() => { fileCopied.value = false }, 2000)
}
</script>

<template>
  <div class="cognitive-panel">

    <!-- Header with tabs -->
    <div class="panel-hd">
      <div class="hd-left">
        <!-- Tab 切换 -->
        <button class="hd-tab" :class="{ active: activeTab === 'plan' }" @click="activeTab = 'plan'">
          <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
            <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="currentColor"/>
          </svg>
          执行计划
          <span v-if="loading && cognitive.plan.length > 0" class="hd-progress">
            {{ doneCount }}/{{ cognitive.plan.length }}
          </span>
        </button>
        <div v-if="selectedFile" class="hd-tab" :class="{ active: activeTab === 'file' }" @click="activeTab = 'file'">
          <span class="hd-tab-file-icon">📄</span>
          <span class="hd-tab-filename">{{ selectedFile.name }}</span>
          <span class="hd-tab-close" @click.stop="emit('closeFile'); activeTab = 'plan'" title="关闭">
            <svg width="8" height="8" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
              <path d="M12 4L4 12M4 4l8 8"/>
            </svg>
          </span>
        </div>
      </div>
      <el-button size="small" text style="padding:4px;color:#9ca3af" @click="$emit('collapse')">
        <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M11 8L6 3v10l5-5z"/></svg>
      </el-button>
    </div>

    <!-- ══ Tab: 执行计划 ══ -->
    <div v-show="activeTab === 'plan'" class="tab-content plan-tab">

    <!-- Goal bar -->
    <div v-if="userMessage" class="goal-bar">
      <span class="goal-chip">目标</span>
      <span class="goal-text">{{ userMessage.slice(0, 80) }}{{ userMessage.length > 80 ? '…' : '' }}</span>
    </div>

    <!-- Empty -->
    <div v-if="localPlan.length === 0 && !loading" class="empty-state">
      <svg width="24" height="24" viewBox="0 0 48 48" fill="none" style="opacity:0.15">
        <path d="M24 4C24 4 26.5 17 36 22C26.5 27 24 40 24 40C24 40 21.5 27 12 22C21.5 17 24 4 24 4Z" fill="#00AEEC"/>
      </svg>
      <p>搜索/分析任务执行时，计划节点将在此展示</p>
    </div>

    <!-- 加载中但尚无计划：友好状态提示（Bilibili 风格） -->
    <div v-else-if="localPlan.length === 0 && loading" class="loading-hint">
      <div class="hint-icon-wrap">
        <svg class="hint-icon" width="28" height="28" viewBox="0 0 32 32" fill="none">
          <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#00aeec"/>
          <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#00aeec" opacity="0.4"/>
        </svg>
      </div>
      <p class="hint-title">模型正在回答中</p>
      <p class="hint-desc">当前问题无需多步计划，正在直接生成回答</p>
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

    </div><!-- /plan-tab -->

    <!-- ══ Tab: 文件预览 ══ -->
    <div v-show="activeTab === 'file'" class="tab-content file-tab">
      <template v-if="selectedFile">
      <!-- 文件信息栏 -->
      <div class="file-info-bar">
        <span class="file-name-badge">{{ selectedFile.name }}</span>
        <span class="file-lang-tag">{{ selectedFile.language }}</span>
        <span class="file-size">{{ fileSizeKb }} KB</span>
      </div>

      <!-- 操作按钮栏 -->
      <div class="file-actions-bar">
        <button class="file-action-btn" :class="{ active: fileViewMode === 'code' }" @click="fileViewMode = 'code'">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
          </svg>
          代码
        </button>
        <button v-if="canPreview" class="file-action-btn" :class="{ active: fileViewMode === 'preview' }" @click="fileViewMode = 'preview'">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/>
          </svg>
          预览
        </button>
        <div class="file-actions-spacer"></div>
        <button class="file-action-btn file-action-sm" :title="fileCopied ? '已复制' : '复制内容'" @click="copyFileContent">
          <svg v-if="!fileCopied" width="12" height="12" viewBox="0 0 16 16" fill="none">
            <rect x="5.5" y="5.5" width="8" height="9" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
            <path d="M3 10.5V3a1 1 0 011-1h7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
          </svg>
          <svg v-else width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="#00B578" stroke-width="2" stroke-linecap="round">
            <polyline points="3 8 6 11 13 5"/>
          </svg>
        </button>
        <button class="file-action-btn file-action-sm" title="下载文件" @click="downloadFile">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        </button>
      </div>

      <!-- 代码视图 -->
      <div v-if="fileViewMode === 'code'" class="file-code-view">
        <pre class="file-code-pre"><code class="hljs" v-html="highlightedCode"></code></pre>
      </div>

      <!-- 预览视图 -->
      <div v-if="fileViewMode === 'preview' && canPreview" class="file-preview-view">
        <iframe
          :srcdoc="previewSrcdoc"
          class="file-preview-frame"
          sandbox="allow-scripts allow-forms allow-modals allow-popups"
        />
      </div>
      </template>
      <div v-else class="file-tab-empty">
        <p>点击对话中的文件卡片查看文件</p>
      </div>
    </div><!-- /file-tab -->

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
  0%, 100% { box-shadow: 0 0 0 0 rgba(0,174,236,0.18); }
  50%       { box-shadow: 0 0 0 4px rgba(0,174,236,0); }
}


.cognitive-panel {
  width: 100%;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: #ffffff;
  border-radius: var(--cf-radius-lg, 18px);
  border: 1px solid var(--cf-border-soft, #EBEDF0);
  box-shadow: var(--cf-shadow-sm, 0 2px 8px rgba(0,0,0,0.06));
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
.hd-left { display: flex; align-items: center; gap: 2px; min-width: 0; flex: 1; overflow: hidden; }
.hd-title { font-size: 12.5px; font-weight: 600; color: #111827; }
.hd-progress {
  font-size: 10.5px; font-weight: 600; color: #00AEEC;
  background: rgba(0,174,236,0.08); padding: 1px 7px; border-radius: 10px;
}

/* Goal — Bilibili 风格 */
.goal-bar {
  display: flex; align-items: flex-start; gap: 6px;
  padding: 5px 12px;
  background: rgba(0,174,236,0.03);
  border-bottom: 1px solid rgba(0,174,236,0.07);
  flex-shrink: 0;
}
.goal-chip {
  font-size: 10px; font-weight: 600; color: #00AEEC;
  background: rgba(0,174,236,0.08); padding: 1px 5px;
  border-radius: 10px; flex-shrink: 0; margin-top: 1px;
}
.goal-text { font-size: 11px; color: #374151; line-height: 1.4; }

/* Empty */
.empty-state {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 8px; padding: 20px; text-align: center;
}
.empty-state p { font-size: 11px; color: #9ca3af; line-height: 1.6; max-width: 180px; }

/* 加载友好提示（Bilibili 风格） */
.loading-hint {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  gap: 8px; padding: 24px 16px; text-align: center;
}
.hint-icon-wrap {
  width: 48px; height: 48px; border-radius: 14px;
  background: rgba(0, 174, 236, 0.06);
  display: flex; align-items: center; justify-content: center;
}
.hint-icon {
  animation: hint-breathe 2s ease-in-out infinite;
}
@keyframes hint-breathe {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.5; transform: scale(0.88); }
}
.hint-title {
  font-size: 13px; font-weight: 600; color: #18191c;
  margin: 4px 0 0;
}
.hint-desc {
  font-size: 11.5px; color: #9499a0; line-height: 1.5;
  max-width: 200px;
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

/* ── Dirty banner — Bilibili 风格 ── */
.dirty-banner {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 10px;
  background: rgba(0,174,236,0.04); border: 1px solid rgba(0,174,236,0.15);
  border-radius: 10px; margin: 4px 8px 8px; gap: 8px;
}
.dirty-left { display: flex; align-items: center; gap: 6px; }
.dirty-dot {
  width: 6px; height: 6px; border-radius: 50%; background: #00AEEC;
  animation: blink 1.2s ease-in-out infinite; flex-shrink: 0;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.dirty-label { font-size: 11.5px; color: #0095CC; font-weight: 500; }
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
  background: linear-gradient(135deg, #00AEEC, #23C1F0); border: none; cursor: pointer;
  padding: 4px 12px; border-radius: 20px; font-family: inherit;
  transition: all 0.15s; box-shadow: 0 2px 6px rgba(0,174,236,0.2);
}
.dirty-run:hover { box-shadow: 0 4px 12px rgba(0,174,236,0.3); transform: translateY(-1px); }
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
.trace-resize-handle:hover { background: rgba(0,174,236,0.04); }
.trace-resize-bar {
  width: 36px;
  height: 3px;
  background: #E3E5E7;
  border-radius: 99px;
  transition: background 0.2s, width 0.2s;
}
.trace-resize-handle:hover .trace-resize-bar {
  background: #00AEEC;
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
.hist-row:hover { background: rgba(0,174,236,0.04); }
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

/* ── Tab 切换按钮 ── */
.hd-tab {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: none;
  border-radius: 8px;
  background: transparent;
  color: #9499A0;
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
  max-width: 150px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.hd-tab:hover { background: rgba(0,174,236,0.05); color: #61666D; }
.hd-tab.active {
  background: rgba(0,174,236,0.08);
  color: #00AEEC;
  font-weight: 600;
}
.hd-tab-file-icon { font-size: 11px; }
.hd-tab-filename {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100px;
}
.hd-tab-close {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 14px; height: 14px;
  border-radius: 50%;
  border: none;
  background: transparent;
  color: #9499A0;
  cursor: pointer;
  padding: 0;
  margin-left: 2px;
  transition: all 0.12s;
  flex-shrink: 0;
}
.hd-tab-close:hover { background: rgba(242,93,89,0.1); color: #F25D59; }

/* ── Tab content container ── */
.tab-content {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
  overflow: hidden;
}
.plan-tab { /* inherits from existing styles */ }
.file-tab { flex: 1; }
.file-tab-empty {
  flex: 1;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #9499A0;
  font-size: 12px;
}

/* ── 文件信息栏 ── */
.file-info-bar {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: rgba(0,174,236,0.03);
  border-bottom: 1px solid rgba(0,174,236,0.07);
  flex-shrink: 0;
}
.file-name-badge {
  font-size: 12px;
  font-weight: 600;
  color: #18191C;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.file-lang-tag {
  font-size: 10px;
  font-weight: 600;
  color: #00AEEC;
  background: rgba(0,174,236,0.08);
  padding: 1px 6px;
  border-radius: 8px;
  flex-shrink: 0;
  text-transform: uppercase;
}
.file-size {
  font-size: 10.5px;
  color: #9499A0;
  margin-left: auto;
  flex-shrink: 0;
}

/* ── 操作按钮栏 ── */
.file-actions-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 6px 10px;
  border-bottom: 1px solid #E3E5E7;
  flex-shrink: 0;
}
.file-action-btn {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border: 1px solid #E3E5E7;
  border-radius: 8px;
  background: #fff;
  color: #61666D;
  font-size: 11.5px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
}
.file-action-btn:hover {
  border-color: #00AEEC;
  color: #00AEEC;
  background: #E3F6FD;
}
.file-action-btn.active {
  border-color: #00AEEC;
  background: #E3F6FD;
  color: #00AEEC;
  font-weight: 600;
}
.file-action-sm {
  padding: 4px 6px;
}
.file-actions-spacer { flex: 1; }

/* ── 代码视图 ── */
.file-code-view {
  flex: 1;
  overflow: auto;
  background: #FAFBFC;
}
.file-code-pre {
  margin: 0;
  padding: 12px 16px;
  font-size: 12.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  font-family: 'Fira Code', 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
}
.file-code-pre code.hljs {
  background: transparent !important;
  padding: 0;
  font-size: inherit;
  line-height: inherit;
  border: none;
}

/* ── 预览视图 ── */
.file-preview-view {
  flex: 1;
  overflow: hidden;
  background: #fff;
}
.file-preview-frame {
  width: 100%;
  height: 100%;
  border: none;
  display: block;
}
</style>
