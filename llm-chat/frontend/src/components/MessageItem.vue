<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted } from 'vue'
import { Marked } from 'marked'
import hljs from 'highlight.js/lib/common'
import type { Message, ToolCallRecord, StepRecord, AgentStatus, CognitiveState } from '../types'
import { makeEmptyCognitiveState } from '../types'
import { CopyDocument, Check, Search, Clock, Cpu, Document, ArrowDown, Loading, Close } from '@element-plus/icons-vue'
import CodePreview from './CodePreview.vue'
import AgentStatusBubble from './AgentStatusBubble.vue'

const PREVIEWABLE = new Set(['html','svg','css','javascript','js','typescript','ts','vue','jsx','tsx','react'])

// ─── 工具元信息 ───
const TOOL_META: Record<string, { label: string; icon: any; color: string }> = {
  web_search:        { label: '搜索了网络',  icon: Search,   color: '#6366f1' },
  fetch_webpage:     { label: '阅读了网页',  icon: Document, color: '#0ea5e9' },
  get_current_time:  { label: '获取了时间',  icon: Clock,    color: '#0ea5e9' },
  calculator:        { label: '执行了计算',  icon: Cpu,      color: '#10b981' },
}
function toolMeta(name: string) {
  return TOOL_META[name] ?? { label: `调用了 ${name}`, icon: Cpu, color: '#6b7280' }
}
function faviconUrl(url: string) {
  try { return `https://www.google.com/s2/favicons?domain=${new URL(url).hostname}&sz=16` }
  catch { return '' }
}

// ─── Marked + highlight.js 实例 ───
function buildCodeHtml(rawToken: any): string {
  const text: string = typeof rawToken === 'object' && rawToken !== null
    ? (rawToken.text ?? '')
    : String(rawToken ?? '')
  const lang: string = typeof rawToken === 'object' && rawToken !== null
    ? (rawToken.lang ?? '')
    : ''

  const rawLang = lang.trim().toLowerCase()
  // Content-based fallback: if no lang, detect HTML/SVG by content
  const detectedLang = rawLang || (
    /^\s*<!doctype\s+html/i.test(text) || /^\s*<html[\s>]/i.test(text) ? 'html'
    : /^\s*<svg[\s>]/i.test(text) ? 'svg'
    : ''
  )
  const language = detectedLang || 'plaintext'

  let highlighted: string
  try {
    if (hljs.getLanguage(language)) {
      highlighted = hljs.highlight(text, { language, ignoreIllegals: true }).value
    } else {
      highlighted = hljs.highlightAuto(text).value
    }
  } catch {
    highlighted = text
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
  }

  const isPreviewable = PREVIEWABLE.has(language)
  const encoded = encodeURIComponent(text)

  const previewBtn = isPreviewable
    ? `<button class="cb-btn cb-preview" data-code="${encoded}" data-lang="${language}" title="在沙盒中预览渲染效果">
        <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
          <path d="M1 8s3-5 7-5 7 5 7 5-3 5-7 5-7-5-7-5z" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/>
          <circle cx="8" cy="8" r="2.5" stroke="currentColor" stroke-width="1.5"/>
        </svg>
        <span class="cb-text">预览</span>
      </button>`
    : ''

  return `<div class="code-block">
    <div class="code-header">
      <span class="code-lang-badge">${language}</span>
      <div class="code-action-row">
        ${previewBtn}
        <button class="cb-btn cb-copy" data-code="${encoded}" title="复制代码">
          <svg width="11" height="11" viewBox="0 0 16 16" fill="none">
            <rect x="5.5" y="5.5" width="8" height="9" rx="1.5" stroke="currentColor" stroke-width="1.4"/>
            <path d="M3 10.5V3a1 1 0 011-1h7.5" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>
          </svg>
          <span class="cb-text">复制</span>
        </button>
      </div>
    </div>
    <pre class="code-pre"><code class="hljs">${highlighted}</code></pre>
  </div>`
}

const markedInstance = new Marked({ gfm: true, breaks: false })

// marked v13 breaking change：use({ renderer }) 钩子收到的是已渲染 HTML 字符串，
// 不再是 token 对象，因此 code/html 用 renderer 钩子，
// table 必须改用 extensions API 才能拿到 token 对象。
markedInstance.use({
  renderer: {
    code(token: any): string {
      return buildCodeHtml(token)
    },

    // 拦截 marked 识别出的 HTML 块，防止原始 HTML 直接渲染到 DOM
    // 场景：模型输出 <!DOCTYPE html>... 不包裹代码围栏时，marked 会把它当 HTML 块直接透传
    html(token: any): string {
      const raw: string = (token.raw ?? token.text ?? '').trim()
      // 完整 HTML 页面 → 以代码块形式展示
      if (/^<!doctype\s+html/i.test(raw) || /^<html[\s>]/i.test(raw)) {
        return buildCodeHtml({ text: raw, lang: 'html' })
      }
      // 其他 raw HTML（如 <br>、<details> 等）→ 转义为可见文本，不执行
      return raw
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
    },
  }
})

