<script setup lang="ts">
import { computed, ref, watch, onMounted, onUnmounted, nextTick, type Directive } from 'vue'
import katex from 'katex'
import 'katex/dist/katex.min.css'
import { Marked } from 'marked'
import hljs from 'highlight.js/lib/common'
import type { Message, ToolCallRecord, StepRecord, AgentStatus, CognitiveState } from '../types'
import { makeEmptyCognitiveState } from '../types'
import { CopyDocument, Check, Search, Clock, Cpu, Document, ArrowDown, Loading, Close } from '@element-plus/icons-vue'
import CodePreview from './CodePreview.vue'
import AgentStatusBubble from './AgentStatusBubble.vue'
import FileArtifactCard from './FileArtifactCard.vue'
import type { FileArtifact } from '../types'
import { detectLanguage } from '../types'

const PREVIEWABLE = new Set(['html','svg','css','javascript','js','typescript','ts','vue','jsx','tsx','react'])

// ─── 工具元信息 ───
const TOOL_META: Record<string, { label: string; icon: any; color: string }> = {
  web_search:        { label: '搜索了网络',  icon: Search,   color: '#00AEEC' },
  fetch_webpage:     { label: '阅读了网页',  icon: Document, color: '#0ea5e9' },
  get_current_time:  { label: '获取了时间',  icon: Clock,    color: '#0ea5e9' },
  calculator:        { label: '执行了计算',  icon: Cpu,      color: '#10b981' },
  execute_code:      { label: '执行了代码',  icon: Cpu,      color: '#10b981' },
  run_shell:         { label: '执行了命令',  icon: Cpu,      color: '#10b981' },
  sandbox_write:     { label: '写入了文件',  icon: Document, color: '#f59e0b' },
  sandbox_read:      { label: '读取了文件',  icon: Document, color: '#0ea5e9' },
}
function toolMeta(name: string) {
  return TOOL_META[name] ?? { label: `调用了 ${name}`, icon: Cpu, color: '#6b7280' }
}
function faviconUrl(url: string) {
  try { return `https://www.google.com/s2/favicons?domain=${new URL(url).hostname}&sz=16` }
  catch { return '' }
}

// ─── Marked + highlight.js 实例 ───

// 流式渲染时跳过 hljs（大内容会阻塞主线程），通过 renderContent(raw, streaming=true) 激活
let _skipHljs = false

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
    // 流式大内容：跳过 hljs（避免每 token 触发 O(n²) 语法高亮阻塞主线程）
    if (_skipHljs) {
      highlighted = text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    } else if (hljs.getLanguage(language)) {
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

    // 图片：支持 data URI（模型可能输出 inline SVG 预览图）和普通 URL
    image(token: any): string {
      const href: string = token.href ?? ''
      const alt: string = token.text ?? token.title ?? ''
      // data URI 图片：直接渲染（marked 默认可能无法正确处理超长 data URI）
      if (href.startsWith('data:')) {
        return `<img src="${href}" alt="${alt}" style="max-width:100%;border-radius:8px;margin:8px 0;" />`
      }
      // 普通 URL 图片
      return `<img src="${href}" alt="${alt}" style="max-width:100%;border-radius:8px;margin:8px 0;" loading="lazy" />`
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

// LaTeX 公式支持：$...$ 行内、$$...$$ 块级
import markedKatex from 'marked-katex-extension'
markedInstance.use(markedKatex({ throwOnError: false }))

// ─── Props ───
const props = defineProps<{
  message: Message
  isLastLoading?: boolean
  agentStatus?: AgentStatus
  cognitive?: CognitiveState
  messageIndex?: number
}>()

const emit = defineEmits<{
  regenerate: []
  editMessage: [payload: { index: number; content: string }]
  selectFile: [file: FileArtifact]
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
    const result = props.message.steps.map(step => ({
      step,
      toolCalls: step.toolCalls,
      thinking: step.thinking,
      content: step.content,
    }))
    // DB 恢复兜底：如果所有 step 的 toolCalls 都空但 message 级别有（旧数据无 step_index），
    // 将 message 级别工具分配到最后一个 section，保证工具卡片可见
    const totalStepTools = result.reduce((n, s) => n + s.toolCalls.length, 0)
    if (totalStepTools === 0 && props.message.toolCalls?.length) {
      result[result.length - 1].toolCalls = props.message.toolCalls
    }
    return result
  }
  return [{
    step: null,
    toolCalls: props.message.toolCalls ?? [],
    thinking: props.message.thinking ?? '',
    content: props.message.content ?? '',
  }]
})

// ─── Markdown 内容渲染（去除 think 块残留） ─────────────────────────────────
// streaming=true：流式模式，大内容（>3000字）跳过 hljs，避免主线程阻塞
const _SKIP_HLJS_THRESHOLD = 3000

function renderContent(raw: string, streaming = false): string {
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

  // data URI 图片预处理：marked 无法正确解析含 http:// 和单引号的 data URI，
  // 先提取为占位符，渲染后还原为 <img> 标签
  const dataUriImages: { placeholder: string; alt: string; uri: string }[] = []
  trimmed = trimmed.replace(
    /!\[([^\]]*)\]\((data:image\/[^)]+)\)/g,
    (_match, alt, uri) => {
      const id = `__DATA_IMG_${dataUriImages.length}__`
      dataUriImages.push({ placeholder: id, alt, uri })
      return id
    }
  )

  // 流式大内容：激活 _skipHljs 标志，对本次 buildCodeHtml 调用生效
  _skipHljs = streaming && trimmed.length > _SKIP_HLJS_THRESHOLD
  try {
    // 模型直接输出裸 HTML 页面时（没有 markdown 代码块包裹），自动包裹为 html 代码块渲染
    if (/^<!doctype\s+html/i.test(trimmed) || /^<html[\s>]/i.test(trimmed)) {
      return buildCodeHtml({ text: trimmed, lang: 'html' })
    }
    let result = markedInstance.parse(trimmed) as string
    // 还原 data URI 图片占位符为 <img> 标签
    for (const img of dataUriImages) {
      result = result.replace(
        img.placeholder,
        `<img src="${img.uri}" alt="${img.alt}" style="max-width:100%;border-radius:8px;margin:8px 0;" />`
      )
    }
    return result
  } finally {
    _skipHljs = false
  }
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

