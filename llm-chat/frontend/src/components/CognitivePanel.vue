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
  fileLoading?: boolean
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

// iframe 渲染状态（srcdoc 大 HTML 加载需要时间）
const iframeRendering = ref(false)
function onIframeLoad() { iframeRendering.value = false }

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

const canPreview = computed(() => {
  if (!props.selectedFile) return false
  if (props.selectedFile.language === 'pptx') return true  // PPT 始终可预览
  return isPreviewable(props.selectedFile.language)
})

const isPptFile = computed(() => props.selectedFile?.language === 'pptx')

// PPT 幻灯片翻页
const pptSlideIndex = ref(0)
const pptSlides = computed(() => props.selectedFile?.slides_html || [])
const pptSlideCount = computed(() => pptSlides.value.length)
const pptCurrentSlideHtml = computed(() => pptSlides.value[pptSlideIndex.value] || '')

watch(() => props.selectedFile, (f) => {
  pptSlideIndex.value = 0
  // PPT 文件始终默认预览模式（不该显示 base64 代码）
  if (f && f.language === 'pptx') {
    fileViewMode.value = 'preview'
  } else if (f && isPreviewable(f.language)) {
    fileViewMode.value = 'preview'
    iframeRendering.value = true  // 标记 iframe 正在渲染
  } else {
    fileViewMode.value = 'code'
  }
})

function pptPrev() { if (pptSlideIndex.value > 0) pptSlideIndex.value-- }
function pptNext() { if (pptSlideIndex.value < pptSlideCount.value - 1) pptSlideIndex.value++ }

const fileSizeKb = computed(() => {
  if (!props.selectedFile) return '0'
  if (props.selectedFile.size) return (props.selectedFile.size / 1024).toFixed(1)
  return (props.selectedFile.content.length / 1024).toFixed(1)
})