// table 用 extensions API 注入，extensions 的 renderer 收到 token 对象（v13 兼容）
markedInstance.use({
  extensions: [{
    name: 'table',
    level: 'block' as const,
    renderer(token: any): string {
      const header = token.header ?? []
      const rows   = token.rows   ?? []
      const align  = token.align  ?? []

      // parseInline 将单元格内的 **加粗**、`code`、[链接] 等渲染成 HTML
      const parseCell = (cell: any): string => {
        const raw = typeof cell === 'object' ? (cell.text ?? '') : String(cell)
        return markedInstance.parseInline(raw) as string
      }

      const thCells = header.map((cell: any, i: number) => {
        const a = align[i]
        const style = a ? ` style="text-align:${a}"` : ''
        return `<th${style}>${parseCell(cell)}</th>`
      }).join('')

      const bodyRows = rows.map((row: any[]) => {
        const tds = row.map((cell: any, i: number) => {
          const a = align[i]
          const style = a ? ` style="text-align:${a}"` : ''
          return `<td${style}>${parseCell(cell)}</td>`
        }).join('')
        return `<tr>${tds}</tr>`
      }).join('\n')

      return `<div class="table-wrapper"><table><thead><tr>${thCells}</tr></thead><tbody>${bodyRows}</tbody></table></div>`
    },
  }],
})

// ─── Props ───
const props = defineProps<{
  message: Message
  isLastLoading?: boolean
  agentStatus?: AgentStatus
  cognitive?: CognitiveState
}>()

const emptyCognitive = makeEmptyCognitiveState()

// ─── 统一渲染段落：多步模式每步一段，普通模式一段 ───────────────────────────
interface Section {
  step: StepRecord | null   // null = 普通无步骤消息
  toolCalls: ToolCallRecord[]
  thinking: string
  content: string
}
const sections = computed<Section[]>(() => {
  if (props.message.role !== 'assistant') return []
  if (props.message.steps?.length) {
    return props.message.steps.map(step => ({
      step,
      toolCalls: step.toolCalls,
      thinking: step.thinking,
      content: step.content,
    }))
  }
  return [{
    step: null,
    toolCalls: props.message.toolCalls ?? [],
    thinking: props.message.thinking ?? '',
    content: props.message.content ?? '',
  }]
})