// ─── 节流渲染（防止大代码块 O(n²) 渲染冻结浏览器） ────────────────────────────
// 每次 token 到来都重新 renderContent + hljs.highlight 全量内容代价很高（尤其是大 HTML 页面）。
// 超过 _RENDER_LARGE_CHARS 时：
//   - 节流至每 THROTTLE_MS 更新一次
//   - 流式期间（isLastLoading）跳过 hljs，用纯转义占位，保证每帧都能渲染
//   - 流式结束后（isLastLoading→false）触发最终全量 hljs 高亮
const _RENDER_THROTTLE_MS  = 100  // 超过阈值时节流间隔（ms）
const _RENDER_LARGE_CHARS  = 3000 // 内容字节数超过此值时启用节流（同 _SKIP_HLJS_THRESHOLD）

const renderedHtml = ref<string[]>([])
let _renderTimer: ReturnType<typeof setTimeout> | null = null

function _doRender(streaming = false) {
  _renderTimer = null
  renderedHtml.value = sections.value.map(s => renderContent(s.content, streaming))
}

watch(
  sections,
  (secs) => {
    const streaming = props.isLastLoading ?? false
    const maxLen = Math.max(0, ...secs.map(s => s.content.length))
    if (maxLen < _RENDER_LARGE_CHARS) {
      // 小内容：立即渲染，不节流（无需跳过 hljs）
      if (_renderTimer !== null) { clearTimeout(_renderTimer); _renderTimer = null }
      renderedHtml.value = secs.map(s => renderContent(s.content, false))
    } else if (_renderTimer === null) {
      // 大内容：节流 + 流式期间跳过 hljs，避免每 token 全量高亮阻塞主线程
      _renderTimer = setTimeout(() => _doRender(streaming), _RENDER_THROTTLE_MS)
    }
    // else: 已有待执行的定时器，等它触发即可
  },
  { deep: true, immediate: true },
)

// 流式结束后触发最终全量 hljs 高亮（之前跳过 hljs 的占位内容替换为正式高亮版本）
watch(
  () => props.isLastLoading,
  (loading) => {
    if (!loading) {
      if (_renderTimer !== null) { clearTimeout(_renderTimer); _renderTimer = null }
      _doRender(false)
    }
  },
)

onUnmounted(() => {
  if (_renderTimer !== null) { clearTimeout(_renderTimer); _renderTimer = null }
})

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
// 用户未手动操作过时，正在思考（无 content）自动展开，有 content 后自动折叠
const thinkExpanded = ref<Record<number, boolean>>({})
const thinkManual   = ref<Record<number, boolean>>({})  // 用户是否手动操作过
function toggleThink(si: number) {
  thinkManual.value[si] = true
  thinkExpanded.value[si] = !thinkExpanded.value[si]
}
function isThinkExpanded(si: number) {
  // 用户手动操作过 → 尊重用户选择
  if (thinkManual.value[si]) return thinkExpanded.value[si] ?? false
  // 未手动操作：还在思考（无 content）时自动展开，有 content 后自动折叠
  const sec = sections.value[si]
  return sec ? !sec.content : false
}

// ─── 工具折叠（key = "sectionIndex-toolIndex"） ─────────────────────────────
const collapsed = ref<Record<string, boolean>>({})
function toggle(si: number, ti: number) {
  const k = `${si}-${ti}`
  collapsed.value[k] = !collapsed.value[k]
}
function isCollapsed(si: number, ti: number) { return collapsed.value[`${si}-${ti}`] ?? false }

// 工具执行完成后不自动折叠（保持展开，方便用户查看结果）

// ─── 沙箱工具分组：连续沙箱操作合并为一个终端块 ───────────────────────────────
// 按 displayMode 协议判断是否为终端工具（非 default 的都进终端渲染）
const TERMINAL_MODES = new Set(['terminal', 'file_write', 'file_read'])