function downloadFile() {
  if (!props.selectedFile) return
  const isBinary = props.selectedFile.binary || ['pptx', 'pdf'].includes(props.selectedFile.language)
  if (isBinary) {
    // base64 → 二进制 → Blob → 下载
    try {
      const byteChars = atob(props.selectedFile.content)
      const bytes = new Uint8Array(byteChars.length)
      for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i)
      const mimeMap: Record<string, string> = {
        pptx: 'application/vnd.openxmlformats-officedocument.presentationml.presentation',
        pdf: 'application/pdf',
      }
      const blob = new Blob([bytes], { type: mimeMap[props.selectedFile.language] || 'application/octet-stream' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = props.selectedFile.name; a.click()
      URL.revokeObjectURL(url)
    } catch (e) {
      console.error('下载失败:', e)
    }
  } else {
    const blob = new Blob([props.selectedFile.content], { type: 'text/plain;charset=utf-8' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = props.selectedFile.name; a.click()
    URL.revokeObjectURL(url)
  }
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
        <el-tabs v-model="activeTab" class="panel-tabs" @tab-click="() => {}">
          <el-tab-pane name="plan">
            <template #label>
              <span class="tab-label-inner">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none">
                  <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="currentColor"/>
                </svg>
                执行计划
                <el-tag v-if="loading && cognitive.plan.length > 0" size="small" effect="plain" type="primary" round class="hd-progress-tag">
                  {{ doneCount }}/{{ cognitive.plan.length }}
                </el-tag>
              </span>
            </template>
          </el-tab-pane>
          <el-tab-pane v-if="selectedFile" name="file">
            <template #label>
              <span class="tab-label-inner tab-file-label">
                <span class="hd-tab-file-icon">📄</span>
                <span class="hd-tab-filename">{{ selectedFile.name }}</span>
                <span class="hd-tab-close" @click.stop="emit('closeFile'); activeTab = 'plan'" title="关闭">
                  <svg width="8" height="8" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round">
                    <path d="M12 4L4 12M4 4l8 8"/>
                  </svg>
                </span>
              </span>
            </template>
          </el-tab-pane>
        </el-tabs>
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

    <!-- 加载中但尚无计划：骨架屏 + 友好提示 -->
    <div v-else-if="localPlan.length === 0 && loading" class="loading-hint">
      <div class="skeleton-plan-area">
        <el-skeleton :rows="0" animated class="skeleton-header">
          <template #template>
            <el-skeleton-item variant="text" style="width: 40%; height: 14px;" />
          </template>
        </el-skeleton>
        <el-skeleton :rows="3" animated class="skeleton-body" />
        <el-skeleton :rows="2" animated class="skeleton-body" />
      </div>
      <div class="hint-bottom">
        <div class="hint-icon-wrap">
          <svg class="hint-icon" width="22" height="22" viewBox="0 0 32 32" fill="none">
            <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#00aeec"/>
          </svg>
        </div>
        <p class="hint-title">模型正在回答中</p>
        <p class="hint-desc">当前问题无需多步计划，正在直接生成回答</p>
      </div>
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
            <el-tag type="warning" effect="light" size="small" round>已修改</el-tag>
            <span class="dirty-label">{{ localPlan.length }} 步</span>
          </div>
          <el-button-group class="dirty-right" size="small">
            <el-button @click="resetLocalPlan">撤销</el-button>
            <el-button type="primary" @click="onReexecute">
              <svg width="9" height="9" viewBox="0 0 10 10" fill="currentColor" style="margin-right:4px"><path d="M2 1.5l6 3.5-6 3.5V1.5z"/></svg>
              重新执行
            </el-button>
          </el-button-group>
        </div>
      </transition>
    </div>

    <!-- Reflection bar -->
    <transition name="fadebar">
      <el-alert v-if="cognitive.reflection" type="info" :closable="false" class="ref-alert">
        <template #title>
          <div class="ref-alert-content">
            <span class="ref-icon">💭</span>
            <span class="ref-text">{{ cognitive.reflection }}</span>
            <el-tag v-if="cognitive.reflectorDecision" size="small" effect="light" round
              :type="({ done:'success', continue:'info', retry:'warning' } as Record<string, any>)[cognitive.reflectorDecision] ?? 'info'">
              {{ ({ done:'完成', continue:'继续', retry:'重试' } as Record<string, string>)[cognitive.reflectorDecision] || cognitive.reflectorDecision }}
            </el-tag>
          </div>
        </template>
      </el-alert>
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
          <el-timeline class="trace-timeline">
            <el-timeline-item
              v-for="(e, i) in cognitive.traceLog"
              :key="i"
              :color="traceColor(e.type)"
              :hollow="e.type !== 'tool_call'"
              size="small"
              class="trace-timeline-item"
            >
              <span class="trace-txt">
                <span class="trace-ic">{{ traceIcon(e.type) }}</span>
                {{ e.content }}
              </span>
            </el-timeline-item>
          </el-timeline>
        </template>
        <!-- 历史工具事件（刷新后从 DB 加载） -->
        <template v-else>
          <div v-if="!cognitive.historyEvents.length" class="trace-empty">暂无历史记录</div>
          <el-timeline class="trace-timeline">
            <el-timeline-item
              v-for="ev in cognitive.historyEvents"
              :key="ev.id"
              :color="histToolMeta(ev.tool_name).color"
              size="small"
              class="trace-timeline-item"
            >
              <div class="hist-content">
                <span class="hist-icon">{{ histToolMeta(ev.tool_name).icon }}</span>
                <div class="hist-body">
                  <span class="hist-name" :style="{ color: histToolMeta(ev.tool_name).color }">
                    {{ histToolMeta(ev.tool_name).label }}
                  </span>
                  <span v-if="histToolDetail(ev)" class="hist-detail">{{ histToolDetail(ev) }}</span>
                </div>
                <span class="hist-time">{{ histFormatTime(ev.created_at) }}</span>
              </div>
            </el-timeline-item>
          </el-timeline>
        </template>
      </div>
    </div>

    </div><!-- /plan-tab -->

    <!-- ══ Tab: 文件预览 ══ -->
    <div v-show="activeTab === 'file'" class="tab-content file-tab">
      <template v-if="selectedFile">
      <!-- 文件信息栏 -->
      <div class="file-info-section">
        <el-descriptions :column="4" size="small" border class="file-descriptions">
          <el-descriptions-item label="文件名" :span="2">
            <span class="file-name-badge">{{ selectedFile.name }}</span>
          </el-descriptions-item>
          <el-descriptions-item label="语言">
            <el-tag size="small" type="primary" effect="plain">{{ isPptFile ? 'PowerPoint' : selectedFile.language }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="大小">
            {{ fileSizeKb }} KB
            <span v-if="selectedFile.slide_count"> · {{ selectedFile.slide_count }} 页</span>
          </el-descriptions-item>
        </el-descriptions>
        <div class="file-info-actions">
          <el-button size="small" text title="下载文件" @click="downloadFile">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
              <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
          </el-button>
        </div>
      </div>

      <!-- 操作按钮栏（PPT 时只显示预览，非 PPT 显示代码/预览/复制） -->
      <div v-if="!isPptFile" class="file-actions-bar">
        <button class="file-action-btn" :class="{ active: fileViewMode === 'code' }" @click="fileViewMode = 'code'">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>
          </svg>
          代码
        </button>
        <button v-if="canPreview" class="file-action-btn" :class="{ active: fileViewMode === 'preview' }" @click="fileViewMode = 'preview'; iframeRendering = true">
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
      </div>

      <!-- 代码视图（PPT 不显示代码，base64 没有意义） -->
      <div v-if="fileViewMode === 'code' && !isPptFile" class="file-code-view">
        <pre class="file-code-pre"><code class="hljs" v-html="highlightedCode"></code></pre>
      </div>

      <!-- 预览视图：普通文件（HTML/SVG） -->
      <div v-if="fileViewMode === 'preview' && canPreview && !isPptFile" class="file-preview-view">
        <!-- iframe 渲染中：小电脑加载动画覆盖在上方 -->
        <Transition name="fade">
          <div v-if="iframeRendering" class="iframe-loading-overlay">
            <div class="bili-loader">
              <svg class="bili-pc-svg" width="80" height="64" viewBox="0 0 80 64">
                <rect x="12" y="2" width="56" height="38" rx="4" fill="#E3F6FD" stroke="#00AEEC" stroke-width="2"/>
                <circle cx="32" cy="18" r="3" fill="#00AEEC"><animate attributeName="r" values="3;2;3" dur="1.5s" repeatCount="indefinite"/></circle>
                <circle cx="48" cy="18" r="3" fill="#00AEEC"><animate attributeName="r" values="3;2;3" dur="1.5s" begin="0.1s" repeatCount="indefinite"/></circle>
                <path d="M35 27 Q40 32 45 27" fill="none" stroke="#00AEEC" stroke-width="1.5" stroke-linecap="round"/>
                <rect x="28" y="40" width="24" height="4" rx="1" fill="#00AEEC"/>
                <rect x="22" y="44" width="36" height="3" rx="1.5" fill="#00AEEC"/>
                <g class="bili-leg-left">
                  <line x1="32" y1="47" x2="28" y2="60" stroke="#00AEEC" stroke-width="2.5" stroke-linecap="round"/>
                  <ellipse cx="26" cy="61" rx="5" ry="2.5" fill="#00AEEC"/>
                </g>
                <g class="bili-leg-right">
                  <line x1="48" y1="47" x2="52" y2="60" stroke="#00AEEC" stroke-width="2.5" stroke-linecap="round"/>
                  <ellipse cx="54" cy="61" rx="5" ry="2.5" fill="#00AEEC"/>
                </g>
              </svg>
              <div class="bili-loader-text">页面渲染中<span class="bili-dot-anim"></span></div>
            </div>
          </div>
        </Transition>
        <iframe
          :srcdoc="previewSrcdoc"
          class="file-preview-frame"
          sandbox="allow-scripts allow-forms allow-modals allow-popups"
          @load="onIframeLoad"
        />
      </div>

      <!-- 文件加载中：Bilibili 小电脑颜文字风格 -->
      <!-- Bilibili 风格文件加载动画：SVG 小电脑 + 跑步小人 -->
      <div v-if="fileLoading" class="file-loading-view">
        <div class="bili-loader">
          <!-- SVG 小电脑 -->
          <svg class="bili-pc-svg" width="80" height="64" viewBox="0 0 80 64">
            <!-- 屏幕 -->
            <rect x="12" y="2" width="56" height="38" rx="4" fill="#E3F6FD" stroke="#00AEEC" stroke-width="2"/>
            <!-- 屏幕内眼睛 -->
            <circle cx="32" cy="18" r="3" fill="#00AEEC"><animate attributeName="r" values="3;2;3" dur="1.5s" repeatCount="indefinite"/></circle>
            <circle cx="48" cy="18" r="3" fill="#00AEEC"><animate attributeName="r" values="3;2;3" dur="1.5s" begin="0.1s" repeatCount="indefinite"/></circle>
            <!-- 嘴巴 -->
            <path d="M35 27 Q40 32 45 27" fill="none" stroke="#00AEEC" stroke-width="1.5" stroke-linecap="round"/>
            <!-- 底座 -->
            <rect x="28" y="40" width="24" height="4" rx="1" fill="#00AEEC"/>
            <rect x="22" y="44" width="36" height="3" rx="1.5" fill="#00AEEC"/>
            <!-- 左腿 -->
            <g class="bili-leg-left">
              <line x1="32" y1="47" x2="28" y2="60" stroke="#00AEEC" stroke-width="2.5" stroke-linecap="round"/>
              <ellipse cx="26" cy="61" rx="5" ry="2.5" fill="#00AEEC"/>
            </g>
            <!-- 右腿 -->
            <g class="bili-leg-right">
              <line x1="48" y1="47" x2="52" y2="60" stroke="#00AEEC" stroke-width="2.5" stroke-linecap="round"/>
              <ellipse cx="54" cy="61" rx="5" ry="2.5" fill="#00AEEC"/>
            </g>
          </svg>
          <div class="bili-loader-text">正在加载中<span class="bili-dot-anim"></span></div>
        </div>
      </div>

      <!-- PPT 视图（不依赖 fileViewMode，PPT 始终进入此分支） -->
      <!-- 无幻灯片数据：显示下载提示 -->
      <div v-if="!fileLoading && isPptFile && pptSlideCount === 0" class="ppt-empty-view">
        <div class="ppt-empty-icon">📊</div>
        <div class="ppt-empty-text">PPT 已生成</div>
        <div class="ppt-empty-sub">{{ selectedFile?.name }} · {{ fileSizeKb }} KB</div>
        <button class="ppt-empty-download" @click="downloadFile">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
            <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
          下载 PPT 文件
        </button>
      </div>

      <!-- 有幻灯片数据：走马灯浏览器 -->
      <div v-if="!fileLoading && isPptFile && pptSlideCount > 0" class="ppt-viewer">
        <el-carousel
          :initial-index="pptSlideIndex"
          :autoplay="false"
          trigger="click"
          arrow="always"
          indicator-position="outside"
          height="100%"
          class="ppt-carousel"
          @change="(idx: number) => { pptSlideIndex = idx }"
        >
          <el-carousel-item v-for="(slide, i) in pptSlides" :key="i" class="ppt-carousel-item">
            <div class="ppt-slide-container">
              <iframe
                :srcdoc="slide"
                class="ppt-slide-frame"
                sandbox="allow-scripts"
              />
            </div>
          </el-carousel-item>
        </el-carousel>
        <div class="ppt-nav-info-bar">
          <el-tag size="small" effect="plain" type="info" round>
            {{ pptSlideIndex + 1 }} / {{ pptSlideCount }}
          </el-tag>
        </div>
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
  background: var(--cf-card, #ffffff);
  border-radius: var(--cf-radius-lg, 18px);
  border: 1px solid var(--cf-border-soft, #EBF0F5);
  box-shadow: var(--cf-shadow-sm), var(--cf-shadow-glow, none);
  overflow: hidden;
}

/* Header */
.panel-hd {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 12px 0 0;
  background: rgba(250,250,252,0.92);
  backdrop-filter: blur(8px);
  -webkit-backdrop-filter: blur(8px);
  border-bottom: 1px solid #e8eaf2;
  flex-shrink: 0;
}
.hd-left { display: flex; align-items: center; gap: 0; min-width: 0; flex: 1; overflow: hidden; }

/* el-tabs Bilibili style overrides */
.panel-tabs {
  --el-tabs-header-height: 38px;
}
:deep(.panel-tabs .el-tabs__header) {
  margin: 0;
  border: none;
}
:deep(.panel-tabs .el-tabs__nav-wrap::after) {
  display: none;
}
:deep(.panel-tabs .el-tabs__active-bar) {
  background: #00AEEC;
  height: 2.5px;
  border-radius: 2px;
  transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.panel-tabs .el-tabs__item) {
  font-size: 12px;
  font-weight: 500;
  color: #9499A0;
  padding: 0 12px;
  height: 38px;
  line-height: 38px;
  transition: color 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.panel-tabs .el-tabs__item.is-active) {
  color: #00AEEC;
  font-weight: 600;
}
:deep(.panel-tabs .el-tabs__item:hover) {
  color: #00AEEC;
}

.tab-label-inner {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}
.tab-file-label {
  max-width: 150px;
}
.hd-progress-tag {
  font-size: 10px !important;
  height: 18px !important;
  line-height: 16px !important;
  padding: 0 6px !important;
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
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
  flex-shrink: 0;
}
.hd-tab-close:hover { background: rgba(242,93,89,0.1); color: #F25D59; }

/* Goal -- Bilibili style */
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

/* Loading skeleton + hint */
.loading-hint {
  flex: 1; display: flex; flex-direction: column;
  gap: 0; padding: 0;
}
.skeleton-plan-area {
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 12px;
  flex: 1;
}
.skeleton-header {
  margin-bottom: 4px;
}
.skeleton-body {
  /* let el-skeleton handle it */
}
.hint-bottom {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  padding: 12px 16px 16px;
  text-align: center;
}
.hint-icon-wrap {
  width: 40px; height: 40px; border-radius: 12px;
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
  margin: 0;
}
.hint-desc {
  font-size: 11.5px; color: #9499a0; line-height: 1.5;
  max-width: 200px; margin: 0;
}

/* AntV X6 canvas container */
.flow-canvas-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-height: 0;
  overflow: hidden;
  position: relative;
}

/* Dirty banner -- Bilibili style */
.dirty-banner {
  display: flex; align-items: center; justify-content: space-between;
  padding: 7px 10px;
  background: rgba(0,174,236,0.04); border: 1px solid rgba(0,174,236,0.15);
  border-radius: 10px; margin: 4px 8px 8px; gap: 8px;
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.dirty-left { display: flex; align-items: center; gap: 6px; }
.dirty-dot {
  width: 6px; height: 6px; border-radius: 50%; background: #00AEEC;
  animation: blink 1.2s ease-in-out infinite; flex-shrink: 0;
}
@keyframes blink { 0%,100%{opacity:1} 50%{opacity:0.3} }
.dirty-label { font-size: 11.5px; color: #0095CC; font-weight: 500; }
.dirty-right {
  flex-shrink: 0;
}

/* Reflection alert */
.ref-alert {
  flex-shrink: 0;
  margin: 0;
  border-radius: 0 !important;
  background: rgba(245,158,11,0.05) !important;
  border: none !important;
  border-top: 1px solid rgba(245,158,11,0.1) !important;
  padding: 6px 12px !important;
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.ref-alert .el-alert__content) {
  padding: 0;
}
:deep(.ref-alert .el-alert__icon) {
  display: none;
}
.ref-alert-content {
  display: flex;
  align-items: center;
  gap: 6px;
}
.ref-icon { font-size: 14px; flex-shrink: 0; }
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
/* Resize handle */
.trace-resize-handle {
  height: 10px;
  cursor: ns-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  background: transparent;
  transition: background 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.trace-resize-handle:hover { background: rgba(0,174,236,0.04); }
.trace-resize-bar {
  width: 36px;
  height: 3px;
  background: #E3E5E7;
  border-radius: 99px;
  transition: background 0.25s, width 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
.trace-resize-handle:hover .trace-resize-bar {
  background: #00AEEC;
  width: 48px;
}
.trace-hd { font-size: 10px; font-weight: 600; color: #9ca3af; padding: 2px 12px 3px; text-transform: uppercase; letter-spacing: 0.06em; }
.trace-body { flex: 1; overflow-y: auto; padding: 0 8px 6px; }
.trace-empty { font-size: 11px; color: #d1d5db; text-align: center; padding: 12px 0; }

/* Timeline trace */
.trace-timeline {
  padding: 4px 0 0 4px;
}
:deep(.trace-timeline .el-timeline-item) {
  padding-bottom: 4px;
}
:deep(.trace-timeline .el-timeline-item__wrapper) {
  padding-left: 18px;
  top: -2px;
}
:deep(.trace-timeline .el-timeline-item__node) {
  width: 8px;
  height: 8px;
  left: -1px;
}
:deep(.trace-timeline .el-timeline-item__tail) {
  left: 2px;
  border-left-width: 1.5px;
  border-color: #e5e7eb;
}

.trace-ic { font-size: 10px; margin-right: 4px; }
.trace-txt { font-size: 10.5px; color: #4b5563; line-height: 1.65; word-break: break-all; }

/* History timeline items */
.hist-content {
  display: flex;
  align-items: flex-start;
  gap: 6px;
}
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

/* Dialog footer */
.dialog-ft { display: flex; align-items: center; justify-content: space-between; gap: 8px; }

/* Animations */
.banner-enter-active, .banner-leave-active {
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.banner-enter-from, .banner-leave-to { opacity: 0; transform: translateY(-6px); }
.fadebar-enter-active, .fadebar-leave-active {
  transition: opacity 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.fadebar-enter-from, .fadebar-leave-to { opacity: 0; }

/* Tab content container */
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

/* File info with el-descriptions */
.file-info-section {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 12px;
  background: rgba(0,174,236,0.03);
  border-bottom: 1px solid rgba(0,174,236,0.07);
  flex-shrink: 0;
}
.file-descriptions {
  flex: 1;
}
:deep(.file-descriptions .el-descriptions__body) {
  background: transparent;
}
:deep(.file-descriptions .el-descriptions__label) {
  font-size: 10px;
  color: #9499A0;
  padding: 2px 6px;
  width: auto;
  min-width: auto;
  background: rgba(0,174,236,0.04);
}
:deep(.file-descriptions .el-descriptions__content) {
  font-size: 11px;
  padding: 2px 8px;
}
:deep(.file-descriptions .el-descriptions__cell) {
  border-color: rgba(0,174,236,0.08) !important;
}
.file-name-badge {
  font-size: 12px;
  font-weight: 600;
  color: #18191C;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.file-info-actions {
  flex-shrink: 0;
}

/* Action buttons bar */
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
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
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

/* Code view */
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

/* Preview view */
.file-preview-view {
  flex: 1;
  overflow: hidden;
  background: #fff;
  position: relative;
}
.file-preview-frame {
  width: 100%;
  height: 100%;
  border: none;
  display: block;
}
/* iframe 渲染中加载遮罩 */
.iframe-loading-overlay {
  position: absolute;
  inset: 0;
  z-index: 10;
  background: var(--cf-card, #fff);
  display: flex;
  align-items: center;
  justify-content: center;
}
.fade-enter-active { transition: opacity 0.2s; }
.fade-leave-active { transition: opacity 0.4s; }
.fade-enter-from, .fade-leave-to { opacity: 0; }

/* File loading view */
.file-loading-view {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center;
  background: #F8F9FA;
}
.bili-loader {
  display: flex; flex-direction: column; align-items: center; gap: 16px;
}
.bili-pc-svg {
  animation: bili-pc-hop 0.6s ease-in-out infinite;
}
@keyframes bili-pc-hop {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
.bili-leg-left {
  animation: bili-run-l 0.3s ease-in-out infinite alternate;
  transform-origin: 32px 47px;
}
.bili-leg-right {
  animation: bili-run-r 0.3s ease-in-out infinite alternate;
  transform-origin: 48px 47px;
}
@keyframes bili-run-l {
  0% { transform: rotate(-15deg); }
  100% { transform: rotate(15deg); }
}
@keyframes bili-run-r {
  0% { transform: rotate(15deg); }
  100% { transform: rotate(-15deg); }
}
.bili-loader-text {
  font-size: 13px; color: var(--cf-text-3, #9499A0);
}
.bili-dot-anim::after {
  content: '';
  animation: bili-dots 1.5s steps(3, end) infinite;
}
@keyframes bili-dots {
  0% { content: ''; }
  33% { content: '.'; }
  66% { content: '..'; }
  100% { content: '...'; }
}

/* PPT empty download view */
.ppt-empty-view {
  flex: 1; display: flex; flex-direction: column;
  align-items: center; justify-content: center; gap: 10px;
  background: #F8F9FA; padding: 40px;
}
.ppt-empty-icon { font-size: 48px; }
.ppt-empty-text { font-size: 16px; font-weight: 600; color: #18191C; }
.ppt-empty-sub { font-size: 13px; color: #9499A0; }
.ppt-empty-download {
  display: inline-flex; align-items: center; gap: 6px;
  margin-top: 10px; padding: 10px 24px; border-radius: 10px;
  background: #FF9800; color: #fff; border: none; cursor: pointer;
  font-size: 14px; font-weight: 600;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
.ppt-empty-download:hover { background: #F57C00; transform: translateY(-1px); box-shadow: 0 4px 12px rgba(255,152,0,0.3); }

/* PPT carousel viewer */
.ppt-viewer {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: #F1F2F3;
}
.ppt-carousel {
  flex: 1;
}
:deep(.ppt-carousel .el-carousel__container) {
  height: 100% !important;
}
:deep(.ppt-carousel .el-carousel__arrow) {
  background: rgba(255,255,255,0.9);
  color: #18191C;
  border: 1.5px solid #E3E5E7;
  width: 36px;
  height: 36px;
  font-size: 14px;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
}
:deep(.ppt-carousel .el-carousel__arrow:hover) {
  border-color: #00AEEC;
  color: #00AEEC;
  box-shadow: 0 2px 12px rgba(0,174,236,0.2);
}
:deep(.ppt-carousel .el-carousel__indicators) {
  bottom: 8px;
}
:deep(.ppt-carousel .el-carousel__button) {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: #d1d5db;
  opacity: 1;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.ppt-carousel .el-carousel__indicator.is-active .el-carousel__button) {
  background: #00AEEC;
  width: 20px;
  border-radius: 4px;
}
.ppt-carousel-item {
  display: flex;
  align-items: center;
  justify-content: center;
}
.ppt-slide-container {
  width: 100%;
  height: 100%;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  overflow: hidden;
}
.ppt-slide-frame {
  width: 100%;
  height: 100%;
  border: none;
  border-radius: 8px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.12);
  background: #fff;
}
.ppt-nav-info-bar {
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 6px 0 10px;
}
</style>