// ─── Markdown 内容渲染（去除 think 块残留） ─────────────────────────────────
function renderContent(raw: string): string {
  let content = raw.replace(/<think>[\s\S]*?<\/think>\n*/g, '')
  const thinkStart = content.indexOf('<think>')
  if (thinkStart !== -1) content = content.slice(0, thinkStart)
  let trimmed = content.trim()

  // 流式安全：补全未闭合的代码围栏（防止 HTML 在 stream 中途泄露到 DOM）
  const fenceCount = (trimmed.match(/^```/gm) || []).length
  if (fenceCount % 2 !== 0) {
    trimmed = trimmed + '\n```'
  }

  // 防止 --- 被 marked 误解析为 setext h2 标题（在 --- 前插入空行）
  trimmed = trimmed.replace(/([^\n]+)\n(-{3,})(\n|$)/g, '$1\n\n$2$3')

  // 模型直接输出裸 HTML 页面时（没有 markdown 代码块包裹），自动包裹为 html 代码块渲染
  if (/^<!doctype\s+html/i.test(trimmed) || /^<html[\s>]/i.test(trimmed)) {
    return buildCodeHtml({ text: trimmed, lang: 'html' })
  }
  return markedInstance.parse(trimmed) as string
}

// ─── 代码块事件委托（挂载在整个 ai-content-wrap 上） ────────────────────────
const contentWrapEl = ref<HTMLElement>()
const previewVisible = ref(false)
const previewCode = ref('')
const previewLang = ref('html')

function handleContentClick(e: MouseEvent) {
  const copyBtn = (e.target as Element).closest<HTMLElement>('.cb-copy')
  const previewBtn = (e.target as Element).closest<HTMLElement>('.cb-preview')

  if (copyBtn) {
    e.stopPropagation()
    const code = decodeURIComponent(copyBtn.dataset.code || '')
    navigator.clipboard.writeText(code).catch(() => {})
    const span = copyBtn.querySelector<HTMLElement>('.cb-text')
    if (span) {
      span.textContent = '已复制'
      copyBtn.classList.add('cb-done')
      setTimeout(() => {
        span.textContent = '复制'
        copyBtn.classList.remove('cb-done')
      }, 2000)
    }
    return
  }

  if (previewBtn) {
    e.stopPropagation()
    previewCode.value = decodeURIComponent(previewBtn.dataset.code || '')
    previewLang.value = previewBtn.dataset.lang || 'html'
    previewVisible.value = true
  }
}

onMounted(() => contentWrapEl.value?.addEventListener('click', handleContentClick))
onUnmounted(() => contentWrapEl.value?.removeEventListener('click', handleContentClick))

// ─── 整条消息复制 ───
const copied = ref(false)
async function copy() {
  try {
    const text = props.message.steps?.length
      ? props.message.steps.map(s => s.content).filter(Boolean).join('\n\n')
      : props.message.content
    await navigator.clipboard.writeText(text)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch {}
}

// ─── 推理过程折叠（key = sectionIndex） ────────────────────────────────────
const thinkExpanded = ref<Record<number, boolean>>({})
function toggleThink(si: number) { thinkExpanded.value[si] = !thinkExpanded.value[si] }
function isThinkExpanded(si: number) { return thinkExpanded.value[si] ?? false }

// ─── 工具折叠（key = "sectionIndex-toolIndex"） ─────────────────────────────
const collapsed = ref<Record<string, boolean>>({})
function toggle(si: number, ti: number) {
  const k = `${si}-${ti}`
  collapsed.value[k] = !collapsed.value[k]
}
function isCollapsed(si: number, ti: number) { return collapsed.value[`${si}-${ti}`] ?? false }

// 工具执行完成后 1.2s 自动折叠
watch(
  () => sections.value.flatMap((sec, si) =>
    sec.toolCalls.map((tc, ti) => ({ si, ti, done: tc.done }))
  ),
  (items, prev) => {
    items.forEach((item, idx) => {
      if (item.done && !prev?.[idx]?.done) {
        setTimeout(() => { collapsed.value[`${item.si}-${item.ti}`] = true }, 1200)
      }
    })
  },
  { deep: true }
)

// ─── 整条消息是否有可复制内容 ───────────────────────────────────────────────
const hasContent = computed(() => {
  if (props.message.steps?.length) return props.message.steps.some(s => s.content)
  return !!props.message.content
})
</script>

<template>
  <div class="msg" :class="message.role">

    <!-- 用户消息 -->
    <template v-if="message.role === 'user'">
      <div class="user-wrap">
        <div v-if="message.images?.length" class="user-imgs">
          <el-image
            v-for="(img, i) in message.images"
            :key="i"
            :src="img"
            :preview-src-list="message.images"
            :initial-index="i"
            fit="cover"
            class="user-img"
          />
        </div>

        <!-- Workflow plan card -->
        <div v-if="message.workflowPlan?.length" class="wf-card">
          <div class="wf-card-header">
            <div class="wf-card-badge">
              <svg width="10" height="10" viewBox="0 0 24 24" fill="none">
                <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="currentColor"/>
              </svg>
              工作流执行
            </div>
            <span class="wf-card-count">{{ message.workflowPlan.length }} 步</span>
          </div>
          <div v-if="message.workflowGoal" class="wf-card-goal">{{ message.workflowGoal }}</div>
          <div class="wf-card-steps">
            <div v-for="(step, i) in message.workflowPlan.slice(0, 7)" :key="i" class="wf-card-step">
              <span class="wf-step-num">{{ i + 1 }}</span>
              <span class="wf-step-title">{{ step.title }}</span>
            </div>
            <div v-if="message.workflowPlan.length > 7" class="wf-card-more">
              +{{ message.workflowPlan.length - 7 }} 个步骤
            </div>
          </div>
        </div>

        <!-- Plain text bubble -->
        <div v-else-if="message.content" class="user-bubble">{{ message.content }}</div>
      </div>
      <div class="user-avatar">
        <svg width="17" height="17" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="7.5" r="3.8" fill="#374151"/>
          <path d="M4.5 20.5C4.5 16.9 7.9 14 12 14C16.1 14 19.5 16.9 19.5 20.5" stroke="#374151" stroke-width="2" stroke-linecap="round" fill="none"/>
        </svg>
      </div>
    </template>

    <!-- AI 消息 -->
    <template v-else>
      <div class="ai-avatar" :class="{ 'ai-avatar--breathing': isLastLoading }">
        <!-- 与 favicon / logo 一致：主星 + 副星 -->
        <svg width="17" height="17" viewBox="0 0 32 32" fill="none">
          <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#111827"/>
          <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#111827" opacity="0.5"/>
        </svg>
      </div>

      <!-- ai-content-wrap 挂载代码块点击委托 -->
      <div class="ai-content-wrap" ref="contentWrapEl">

        <!-- ── 加载中且尚无内容：内联状态气泡（与头像同行，保持对齐） ── -->
        <AgentStatusBubble
          v-if="isLastLoading && !message.content && agentStatus && agentStatus.state !== 'idle'"
          :status="agentStatus"
          :cognitive="cognitive ?? emptyCognitive"
        />

        <!-- ── 统一渲染段落（步骤或普通消息） ── -->
        <div
          v-for="(sec, si) in sections" :key="si"
          :class="['section-wrap', { 'has-step': !!sec.step, 'step-done': sec.step?.status === 'done', 'step-running': sec.step?.status === 'running', 'step-failed': sec.step?.status === 'failed' }]"
        >

          <!-- 步骤标题行（多步时显示） -->
          <div v-if="sec.step" class="step-hdr">
            <div class="step-badge" :class="sec.step.status">
              <el-icon v-if="sec.step.status === 'done'" class="step-icon"><Check /></el-icon>
              <el-icon v-else-if="sec.step.status === 'running'" class="step-icon step-spin"><Loading /></el-icon>
              <el-icon v-else-if="sec.step.status === 'failed'" class="step-icon"><Close /></el-icon>
              <span v-else class="step-num">{{ si + 1 }}</span>
            </div>
            <span class="step-title">{{ sec.step.title }}</span>
          </div>

          <!-- 工具调用块 -->
          <div v-if="sec.toolCalls.length" class="tool-calls">
            <div v-for="(tc, ti) in sec.toolCalls" :key="ti"
                 :class="['tool-block', (tc.name === 'web_search' || tc.name === 'fetch_webpage') ? 'tool-block-sources' : '']">

              <!-- web_search -->
              <template v-if="tc.name === 'web_search'">
                <div class="tool-header tool-header-flat">
                  <span class="tool-status-icon">
                    <svg v-if="tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                      <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                      <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                    </svg>
                  </span>
                  <el-icon style="font-size:13px; color:#6366f1"><Search /></el-icon>
                  <span class="tool-label">搜索了网络</span>
                  <span class="tool-query">「{{ (tc.input as any).query }}」</span>
                  <span v-if="!tc.done" class="tool-pending">搜索中...</span>
                </div>
                <div v-if="tc.searchItems?.length" class="search-url-list">
                  <a v-for="(item, ii) in tc.searchItems" :key="ii"
                     :href="item.url" target="_blank"
                     class="search-url-row" :title="item.title || item.url">
                    <span class="url-status">
                      <svg v-if="item.status === 'done'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                        <circle cx="8" cy="8" r="6" stroke="#22c55e" stroke-width="1.5"/>
                        <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.4" stroke-linecap="round"/>
                      </svg>
                      <svg v-else-if="item.status === 'fail'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                        <circle cx="8" cy="8" r="6" stroke="#ef4444" stroke-width="1.5"/>
                        <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#ef4444" stroke-width="1.4" stroke-linecap="round"/>
                      </svg>
                      <svg v-else width="12" height="12" viewBox="0 0 16 16" fill="none" class="spin">
                        <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                      </svg>
                    </span>
                    <img :src="faviconUrl(item.url)" class="url-favicon"
                         @error="($event.target as HTMLImageElement).style.display='none'" />
                    <span class="url-text">{{ item.url }}</span>
                  </a>
                </div>
              </template>

              <!-- fetch_webpage -->
              <template v-else-if="tc.name === 'fetch_webpage'">
                <div class="tool-header tool-header-flat">
                  <span class="tool-status-icon">
                    <svg v-if="tc.done && tc.fetchStatus === 'fail'" width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="6.5" stroke="#ef4444" stroke-width="1.5"/>
                      <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#ef4444" stroke-width="1.4" stroke-linecap="round"/>
                    </svg>
                    <svg v-else-if="tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                      <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                      <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                    </svg>
                  </span>
                  <el-icon style="font-size:13px; color:#0ea5e9"><Document /></el-icon>
                  <span class="tool-label">{{ tc.done && tc.fetchStatus === 'fail' ? '读取失败' : (tc.done ? '读取了网页' : '正在阅读') }}</span>
                  <span v-if="(tc.input as any).url" class="tool-query">
                    <a :href="(tc.input as any).url" target="_blank" class="fetch-url-link" @click.stop>
                      <img :src="faviconUrl((tc.input as any).url)" class="url-favicon" style="margin-right:3px"
                           @error="($event.target as HTMLImageElement).style.display='none'" />
                      {{ (tc.input as any).url }}
                    </a>
                  </span>
                  <span v-if="!tc.done" class="tool-pending">读取中...</span>
                </div>
              </template>

              <!-- 其他工具 -->
              <template v-else>
                <div class="tool-header" @click="toggle(si, ti)">
                  <span class="tool-status-icon">
                    <svg v-if="tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                      <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                    </svg>
                    <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                      <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                    </svg>
                  </span>
                  <el-icon :style="{ color: toolMeta(tc.name).color }" style="font-size:13px">
                    <component :is="toolMeta(tc.name).icon" />
                  </el-icon>
                  <span class="tool-label">{{ toolMeta(tc.name).label }}</span>
                  <span v-if="!tc.done" class="tool-pending">执行中...</span>
                  <span class="tool-chevron" :class="{ open: !isCollapsed(si, ti) }">›</span>
                </div>
                <Transition name="slide">
                  <div v-show="!isCollapsed(si, ti)" class="tool-body">
                    <div class="tool-output-plain">
                      <span class="tool-tag">结果</span>
                      <span>{{ tc.output }}</span>
                    </div>
                  </div>
                </Transition>
              </template>

            </div>
          </div>

          <!-- 推理过程（可折叠白卡） -->
          <div v-if="sec.thinking" class="think-block">
            <button class="think-toggle" @click="toggleThink(si)">
              <span class="think-hd-left">
                <!-- 旋转星星图标（内容生成中）或静态图标（已完成） -->
                <svg v-if="!sec.content" class="think-spin-icon" width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="#7c3aed"/>
                  <path d="M19.5 4C19.5 4 20.1 6.6 22 7.5C20.1 8.4 19.5 11 19.5 11C19.5 11 18.9 8.4 17 7.5C18.9 6.6 19.5 4 19.5 4Z" fill="#7c3aed" opacity="0.4"/>
                </svg>
                <svg v-else width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="#7c3aed" opacity="0.6"/>
                </svg>
                <span class="think-title">{{ sec.content ? '推理过程' : '思考中...' }}</span>
              </span>
              <span class="think-hd-right">
                <span class="think-len">{{ sec.thinking.length > 50 ? (sec.thinking.length / 1000).toFixed(1) + 'k 字' : '' }}</span>
                <svg class="think-chevron" :class="{ expanded: isThinkExpanded(si) }" width="12" height="12" viewBox="0 0 16 16" fill="currentColor">
                  <path d="M4 6l4 4 4-4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
                </svg>
              </span>
            </button>
            <Transition name="think-slide">
              <div v-if="isThinkExpanded(si)" class="think-body">{{ sec.thinking }}</div>
            </Transition>
          </div>

          <!-- Markdown 内容 -->
          <div v-if="sec.content" class="ai-content markdown-body" v-html="renderContent(sec.content)"></div>

        </div>
        <!-- /sections -->

        <!-- 操作行 -->
        <div v-if="hasContent" class="ai-actions">
          <el-tooltip :content="copied ? '已复制！' : '复制内容'" placement="top" :show-after="300">
            <button class="action-btn" :class="{ copied }" @click="copy">
              <el-icon><component :is="copied ? Check : CopyDocument" /></el-icon>
              <span>{{ copied ? '已复制' : '复制' }}</span>
            </button>
          </el-tooltip>
        </div>
      </div>
    </template>

  </div>

  <!-- 代码预览弹窗 -->
  <CodePreview
    v-model="previewVisible"
    :code="previewCode"
    :lang="previewLang"
  />
</template>

<style scoped>
.msg {
  width: 100%;
  padding: 10px 0;
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

/* 用户 */
.msg.user { flex-direction: row-reverse; }
.user-avatar {
  width: 34px; height: 34px;
  border-radius: 50%;
  background: #f4f4f5;
  border: 1.5px solid #e4e4e7;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07);
}
.user-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  max-width: 68%;
}
.user-imgs { display: flex; flex-wrap: wrap; gap: 6px; justify-content: flex-end; }
.user-img {
  width: 200px; height: 200px;
  border-radius: var(--cf-radius-md) !important;
  border: 1.5px solid var(--cf-border);
  cursor: zoom-in;
}
.user-bubble {
  background: #f4f4f4;
  color: #0d0d0d;
  padding: 11px 18px;
  border-radius: 18px;
  font-size: 14.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: none;
  letter-spacing: -0.1px;
}

/* ── Workflow plan card ── */
.wf-card {
  background: #fff;
  border: 1.5px solid #e0e7ff;
  border-radius: 14px;
  overflow: hidden;
  max-width: 320px;
  box-shadow: 0 2px 10px rgba(99,102,241,0.08);
}
.wf-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px 8px;
  background: linear-gradient(135deg, #eef2ff 0%, #f5f3ff 100%);
  border-bottom: 1px solid #e0e7ff;
}
.wf-card-badge {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 700;
  color: #4f46e5;
}
.wf-card-count {
  font-size: 11px;
  font-weight: 600;
  color: #8b5cf6;
  background: rgba(139,92,246,0.1);
  padding: 1px 7px;
  border-radius: 10px;
}
.wf-card-goal {
  padding: 7px 12px 5px;
  font-size: 12.5px;
  color: #374151;
  line-height: 1.45;
  border-bottom: 1px solid #f3f4f6;
  font-weight: 500;
}
.wf-card-steps {
  padding: 6px 0 4px;
  display: flex;
  flex-direction: column;
  gap: 0;
}
.wf-card-step {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 4px 12px;
  transition: background 0.12s;
}
.wf-card-step:hover { background: #f9fafb; }
.wf-step-num {
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #eef2ff;
  border: 1px solid #c7d2fe;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9.5px;
  font-weight: 700;
  color: #6366f1;
  flex-shrink: 0;
}
.wf-step-title {
  font-size: 12px;
  color: #374151;
  line-height: 1.4;
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.wf-card-more {
  padding: 3px 12px 5px;
  font-size: 11px;
  color: #9ca3af;
  display: flex;
  align-items: center;
  gap: 4px;
}
.wf-card-more::before {
  content: '';
  display: inline-block;
  width: 18px;
  height: 1px;
  background: #e5e7eb;
  flex-shrink: 0;
}

/* AI */
.msg.assistant { flex-direction: row; }
.ai-avatar {
  width: 34px; height: 34px;
  border-radius: 10px;          /* 改为圆角方形，与 logo/favicon 风格统一 */
  background: #ffffff;
  border: 1.5px solid #e4e4e7;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07);
  transition: box-shadow 0.3s, border-color 0.3s;
}
.msg.assistant:hover .ai-avatar {
  border-color: #d1d5db;
  box-shadow: 0 2px 8px rgba(0,0,0,0.10);
}
/* 呼吸动画 — 思考/加载中 */
.ai-avatar--breathing {
  animation: ai-breathe 1.5s ease-in-out infinite !important;
  border-color: #6B9EFF !important;
  background: #f0f6ff !important;
}
@keyframes ai-breathe {
  0%, 100% {
    box-shadow: 0 0 0 0 rgba(107,158,255,0.3), 0 1px 4px rgba(0,0,0,0.07);
    transform: scale(1);
  }
  50% {
    box-shadow: 0 0 0 5px rgba(107,158,255,0), 0 1px 4px rgba(107,158,255,0.15);
    transform: scale(1.04);
  }
}
.ai-content-wrap {
  flex: 1;
  min-width: 0;
  max-width: 86%;
}
.ai-content {
  font-size: 14.5px;
  line-height: 1.75;
  color: var(--cf-text-1);
  letter-spacing: -0.1px;
}
/* think-block 在多步骤模式内的间距 */
.section-wrap.has-step .think-block { margin-bottom: 8px; }

/* ── 多步骤段落 ── */
.section-wrap {
  display: flex;
  flex-direction: column;
  gap: 0;
}
.section-wrap.has-step {
  border-left: 2px solid #e5e7eb;
  padding-left: 14px;
  margin-bottom: 16px;
  padding-bottom: 4px;
}
.section-wrap.has-step.step-running { border-left-color: #93c5fd; }
.section-wrap.has-step.step-done    { border-left-color: #6ee7b7; }
.section-wrap.has-step.step-failed  { border-left-color: #fca5a5; }

/* 步骤标题行 */
.step-hdr {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
}
.step-badge {
  width: 20px; height: 20px;
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  font-size: 10px; font-weight: 700;
  transition: all .2s;
}
.step-badge.pending { background: #f3f4f6; color: #9ca3af; border: 1.5px solid #e5e7eb; }
.step-badge.running { background: #dbeafe; color: #2563eb; border: 1.5px solid #93c5fd; }
.step-badge.done    { background: #d1fae5; color: #059669; border: 1.5px solid #6ee7b7; }
.step-badge.failed  { background: #fee2e2; color: #dc2626; border: 1.5px solid #fca5a5; }
.step-icon  { font-size: 11px; }
.step-spin  { animation: spin 1.1s linear infinite; }
.step-num   { font-size: 10px; line-height: 1; }
.step-title {
  font-size: 12.5px; font-weight: 600; color: #374151;
  flex: 1; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
}
.section-wrap.step-running .step-title { color: #1d4ed8; }
.section-wrap.step-done    .step-title { color: #6b7280; }
.section-wrap.step-failed  .step-title { color: #dc2626; }

/* ── 工具调用 ── */
.tool-calls { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.tool-block {
  border: 1px solid var(--cf-border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--cf-card);
  box-shadow: var(--cf-shadow-xs);
}
/* 胶囊悬浮提示风格（搜索/读取工具） */
.tool-block-sources {
  border: none;
  background: transparent;
  box-shadow: none;
}
.tool-block-sources .tool-header-flat {
  background: #fff;
  border: 1px solid #e0e7ff;
  border-left: 3px solid #a5b4fc;
  border-radius: 10px;
  padding: 6px 12px 6px 10px;
  box-shadow: 0 1px 6px rgba(99,102,241,0.06);
  transition: box-shadow 0.2s, border-color 0.2s, transform 0.2s ease-out;
  gap: 7px;
}
.tool-block-sources .tool-header-flat:hover {
  border-color: #c4b5fd;
  border-left-color: #6366f1;
  box-shadow: 0 3px 12px rgba(99,102,241,0.12);
  transform: translateY(-1px);
}
.tool-header {
  display: flex; align-items: center; gap: 7px;
  padding: 9px 14px;
  cursor: pointer; user-select: none;
  font-size: 13px; color: var(--cf-text-2);
  transition: background 0.15s;
}
.tool-header:hover { background: var(--cf-active); }
.tool-header-flat { cursor: default; padding: 4px 2px; gap: 6px; }
.tool-header-flat:hover { background: transparent; }
.tool-status-icon { display: flex; align-items: center; flex-shrink: 0; }
.tool-label { font-weight: 600; color: var(--cf-text-1); }
.tool-query {
  color: var(--cf-text-3); font-size: 12px;
  flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
.tool-pending { font-size: 11px; color: var(--cf-text-4); animation: blink-text 1.2s ease-in-out infinite; }
@keyframes blink-text { 0%,100%{opacity:1} 50%{opacity:0.3} }
.tool-chevron {
  font-size: 16px; color: var(--cf-text-4);
  transform: rotate(90deg); transition: transform 0.25s;
  line-height: 1; flex-shrink: 0;
}
.tool-chevron.open { transform: rotate(-90deg); }
.tool-body {
  border-top: 1px solid var(--cf-border);
  padding: 8px 10px 10px;
  display: flex; flex-direction: column; gap: 4px;
  max-height: 340px; overflow-y: auto;
}

/* 搜索 URL 列表 */
.search-url-list { display: flex; flex-direction: column; gap: 2px; padding: 2px 4px 6px 24px; }
.search-url-row {
  display: flex; align-items: center; gap: 6px;
  padding: 3px 8px 3px 4px; border-radius: 6px;
  text-decoration: none; color: var(--cf-text-2); font-size: 12px;
  transition: background 0.15s;
  animation: fade-row 0.2s ease both;
  min-width: 0;
}
.search-url-row:hover { background: var(--cf-active); color: #4f46e5; }
@keyframes fade-row { from { opacity: 0; transform: translateX(-4px); } to { opacity: 1; transform: translateX(0); } }
.url-status { display: flex; align-items: center; flex-shrink: 0; }
.url-favicon { width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }
.url-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--cf-text-3); font-size: 11.5px; }
.fetch-url-link {
  display: inline-flex; align-items: center; gap: 4px;
  color: var(--cf-text-3); text-decoration: none; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px;
}
.fetch-url-link:hover { color: #4f46e5; }

/* 普通工具输出 */
.tool-output-plain { display: flex; align-items: flex-start; gap: 8px; font-size: 12.5px; padding: 4px 2px; color: var(--cf-text-3); }
.tool-tag {
  flex-shrink: 0; padding: 1px 7px; border-radius: 5px;
  font-size: 11px; font-weight: 600;
  background: #eef2ff; color: #4f46e5; border: 1px solid #e0e7ff;
}

/* 折叠动画 */
.slide-enter-active, .slide-leave-active { transition: max-height 0.3s ease, opacity 0.25s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { max-height: 0; opacity: 0; }
.slide-enter-to, .slide-leave-from { max-height: 400px; opacity: 1; }

@keyframes spin { to { transform: rotate(360deg); } }
.spin { animation: spin 1s linear infinite; transform-origin: center; }

/* 操作行 */
.ai-actions { display: flex; gap: 4px; margin-top: 8px; opacity: 0; transition: opacity 0.2s; }
.msg.assistant:hover .ai-actions { opacity: 1; }
.action-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 10px;
  background: var(--cf-card); border: 1.5px solid var(--cf-border);
  border-radius: 8px; color: var(--cf-text-4);
  font-size: 12px; font-weight: 500; font-family: inherit;
  cursor: pointer; transition: all 0.15s;
}
.action-btn:hover { border-color: #a5b4fc; color: var(--cf-indigo); background: var(--cf-active); }
.action-btn.copied { border-color: #bbf7d0; color: #16a34a; background: #f0fdf4; }

</style>

<style>
/* ── Markdown 全局样式 ── */
.markdown-body { word-break: break-word; }
.markdown-body p { margin: 0 0 10px; }
.markdown-body p:last-child { margin-bottom: 0; }

.markdown-body h1, .markdown-body h2, .markdown-body h3 {
  font-weight: 700; margin: 20px 0 8px; line-height: 1.3;
  color: #111827; letter-spacing: -0.3px;
}
.markdown-body h1 { font-size: 1.4em; }
.markdown-body h2 { font-size: 1.2em; border-bottom: 1px solid #e4e6ef; padding-bottom: 6px; }
.markdown-body h3 { font-size: 1.05em; }

.markdown-body ul, .markdown-body ol { padding-left: 22px; margin: 6px 0 12px; }
.markdown-body li { margin: 5px 0; line-height: 1.65; }
.markdown-body strong { font-weight: 700; color: #111827; }
.markdown-body em { font-style: italic; }

.markdown-body a {
  color: #6366f1; text-decoration: underline;
  text-decoration-color: #c7d2fe; text-underline-offset: 2px;
}
.markdown-body a:hover { text-decoration-color: #6366f1; }

/* 行内代码 */
.markdown-body code {
  background: #eef2ff; color: #4f46e5;
  padding: 2px 7px; border-radius: 6px;
  font-family: 'Fira Code', 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: 13px; font-weight: 500;
  border: 1px solid #e0e7ff;
}

/* ── 代码块 ── */
.markdown-body .code-block {
  margin: 14px 0;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid #d0d7de;
  background: #f6f8fa;
  box-shadow: 0 1px 4px rgba(0,0,0,0.06);
}
.markdown-body .code-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 7px 14px;
  background: #ebedf0;
  border-bottom: 1px solid #d0d7de;
  user-select: none;
}
.markdown-body .code-lang-badge {
  font-size: 11.5px; font-weight: 600; color: #57606a;
  font-family: 'Fira Code', Consolas, monospace;
  text-transform: lowercase; letter-spacing: 0.3px;
}
.markdown-body .code-action-row { display: flex; align-items: center; gap: 4px; }
.markdown-body .cb-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 3px 9px; border-radius: 6px;
  border: 1px solid #d0d7de; background: transparent;
  color: #57606a; font-size: 11.5px; font-family: inherit;
  cursor: pointer; transition: all 0.15s; line-height: 1.4;
}
.markdown-body .cb-btn:hover { border-color: #8b949e; color: #24292f; background: #fff; }
.markdown-body .cb-btn.cb-done { border-color: #2da44e !important; color: #1a7f37 !important; background: #dafbe1 !important; }
.markdown-body .cb-preview:hover { border-color: #6366f1; color: #4f46e5; background: #eef2ff; }
.markdown-body .code-pre {
  margin: 0; padding: 14px 18px; overflow-x: auto;
  background: #f6f8fa; font-size: 13px; line-height: 1.65;
}
.markdown-body .code-pre code.hljs {
  background: transparent !important; padding: 0;
  font-family: 'Fira Code', 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: inherit; line-height: inherit; font-weight: 400; border: none;
}

.markdown-body blockquote {
  border-left: 3px solid #a5b4fc; padding: 8px 16px; color: #6b7280;
  margin: 12px 0; background: #f5f3ff; border-radius: 0 8px 8px 0; font-style: italic;
}
.markdown-body hr { border: none; border-top: 1px solid #e4e6ef; margin: 18px 0; }

.markdown-body .table-wrapper {
  width: 100%; overflow-x: auto; margin: 14px 0;
  border-radius: 8px; border: 1px solid #e4e6ef;
}
.markdown-body .table-wrapper table {
  border-collapse: collapse; width: 100%; min-width: 400px;
  font-size: 13.5px; margin: 0; border: none;
}
.markdown-body table {
  border-collapse: collapse; width: 100%; margin: 14px 0; font-size: 13.5px;
  border-radius: 8px; overflow: hidden; border: 1px solid #e4e6ef;
}
.markdown-body th, .markdown-body td {
  border: 1px solid #e4e6ef; padding: 8px 14px; text-align: left; white-space: nowrap;
}
.markdown-body th { background: #f3f4f8; font-weight: 600; color: #374151; font-size: 13px; }
.markdown-body tr:nth-child(even) td { background: #f9fafb; }
.markdown-body tr:hover td { background: #eef2ff; }

/* ── 推理过程块 ── */
.think-block {
  border: 1px solid #ede9fe;
  border-radius: 12px;
  overflow: hidden;
  background: #fff;
  margin-bottom: 8px;
  box-shadow: 0 1px 6px rgba(124,58,237,0.06);
}
.think-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  width: 100%;
  padding: 8px 13px;
  background: none;
  border: none;
  cursor: pointer;
  font-size: 12.5px;
  font-family: inherit;
  color: #7c3aed;
  text-align: left;
  user-select: none;
  transition: background 0.15s;
}
.think-toggle:hover { background: rgba(139,92,246,0.04); }
.think-hd-left {
  display: flex;
  align-items: center;
  gap: 6px;
}
.think-hd-right {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #a78bfa;
}
.think-title { font-weight: 600; letter-spacing: 0.1px; }
.think-len { font-size: 10.5px; color: #c4b5fd; font-weight: 400; }
.think-spin-icon { animation: think-rotate 1.8s linear infinite; transform-origin: center; flex-shrink: 0; }
@keyframes think-rotate { to { transform: rotate(360deg); } }
.think-chevron {
  transition: transform 0.22s cubic-bezier(0.4,0,0.2,1);
  transform: rotate(0deg);
  flex-shrink: 0;
}
.think-chevron.expanded { transform: rotate(180deg); }
.think-body {
  padding: 10px 14px 12px;
  font-size: 12px;
  color: #6b7280;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  border-top: 1px solid #f3e8ff;
  max-height: 320px;
  overflow-y: auto;
  background: #faf7ff;
  font-family: 'Fira Code', 'Cascadia Code', Consolas, monospace;
}
.think-slide-enter-active,
.think-slide-leave-active { transition: all 0.22s cubic-bezier(0.4,0,0.2,1); overflow: hidden; }
.think-slide-enter-from,
.think-slide-leave-to { max-height: 0 !important; opacity: 0; }
.think-slide-enter-to,
.think-slide-leave-from { max-height: 320px; opacity: 1; }
</style>