// ─── tool_call_args 流式解码：原始 JSON 转可读代码 ──────────────────────────
// tool_call_args 流的是原始 JSON 参数片段（如 {"path":"a.html","content":"<!DOCTYPE html>\\n<html>..."}）
// 其中 \n \t \" 是字面转义字符，需要解码为真正的换行/缩进才能在终端可读
function decodeStreamOutput(raw: string): string {
  // 尝试提取 "content": "..." 或 "code": "..." 的值部分
  const match = raw.match(/"(?:content|code)"\s*:\s*"/)
  if (match) {
    const start = raw.indexOf(match[0]) + match[0].length
    raw = raw.slice(start)
    // 去掉尾部可能的不完整 JSON（如 "}）
    if (raw.endsWith('"}')) raw = raw.slice(0, -2)
    else if (raw.endsWith('"')) raw = raw.slice(0, -1)
  }
  return raw
    .replace(/\\n/g, '\n')
    .replace(/\\t/g, '\t')
    .replace(/\\"/g, '"')
    .replace(/\\\\/g, '\\')
}

// ─── 文件内容折叠（cat 写入的大文件） ──────────────────────────────────────
const fileExpanded = ref<Record<string, boolean>>({})
function isFileExpanded(si: number, ii: number) { return fileExpanded.value[`${si}-${ii}`] ?? false }
function toggleFileExpand(si: number, ii: number) { fileExpanded.value[`${si}-${ii}`] = !fileExpanded.value[`${si}-${ii}`] }
const FILE_COLLAPSE_LINES = 15  // 超过此行数折叠

interface SandboxGroupItem { tc: ToolCallRecord; ti: number }
type ToolGroup =
  | { type: 'single'; tc: ToolCallRecord; ti: number }
  | { type: 'sandbox'; tools: SandboxGroupItem[]; firstTi: number }

function getToolGroups(toolCalls: ToolCallRecord[]): ToolGroup[] {
  const groups: ToolGroup[] = []
  let buf: SandboxGroupItem[] = []
  const flush = () => {
    if (buf.length) { groups.push({ type: 'sandbox', tools: [...buf], firstTi: buf[0].ti }); buf = [] }
  }
  toolCalls.forEach((tc, ti) => {
    if (TERMINAL_MODES.has(tc.displayMode || 'default')) { buf.push({ tc, ti }) }
    else { flush(); groups.push({ type: 'single', tc, ti }) }
  })
  flush()
  return groups
}

function toggleSandbox(si: number) { const k = `${si}-sbx`; collapsed.value[k] = !collapsed.value[k] }
function isSandboxCollapsed(si: number) { return collapsed.value[`${si}-sbx`] ?? false }
function isSandboxRunning(tools: SandboxGroupItem[]) { return tools.some(t => !t.tc.done) }

// ─── 终端自动滚动指令：内容更新时跟随到底部，用户上滚则暂停 ─────────────────
const vAutoScroll: Directive<HTMLElement> = {
  mounted(el) {
    let userScrolledUp = false
    el.addEventListener('scroll', () => {
      // 距底部 < 30px 视为"在底部"，恢复自动滚动
      const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 30
      userScrolledUp = !atBottom
    }, { passive: true })
    const observer = new MutationObserver(() => {
      if (!userScrolledUp) {
        el.scrollTop = el.scrollHeight
      }
    })
    observer.observe(el, { childList: true, subtree: true, characterData: true })
    ;(el as any).__autoScrollObs = observer
  },
  unmounted(el) {
    (el as any).__autoScrollObs?.disconnect()
  },
}

// ─── 文件产物：优先 message.artifacts（DB 外键），其次 cognitive.artifacts（SSE 实时） ──
const fileArtifacts = computed<FileArtifact[]>(() => {
  if (props.message.role !== 'assistant') return []

  // 正在生成中（loading）→ 从 cognitive.artifacts 中过滤出当前消息关联的产物
  // 不能直接返回全部 cognitive.artifacts，否则第一轮产物会跑到第二轮消息下面
  if (props.isLastLoading && props.cognitive?.artifacts?.length) {
    // 收集当前消息中所有工具写入的文件名
    const myFiles = new Set<string>()
    const collectNames = (toolCalls: ToolCallRecord[]) => {
      for (const tc of toolCalls) {
        // file_write 模式：从 input.path 提取文件名
        if (tc.displayMode === 'file_write' && tc.done) {
          const path = String((tc.input as any).path || '')
          if (path) myFiles.add(path.split('/').pop() || path)
        }
        // terminal 模式的 output 中可能包含产物文件名
        if (tc.displayMode === 'terminal' && tc.done && tc.output) {
          const match = tc.output.match(/文件名:\s*(\S+)/i) || tc.output.match(/已生成:\s*(\S+)/i)
          if (match) myFiles.add(match[1].trim())
        }
        // downloadable 产物通过标记直接放行，不在此处匹配
      }
    }
    if (props.message.steps?.length) {
      for (const step of props.message.steps) collectNames(step.toolCalls)
    }
    if (props.message.toolCalls?.length) collectNames(props.message.toolCalls)

    // downloadable 产物（sandbox_download 产出的打包文件）不需要文件名匹配，直接通过
    const downloadable = props.cognitive.artifacts.filter(a => (a as any).downloadable)

    if (myFiles.size > 0) {
      const matched = props.cognitive.artifacts.filter(a => myFiles.has(a.name))
      return [...matched, ...downloadable.filter(a => !matched.includes(a))]
    }
    if (downloadable.length > 0) return downloadable
    return []
  }

  // ── 已完成：从 message.artifacts 恢复（DB message_id 外键，不依赖正则） ──
  if (props.message.artifacts?.length) {
    return props.message.artifacts.map(meta => {
      // 优先从 cognitive.artifacts 找完整版（含 content/slides_html）
      const full = props.cognitive?.artifacts?.find(a => a.name === meta.name && a.content)
      return full || meta
    })
  }

  // ── 兜底：从 cognitive.artifacts 按工具调用匹配 ──
  const myFiles = new Set<string>()
  const extractNames = (toolCalls: ToolCallRecord[]) => {
    for (const tc of toolCalls) {
      if (tc.displayMode === 'file_write' && tc.done) {
        const path = String((tc.input as any).path || '')
        if (path) myFiles.add(path.split('/').pop() || path)
      }
      if (tc.displayMode === 'terminal' && tc.done && tc.output) {
        const match = tc.output.match(/文件名:\s*(\S+)/i)
          || tc.output.match(/已生成:\s*(\S+)/i)
          || tc.output.match(/文件已准备好下载:\s*(\S+)/)
        if (match) myFiles.add(match[1].trim())
      }
    }
  }
  if (props.message.steps?.length) {
    for (const step of props.message.steps) extractNames(step.toolCalls)
  }
  if (props.message.toolCalls?.length) extractNames(props.message.toolCalls)
  if (myFiles.size === 0) return []

  if (props.cognitive?.artifacts?.length) {
    const matched = props.cognitive.artifacts.filter(a => myFiles.has(a.name))
    if (matched.length > 0) return matched
  }
  return []
})

// ─── 整条消息是否有可复制内容 ───────────────────────────────────────────────
const hasContent = computed(() => {
  if (props.message.steps?.length) return props.message.steps.some(s => s.content)
  return !!props.message.content
})

// ─── 消息编辑 ───
const isEditing = ref(false)
const editContent = ref('')
function startEdit() {
  editContent.value = props.message.content
  isEditing.value = true
}
function submitEdit() {
  if (editContent.value.trim() && props.messageIndex !== undefined) {
    emit('editMessage', { index: props.messageIndex, content: editContent.value.trim() })
  }
  isEditing.value = false
}
function cancelEdit() { isEditing.value = false }
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

        <!-- Editing mode -->
        <div v-else-if="isEditing" class="edit-wrap">
          <textarea v-model="editContent" class="edit-textarea" rows="3" @keydown.ctrl.enter="submitEdit"></textarea>
          <div class="edit-actions">
            <button class="edit-btn edit-cancel" @click="cancelEdit">取消</button>
            <button class="edit-btn edit-submit" @click="submitEdit">发送</button>
          </div>
        </div>
        <!-- Plain text bubble -->
        <div v-else-if="message.content" class="user-bubble" @dblclick="startEdit">{{ message.content }}</div>
        <!-- User message hover actions -->
        <div v-if="message.content && !isEditing" class="user-actions">
          <button class="action-btn-sm" @click="startEdit" title="编辑消息">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
          </button>
        </div>
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
        <!-- Bilibili 风格星星图标 — 蓝粉双色 -->
        <svg width="17" height="17" viewBox="0 0 32 32" fill="none">
          <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#00AEEC"/>
          <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#FB7299" opacity="0.6"/>
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

          <!-- 工具调用块（沙箱操作合并为统一终端） -->
          <div v-if="sec.toolCalls.length" class="tool-calls">
            <template v-for="(group, gi) in getToolGroups(sec.toolCalls)" :key="gi">

              <!-- ═══ 非沙箱工具：保持原有样式 ═══ -->
              <template v-if="group.type === 'single'">
                <div :class="['tool-block', (group.tc.name === 'web_search' || group.tc.name === 'fetch_webpage') ? 'tool-block-sources' : '']">

                  <!-- web_search -->
                  <template v-if="group.tc.name === 'web_search'">
                    <div class="tool-header tool-header-flat">
                      <span class="tool-status-icon">
                        <svg v-if="group.tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                          <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                          <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                          <circle cx="8" cy="8" r="6" stroke="#66D3F5" stroke-width="1.5" stroke-dasharray="20 18"/>
                        </svg>
                      </span>
                      <el-icon style="font-size:13px; color:#00AEEC"><Search /></el-icon>
                      <span class="tool-label">搜索了网络</span>
                      <span class="tool-query">「{{ (group.tc.input as any).query }}」</span>
                      <span v-if="!group.tc.done" class="tool-pending">搜索中...</span>
                    </div>
                    <div v-if="group.tc.searchItems?.length" class="search-url-list">
                      <a v-for="(item, ii) in group.tc.searchItems" :key="ii"
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
                            <circle cx="8" cy="8" r="6" stroke="#66D3F5" stroke-width="1.5" stroke-dasharray="20 18"/>
                          </svg>
                        </span>
                        <img :src="faviconUrl(item.url)" class="url-favicon"
                             @error="($event.target as HTMLImageElement).style.display='none'" />
                        <span class="url-text">{{ item.url }}</span>
                      </a>
                    </div>
                  </template>

                  <!-- fetch_webpage -->
                  <template v-else-if="group.tc.name === 'fetch_webpage'">
                    <div class="tool-header tool-header-flat">
                      <span class="tool-status-icon">
                        <svg v-if="group.tc.done && group.tc.fetchStatus === 'fail'" width="14" height="14" viewBox="0 0 16 16" fill="none">
                          <circle cx="8" cy="8" r="6.5" stroke="#ef4444" stroke-width="1.5"/>
                          <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#ef4444" stroke-width="1.4" stroke-linecap="round"/>
                        </svg>
                        <svg v-else-if="group.tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                          <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                          <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                          <circle cx="8" cy="8" r="6" stroke="#66D3F5" stroke-width="1.5" stroke-dasharray="20 18"/>
                        </svg>
                      </span>
                      <el-icon style="font-size:13px; color:#0ea5e9"><Document /></el-icon>
                      <span class="tool-label">{{ group.tc.done && group.tc.fetchStatus === 'fail' ? '读取失败' : (group.tc.done ? '读取了网页' : '正在阅读') }}</span>
                      <span v-if="(group.tc.input as any).url" class="tool-query">
                        <a :href="(group.tc.input as any).url" target="_blank" class="fetch-url-link" @click.stop>
                          <img :src="faviconUrl((group.tc.input as any).url)" class="url-favicon" style="margin-right:3px"
                               @error="($event.target as HTMLImageElement).style.display='none'" />
                          {{ (group.tc.input as any).url }}
                        </a>
                      </span>
                      <span v-if="!group.tc.done" class="tool-pending">读取中...</span>
                    </div>
                  </template>

                  <!-- 其他工具 -->
                  <template v-else>
                    <div class="tool-header" @click="toggle(si, group.ti)">
                      <span class="tool-status-icon">
                        <svg v-if="group.tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                          <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                          <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                        </svg>
                        <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                          <circle cx="8" cy="8" r="6" stroke="#66D3F5" stroke-width="1.5" stroke-dasharray="20 18"/>
                        </svg>
                      </span>
                      <el-icon :style="{ color: toolMeta(group.tc.name).color }" style="font-size:13px">
                        <component :is="toolMeta(group.tc.name).icon" />
                      </el-icon>
                      <span class="tool-label">{{ toolMeta(group.tc.name).label }}</span>
                      <span v-if="!group.tc.done" class="tool-pending">执行中...</span>
                      <span class="tool-chevron" :class="{ open: !isCollapsed(si, group.ti) }">›</span>
                    </div>
                    <Transition name="slide">
                      <div v-show="!isCollapsed(si, group.ti)" class="tool-body">
                        <div class="tool-output-plain">
                          <span class="tool-tag">结果</span>
                          <span>{{ group.tc.output }}</span>
                        </div>
                      </div>
                    </Transition>
                  </template>

                </div>
              </template>

              <!-- ═══ 沙箱工具组：统一终端块（ChatFlow Sandbox） ═══ -->
              <template v-else>
                <div class="sandbox-block">
                  <!-- 终端标题栏 -->
                  <div class="term-titlebar" @click="toggleSandbox(si)">
                    <!-- macOS 风格三色圆点 -->
                    <div class="term-dots">
                      <span class="term-dot term-dot--red"></span>
                      <span class="term-dot term-dot--yellow"></span>
                      <span class="term-dot term-dot--green"></span>
                    </div>
                    <!-- 居中标题 -->
                    <span class="term-title">🖥 ChatFlow 的电脑</span>
                    <span class="term-ops-count" v-if="(group as any).tools.length > 1">{{ (group as any).tools.length }} ops</span>
                    <span class="tool-chevron" :class="{ open: !isSandboxCollapsed(si) }">›</span>
                  </div>

                  <!-- 终端内容：所有沙箱操作顺序展示 -->
                  <Transition name="slide">
                    <div v-show="!isSandboxCollapsed(si)" v-auto-scroll class="term-body">
                      <template v-for="(item, ii) in (group as any).tools" :key="ii">
                        <!-- 操作间分隔线 -->
                        <div v-if="ii > 0" class="term-divider"></div>

                        <!-- ═══ _generating 阶段：流式参数 + thinking 预览 ═══ -->
                        <template v-if="(item.tc.input as any)?._generating">
                          <div class="term-line term-line--generating">
                            <span class="term-prompt-sign">$</span>
                            <span class="term-generating-text">{{ item.tc.name }}</span>
                            <span class="term-generating-dots">...</span>
                          </div>
                          <!-- tool_call_args 已到达：解码 JSON 转义后显示可读代码 -->
                          <pre v-if="item.tc.output" class="term-stream-output" v-auto-scroll>{{ decodeStreamOutput(item.tc.output) }}</pre>
                          <!-- tool_call_args 未开始，模型在 thinking：显示 thinking 摘要 -->
                          <div v-else-if="sec.thinking" class="term-thinking-preview">
                            <div class="term-thinking-hint">💭 模型正在构思中...</div>
                            <pre class="term-thinking-text">{{ sec.thinking.slice(-500) }}</pre>
                          </div>
                        </template>

                        <!-- ═══ 完成后渲染：按 displayMode 协议决定模板 ═══ -->

                        <!-- file_write 模式（可折叠） -->
                        <template v-else-if="item.tc.displayMode === 'file_write'">
                          <div class="term-line term-line--dimmed term-file-header" @click="toggleFileExpand(si, ii)">
                            <span class="term-prompt-sign">$</span>
                            <span>cat &gt; {{ (item.tc.input as any).path || 'file' }} &lt;&lt; 'EOF'</span>
                            <span v-if="(item.tc.input as any).content && ((item.tc.input as any).content as string).split('\n').length > FILE_COLLAPSE_LINES"
                              class="term-fold-badge">
                              {{ isFileExpanded(si, ii) ? '收起' : `${((item.tc.input as any).content as string).split('\n').length} 行 · 展开` }}
                            </span>
                          </div>
                          <div v-if="(item.tc.input as any).content" class="term-file-body" :class="{ collapsed: !isFileExpanded(si, ii) && ((item.tc.input as any).content as string).split('\n').length > FILE_COLLAPSE_LINES }">
                            <pre class="term-code-inline">{{ (item.tc.input as any).content }}</pre>
                            <div v-if="!isFileExpanded(si, ii) && ((item.tc.input as any).content as string).split('\n').length > FILE_COLLAPSE_LINES" class="term-file-fade" @click="toggleFileExpand(si, ii)"></div>
                          </div>
                          <div v-if="(item.tc.input as any).content" class="term-line term-line--dimmed"><span>EOF</span></div>
                          <div v-if="item.tc.done && item.tc.output" class="term-line term-line--ok">{{ item.tc.output }}</div>
                        </template>

                        <!-- file_read 模式 -->
                        <template v-else-if="item.tc.displayMode === 'file_read'">
                          <div class="term-line">
                            <span class="term-prompt-sign">$</span>
                            <span class="term-cmd-text">cat {{ (item.tc.input as any).path || 'file' }}</span>
                          </div>
                          <pre v-if="item.tc.done && item.tc.output" class="term-code-inline">{{ item.tc.output }}</pre>
                        </template>

                        <!-- terminal 模式（execute_code / run_shell / create_ppt 等，含默认兜底） -->
                        <template v-else>
                          <!-- 代码写入区（execute_code 有 code 参数时） -->
                          <template v-if="(item.tc.input as any).code">
                            <div class="term-line term-line--dimmed">
                              <span class="term-prompt-sign">$</span>
                              <span>cat &gt; {{ ({python:'main.py',javascript:'main.js',java:'Main.java',shell:'run.sh'} as any)[(item.tc.input as any).language] || 'code' }} &lt;&lt; 'EOF'</span>
                            </div>
                            <pre class="term-code-inline">{{ (item.tc.input as any).code }}</pre>
                            <div class="term-line term-line--dimmed"><span>EOF</span></div>
                          </template>
                          <!-- PPT JSON 写入区（create_ppt 有 ppt_json 参数时） -->
                          <template v-else-if="(item.tc.input as any).ppt_json">
                            <div class="term-line term-line--dimmed">
                              <span class="term-prompt-sign">$</span>
                              <span>cat &gt; ppt_data.json &lt;&lt; 'EOF'</span>
                            </div>
                            <pre class="term-code-inline">{{ (item.tc.input as any).ppt_json }}</pre>
                            <div class="term-line term-line--dimmed"><span>EOF</span></div>
                          </template>
                          <!-- 命令行 -->
                          <div class="term-line">
                            <span class="term-prompt-sign">$</span>
                            <span class="term-cmd-text">{{ (item.tc.input as any).command || item.tc.name }}</span>
                          </div>
                          <!-- 流式输出（未完成时） -->
                          <pre v-if="!item.tc.done && item.tc.output" class="term-stream-output">{{ item.tc.output }}</pre>
                          <!-- 完成后逐行渲染 -->
                          <template v-if="item.tc.done && item.tc.output">
                            <template v-for="(line, li) in item.tc.output.split('\n')" :key="`${ii}-${li}`">
                              <div v-if="line.startsWith('root@sandbox:')" class="term-line">
                                <span class="term-prompt-user">{{ line.split('$')[0] }}$</span>
                                <span class="term-cmd-text">{{ line.split('$ ').slice(1).join('$ ') }}</span>
                              </div>
                              <div v-else-if="line.startsWith('$ ')" class="term-line">
                                <span class="term-prompt-sign">$</span>
                                <span class="term-cmd-text">{{ line.slice(2) }}</span>
                              </div>
                              <div v-else-if="line.startsWith('[stderr]')" class="term-line term-line--err">{{ line.replace('[stderr] ', '') }}</div>
                              <div v-else-if="line.startsWith('[exit_code')" class="term-line term-line--err">{{ line }}</div>
                              <div v-else-if="line.startsWith('⏱')" class="term-line term-line--meta">{{ line }}</div>
                              <div v-else-if="line.trim()" class="term-line term-line--out">{{ line }}</div>
                            </template>
                          </template>
                        </template>
                      </template>

                      <!-- 运行中：底部闪烁光标 -->
                      <div v-if="isSandboxRunning((group as any).tools)" class="term-line term-cursor-line">
                        <span class="term-cursor">▋</span>
                      </div>
                    </div>
                  </Transition>
                </div>
              </template>

            </template>
          </div>

          <!-- 推理过程（可折叠白卡） -->
          <div v-if="sec.thinking" class="think-block">
            <button class="think-toggle" @click="toggleThink(si)">
              <span class="think-hd-left">
                <!-- 旋转星星图标（内容生成中）或静态图标（已完成） -->
                <svg v-if="!sec.content" class="think-spin-icon" width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="#00AEEC"/>
                  <path d="M19.5 4C19.5 4 20.1 6.6 22 7.5C20.1 8.4 19.5 11 19.5 11C19.5 11 18.9 8.4 17 7.5C18.9 6.6 19.5 4 19.5 4Z" fill="#00AEEC" opacity="0.4"/>
                </svg>
                <svg v-else width="13" height="13" viewBox="0 0 24 24" fill="none">
                  <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="#00AEEC" opacity="0.6"/>
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
              <div v-if="isThinkExpanded(si)" v-auto-scroll class="think-body">{{ sec.thinking }}</div>
            </Transition>
          </div>

          <!-- Markdown 内容（renderedHtml 节流缓存，防止大文档 O(n²) 渲染） -->
          <div v-if="sec.content" class="ai-content markdown-body" v-html="renderedHtml[si] ?? ''"></div>

        </div>
        <!-- /sections -->

        <!-- 文件产物卡片（sandbox_write 生成的文件） -->
        <div v-if="fileArtifacts.length > 0" class="file-artifacts">
          <FileArtifactCard
            v-for="(f, fi) in fileArtifacts"
            :key="fi"
            :file="f"
            @select="emit('selectFile', $event)"
          />
        </div>

        <!-- 操作行 -->
        <div v-if="hasContent || messageIndex !== undefined" class="ai-actions">
          <el-tooltip :content="copied ? '已复制！' : '复制内容'" placement="top" :show-after="300">
            <button class="action-btn" :class="{ copied }" @click="copy">
              <el-icon><component :is="copied ? Check : CopyDocument" /></el-icon>
              <span>{{ copied ? '已复制' : '复制' }}</span>
            </button>
          </el-tooltip>
          <el-tooltip content="重新生成" placement="top" :show-after="300">
            <button class="action-btn" @click="emit('regenerate')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M1 4v6h6"/><path d="M3.51 15a9 9 0 102.13-9.36L1 10"/></svg>
              <span>重新生成</span>
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
  content-visibility: auto;
  contain-intrinsic-size: auto 120px;
}

/* 用户 — Bilibili 风格 */
.msg.user { flex-direction: row-reverse; }
.user-avatar {
  width: 36px; height: 36px;
  border-radius: 50%;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border: 2px solid #D0EEF9;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
  box-shadow: 0 2px 8px rgba(0,174,236,0.1);
  transition: transform 0.2s;
}
.msg.user:hover .user-avatar { transform: scale(1.05); }
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
  background: #E3F6FD;
  color: #18191C;
  padding: 11px 18px;
  border-radius: 20px 20px 4px 20px;
  font-size: 14.5px;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  box-shadow: 0 1px 4px rgba(0,174,236,0.08);
  letter-spacing: -0.1px;
  border: 1px solid #D0EEF9;
}

/* ── Workflow plan card ── */
.wf-card {
  background: #fff;
  border: 1.5px solid #D0EEF9;
  border-radius: 16px;
  overflow: hidden;
  max-width: 320px;
  box-shadow: 0 2px 10px rgba(0,174,236,0.08);
}
.wf-card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 9px 12px 8px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border-bottom: 1px solid #D0EEF9;
}
.wf-card-badge {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 12px;
  font-weight: 700;
  color: #00AEEC;
}
.wf-card-count {
  font-size: 11px;
  font-weight: 600;
  color: #FB7299;
  background: rgba(251,114,153,0.08);
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
  background: #E3F6FD;
  border: 1px solid #B8E6F9;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 9.5px;
  font-weight: 700;
  color: #00AEEC;
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

/* AI — Bilibili 风格 */
.msg.assistant { flex-direction: row; }
.ai-avatar {
  width: 36px; height: 36px;
  border-radius: 12px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border: 2px solid #D0EEF9;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
  box-shadow: 0 2px 8px rgba(0,174,236,0.1);
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.msg.assistant:hover .ai-avatar {
  border-color: #00AEEC;
  box-shadow: 0 3px 12px rgba(0,174,236,0.15);
}
/* 呼吸动画 — 纯 opacity + border 变化，不改变尺寸避免遮挡 */
.ai-avatar--breathing {
  animation: ai-breathe 2s ease-in-out infinite !important;
  border-color: #00AEEC !important;
  background: linear-gradient(135deg, #E3F6FD 0%, #D0EEF9 100%) !important;
}
@keyframes ai-breathe {
  0%, 100% {
    border-color: #00AEEC;
    box-shadow: 0 0 12px rgba(0,174,236,0.3), 0 0 4px rgba(0,174,236,0.15);
  }
  50% {
    border-color: #FB7299;
    box-shadow: 0 0 12px rgba(251,114,153,0.3), 0 0 4px rgba(251,114,153,0.15);
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

/* ── 工具调用 — Bilibili 风格 ── */
.tool-calls { display: flex; flex-direction: column; gap: 6px; margin-bottom: 10px; }
.tool-block {
  border: 1px solid var(--cf-border);
  border-radius: var(--cf-radius-md, 14px);
  overflow: hidden;
  background: var(--cf-card);
  box-shadow: var(--cf-shadow-xs);
  transition: border-color 0.2s, box-shadow 0.2s;
}
.tool-block:hover {
  border-color: var(--cf-border-glow, rgba(0,174,236,0.2));
  box-shadow: var(--cf-shadow-sm), 0 0 12px rgba(0,174,236,0.04);
}
/* 胶囊悬浮提示风格（搜索/读取工具） */
.tool-block-sources {
  border: none;
  background: transparent;
  box-shadow: none;
}
.tool-block-sources .tool-header-flat {
  background: #fff;
  border: 1px solid #D0EEF9;
  border-left: 3px solid #00AEEC;
  border-radius: 12px;
  padding: 6px 12px 6px 10px;
  box-shadow: 0 1px 6px rgba(0,174,236,0.06);
  transition: box-shadow 0.2s, border-color 0.2s, transform 0.2s ease-out;
  gap: 7px;
}
.tool-block-sources .tool-header-flat:hover {
  border-color: #B8E6F9;
  border-left-color: #0095CC;
  box-shadow: 0 3px 12px rgba(0,174,236,0.12);
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
.search-url-row:hover { background: var(--cf-active); color: #0095CC; }
@keyframes fade-row { from { opacity: 0; transform: translateX(-4px); } to { opacity: 1; transform: translateX(0); } }
.url-status { display: flex; align-items: center; flex-shrink: 0; }
.url-favicon { width: 14px; height: 14px; border-radius: 3px; flex-shrink: 0; }
.url-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--cf-text-3); font-size: 11.5px; }
.fetch-url-link {
  display: inline-flex; align-items: center; gap: 4px;
  color: var(--cf-text-3); text-decoration: none; font-size: 12px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 420px;
}
.fetch-url-link:hover { color: #0095CC; }

/* 普通工具输出 */
.tool-output-plain { display: flex; align-items: flex-start; gap: 8px; font-size: 12.5px; padding: 4px 2px; color: var(--cf-text-3); }
.tool-tag {
  flex-shrink: 0; padding: 1px 7px; border-radius: 10px;
  font-size: 11px; font-weight: 600;
  background: #E3F6FD; color: #0095CC; border: 1px solid #D0EEF9;
}

/* 折叠动画 */
.slide-enter-active, .slide-leave-active { transition: max-height 0.3s ease, opacity 0.25s ease; overflow: hidden; }
.slide-enter-from, .slide-leave-to { max-height: 0; opacity: 0; }
.slide-enter-to, .slide-leave-from { max-height: 400px; opacity: 1; }

@keyframes spin { to { transform: rotate(360deg); } }
.spin { animation: spin 1s linear infinite; transform-origin: center; }

/* 文件产物 */
.file-artifacts {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin-top: 8px;
}

/* 操作行 — Bilibili 风格 */
.ai-actions { display: flex; gap: 4px; margin-top: 8px; opacity: 0; transition: opacity 0.2s; }
.msg.assistant:hover .ai-actions { opacity: 1; }
.action-btn {
  display: inline-flex; align-items: center; gap: 5px;
  padding: 4px 12px;
  background: var(--cf-card); border: 1.5px solid var(--cf-border);
  border-radius: 20px; color: var(--cf-text-4);
  font-size: 12px; font-weight: 500; font-family: inherit;
  cursor: pointer; transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.action-btn:hover { border-color: #00AEEC; color: #00AEEC; background: #E3F6FD; transform: scale(1.04); }
.action-btn.copied { border-color: #8AE0C0; color: #00B578; background: #D5F5E8; }

/* User message actions */
.user-actions {
  display: flex; gap: 4px; opacity: 0; transition: opacity 0.2s;
}
.msg.user:hover .user-actions { opacity: 1; }
.action-btn-sm {
  display: inline-flex; align-items: center; justify-content: center;
  width: 26px; height: 26px; border-radius: 8px;
  background: var(--cf-card); border: 1.5px solid var(--cf-border);
  color: var(--cf-text-4); cursor: pointer; transition: all 0.15s;
}
.action-btn-sm:hover { border-color: #00AEEC; color: #00AEEC; background: #E3F6FD; }

/* Edit mode */
.edit-wrap { width: 100%; max-width: 86%; min-width: 320px; }
.edit-textarea {
  width: 100%; padding: 10px 14px; border: 2px solid #00AEEC;
  border-radius: 14px; font-size: 14px; font-family: inherit;
  resize: vertical; min-height: 80px; background: var(--cf-card);
  color: var(--cf-text-1); outline: none; line-height: 1.6;
}
.edit-actions { display: flex; justify-content: flex-end; gap: 6px; margin-top: 6px; }
.edit-btn {
  padding: 5px 14px; border-radius: 20px; font-size: 12.5px;
  font-weight: 500; font-family: inherit; cursor: pointer; border: none;
  transition: all 0.15s;
}
.edit-cancel { background: var(--cf-hover); color: var(--cf-text-3); }
.edit-cancel:hover { background: var(--cf-border); }
.edit-submit { background: linear-gradient(135deg, #00AEEC, #23C1F0); color: #fff; box-shadow: 0 2px 8px rgba(0,174,236,0.25); }
.edit-submit:hover { box-shadow: 0 4px 14px rgba(0,174,236,0.35); transform: translateY(-1px); }

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
  color: #00AEEC; text-decoration: underline;
  text-decoration-color: #B8E6F9; text-underline-offset: 2px;
}
.markdown-body a:hover { text-decoration-color: #00AEEC; }

/* 行内代码 — Bilibili 蓝 */
.markdown-body code {
  background: #E3F6FD; color: #0095CC;
  padding: 2px 7px; border-radius: 6px;
  font-family: 'Fira Code', 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: 13px; font-weight: 500;
  border: 1px solid #D0EEF9;
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
.markdown-body .cb-btn.cb-done { border-color: #00B578 !important; color: #00875A !important; background: #D5F5E8 !important; }
.markdown-body .cb-preview:hover { border-color: #FB7299; color: #FB7299; background: #FDE8EF; }
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
  border-left: 3px solid #00AEEC; padding: 8px 16px; color: #61666D;
  margin: 12px 0; background: #E3F6FD; border-radius: 0 10px 10px 0; font-style: italic;
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

/* ── 推理过程块 — Bilibili 风格 ── */
.think-block {
  border: 1px solid var(--cf-border-glow, rgba(0,174,236,0.15));
  border-radius: var(--cf-radius-md, 14px);
  overflow: hidden;
  background: var(--cf-card, #fff);
  margin-bottom: 8px;
  box-shadow: 0 1px 6px rgba(0,174,236,0.06), inset 0 1px 0 rgba(255,255,255,0.8);
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
  color: #00AEEC;
  text-align: left;
  user-select: none;
  transition: background 0.15s;
}
.think-toggle:hover { background: rgba(0,174,236,0.04); }
.think-hd-left {
  display: flex;
  align-items: center;
  gap: 6px;
}
.think-hd-right {
  display: flex;
  align-items: center;
  gap: 5px;
  color: #66D3F5;
}
.think-title { font-weight: 600; letter-spacing: 0.1px; }
.think-len { font-size: 10.5px; color: #99E5F9; font-weight: 400; }
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
  color: #61666D;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
  border-top: 1px solid #D0EEF9;
  max-height: 320px;
  overflow-y: auto;
  background: #F0FAFD;
  font-family: 'Fira Code', 'Cascadia Code', Consolas, monospace;
}
.think-slide-enter-active,
.think-slide-leave-active { transition: all 0.22s cubic-bezier(0.4,0,0.2,1); overflow: hidden; }
.think-slide-enter-from,
.think-slide-leave-to { max-height: 0 !important; opacity: 0; }
.think-slide-enter-to,
.think-slide-leave-from { max-height: 320px; opacity: 1; }

/* ── 沙箱终端样式（Bilibili 浅色风格） ── */
.sandbox-block {
  border-radius: 12px;
  overflow: hidden;
  border: 1px solid #e3e5e7;
  background: #ffffff;
  box-shadow: 0 2px 12px rgba(0,0,0,0.06);
  margin-bottom: 6px;
  transition: box-shadow 0.2s, border-color 0.2s;
}
.sandbox-block:hover {
  border-color: #d0d3d6;
  box-shadow: 0 4px 18px rgba(0,0,0,0.09);
}

/* 标题栏 */
.term-titlebar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 14px;
  background: #f6f7f8;
  border-bottom: 1px solid #e3e5e7;
  cursor: pointer;
  user-select: none;
  transition: background 0.15s;
}
.term-titlebar:hover { background: #eef0f2; }

/* macOS 三色圆点 */
.term-dots {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
}
.term-dot {
  width: 10px;
  height: 10px;
  border-radius: 50%;
}
.term-dot--red    { background: #FF5F57; }
.term-dot--yellow { background: #FEBC2E; }
.term-dot--green  { background: #28C840; }

.term-title {
  flex: 1;
  font-size: 12px;
  font-weight: 600;
  color: #61666D;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  letter-spacing: 0.02em;
  text-align: center;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.term-ops-count {
  font-size: 10.5px;
  font-weight: 500;
  color: #9499a0;
  background: #f1f2f3;
  padding: 1px 7px;
  border-radius: 10px;
  flex-shrink: 0;
}

/* ChatFlow Logo 图标（右侧） */
.term-cf-logo {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  background: #f1f2f3;
  color: #9499a0;
  flex-shrink: 0;
  transition: all 0.3s ease;
}
.term-cf-logo.active {
  color: #00aeec;
  background: rgba(0, 174, 236, 0.08);
  animation: cf-logo-pulse 1.8s ease-in-out infinite;
}
@keyframes cf-logo-pulse {
  0%, 100% { opacity: 1; transform: scale(1); }
  50% { opacity: 0.55; transform: scale(0.9); }
}

/* 终端体 */
.term-body {
  padding: 12px 16px 14px;
  max-height: 520px;
  overflow-y: auto;
  font-family: 'Fira Code', 'Cascadia Code', 'JetBrains Mono', 'SF Mono', Consolas, monospace;
  font-size: 12.5px;
  line-height: 1.7;
  background: #fafbfc;
}

/* 操作间分隔线 */
.term-divider {
  height: 1px;
  background: #e3e5e7;
  margin: 8px 0;
}

/* 每一行 */
.term-line {
  white-space: pre-wrap;
  word-break: break-word;
  color: #18191c;
  min-height: 1.7em;
}
.term-line--dimmed { color: #9499a0; }
.term-line--out    { color: #18191c; }
.term-line--ok     { color: #00b578; font-size: 12px; }
.term-line--err    { color: #f25d59; }
.term-line--meta   { color: #9499a0; font-size: 11px; margin-top: 4px; }

/* Prompt */
.term-prompt-user {
  color: #00aeec;
  font-weight: 700;
  margin-right: 0;
}
.term-prompt-sign {
  color: #00aeec;
  font-weight: 700;
  margin-right: 6px;
}
.term-cmd-text { color: #18191c; font-weight: 500; }

/* 内联代码展示（不独立滚动，由 term-body 统一滚动） */
.term-code-inline {
  margin: 2px 0 2px 18px;
  padding: 6px 10px;
  white-space: pre-wrap;
  word-break: break-word;
  color: #61666d;
  font-size: 12px;
  line-height: 1.55;
  background: #f1f2f3;
  border-radius: 6px;
  border: 1px solid #e3e5e7;
}

/* 流式实时输出（不独立滚动，由 term-body 统一滚动 + 自动跟随） */
.term-stream-output {
  margin: 4px 0;
  padding: 6px 10px;
  white-space: pre-wrap;
  word-break: break-word;
  color: #18191c;
  font-size: 12.5px;
  line-height: 1.7;
  background: #f6f7f8;
  border-radius: 6px;
}

/* 工具参数生成中 loading — 纯 opacity 闪烁，不改变宽度避免抖动 */
.term-line--generating { color: #00AEEC; }
.term-generating-text {
  font-weight: 500;
  animation: generating-pulse 1.5s ease-in-out infinite;
}
.term-generating-dots {
  animation: generating-pulse 1.5s ease-in-out infinite;
}
@keyframes generating-pulse {
  0%, 100% { opacity: 1; }
  50% { opacity: 0.3; }
}

/* ─── 流式输出限高 + 自动滚动 ─── */
.term-stream-output {
  max-height: 300px;
  overflow-y: auto;
}

/* ─── 文件内容折叠 ─── */
.term-file-header {
  cursor: pointer;
  user-select: none;
  display: flex;
  align-items: center;
  gap: 6px;
}
.term-fold-badge {
  margin-left: auto;
  font-size: 10.5px;
  color: #00AEEC;
  background: rgba(0, 174, 236, 0.08);
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid rgba(0, 174, 236, 0.2);
  cursor: pointer;
  transition: all 0.2s;
  white-space: nowrap;
}
.term-fold-badge:hover {
  background: rgba(0, 174, 236, 0.15);
  border-color: rgba(0, 174, 236, 0.4);
}
.term-file-body {
  position: relative;
  transition: max-height 0.3s ease;
}
.term-file-body.collapsed {
  max-height: 240px;
  overflow: hidden;
}
.term-file-fade {
  position: absolute;
  bottom: 0;
  left: 0;
  right: 0;
  height: 60px;
  background: linear-gradient(transparent, var(--cf-card, #fff));
  cursor: pointer;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding-bottom: 6px;
  font-size: 11px;
  color: #00AEEC;
}
.term-file-fade::after { content: '点击展开完整内容'; }

/* thinking 预览（模型构思中，tool_call_args 还没到达时显示） */
.term-thinking-preview {
  margin: 6px 0;
  padding: 8px 10px;
  background: rgba(0, 174, 236, 0.06);
  border-left: 3px solid rgba(0, 174, 236, 0.3);
  border-radius: 0 6px 6px 0;
}
.term-thinking-hint {
  font-size: 11.5px;
  color: #00AEEC;
  font-weight: 500;
  margin-bottom: 4px;
  animation: generating-pulse 2s ease-in-out infinite;
}
.term-thinking-text {
  font-size: 11.5px;
  line-height: 1.6;
  color: var(--cf-text-4, #999);
  white-space: pre-wrap;
  word-break: break-word;
  max-height: 150px;
  overflow-y: auto;
  margin: 0;
  background: none;
}

/* 闪烁光标 */
.term-cursor-line { min-height: 1.7em; }
.term-cursor {
  color: #00aeec;
  animation: blink-cursor 1s step-end infinite;
  font-weight: 700;
}
@keyframes blink-cursor { 0%,100% { opacity: 1; } 50% { opacity: 0; } }

/* 折叠的 chevron */
.sandbox-block .tool-chevron {
  font-size: 16px; color: #9499a0;
  transform: rotate(90deg); transition: transform 0.25s;
  line-height: 1; flex-shrink: 0;
}
.sandbox-block .tool-chevron.open { transform: rotate(-90deg); }
</style>
