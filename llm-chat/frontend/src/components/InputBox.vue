<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import type { SendPayload, UploadedFile } from '../types'
import { Picture, Promotion, Loading } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { uploadFile as apiUploadFile } from '../api'
import UploadedFilePreview from './UploadedFilePreview.vue'

const props = defineProps<{
  loading: boolean
  centered?: boolean
  currentConvId?: string | null
}>()

const emit = defineEmits<{
  send: [payload: SendPayload]
  ensureConv: []   // 请求父组件先创建对话（上传需要 conv_id）
  'agent-change': [mode: boolean]   // Agent ⇄ Chat 切换时广播，供外层联动（比如空状态胶囊只在 Agent 显示）
}>()

const input = ref('')
const pendingImages = ref<string[]>([])
// 文件附件（非图片）—— 调用 /api/files/upload 后得到的元数据
// 状态：uploading → ready（上传完成，有 id）→ 发送时带 file_ids
interface PendingFile extends UploadedFile {
  uploading?: boolean
  error?: string
  _localId: string
}
const pendingFiles = ref<PendingFile[]>([])
const fileInputRef = ref<HTMLInputElement>()
const attachInputRef = ref<HTMLInputElement>()
const textareaRef = ref<HTMLTextAreaElement>()
const MAX_FILES_PER_MSG = 10
const MAX_FILE_SIZE_BYTES = 50 * 1024 * 1024  // 与后端 UPLOAD_MAX_FILE_SIZE 对齐

// ── Agent 模式开关 ──
const AGENT_MODE_KEY = 'cf_agent_mode'
const agentMode = ref(true)
const tipVisible = ref(false)
const tipText = ref('')
const flipping = ref(false)
let tipTimer: ReturnType<typeof setTimeout> | null = null

onMounted(() => {
  const saved = localStorage.getItem(AGENT_MODE_KEY)
  if (saved !== null) agentMode.value = saved === 'true'
  emit('agent-change', agentMode.value)

  // 主题封面图预热：首次打开 PPT 画廊时 8 张 WebP 已在浏览器磁盘缓存里
  const warmup = () => {
    for (const t of pptThemesWithUri) {
      const img = new Image()
      img.decoding = 'async'
      img.src = t.image
    }
  }
  const idle = (window as any).requestIdleCallback as ((fn: () => void) => void) | undefined
  if (idle) idle(warmup)
  else setTimeout(warmup, 120)
})

function toggleAgent() {
  if (flipping.value) return
  flipping.value = true
  // 压扁到 0 时切换状态，再弹回来
  setTimeout(() => {
    agentMode.value = !agentMode.value
    localStorage.setItem(AGENT_MODE_KEY, String(agentMode.value))
    emit('agent-change', agentMode.value)
  }, 160)
  setTimeout(() => { flipping.value = false }, 320)

  tipText.value = agentMode.value
    ? 'Chat · 轻快直接'
    : 'Agent · 规划搜索推理'
  tipVisible.value = true
  if (tipTimer) clearTimeout(tipTimer)
  tipTimer = setTimeout(() => { tipVisible.value = false }, 2000)
}

// ── 意图胶囊（PPT / 深研 / 造物 / 书写）──
// 四种胶囊共享一个选配面板槽位；PPT 用主题画廊，其余三种用"档位网格"
type PickerKind = 'ppt' | 'research' | 'code' | 'writing'
const activePicker = ref<PickerKind | null>(null)
const selectedPptTheme = ref<{ id: string; label: string } | null>(null)

interface ModeProfile {
  id: string
  label: string
  desc: string
  accent: string  // 用于胶囊环配色
  prompt: string  // 完整提示词，发送给后端
}

const MODE_META: Record<Exclude<PickerKind, 'ppt'>, { title: string; accent: string; profiles: ModeProfile[] }> = {
  research: {
    title: '选择研究配方',
    accent: '#8B5CF6',
    profiles: [
      {
        id: 'brief', label: '简报', desc: '300 字内 · 结构化要点 · 直给结论', accent: '#8B5CF6',
        prompt: '【任务】生成一份结构化简报，300 字以内，直接给结论。\n\n【要求】\n- 结论先行，再给支撑要点（不超过 3 条）\n- 语言简洁，不废话\n- 不确定的事实调用工具核查\n\n【澄清协议】以下情况先问清楚再答：话题涉及多个对立观点、用户未明确立场偏好、需要引用外部数据但不确定来源。',
      },
      {
        id: 'standard', label: '深解', desc: '多源交叉 · 正反对比 · 可追溯引用', accent: '#6D28D9',
        prompt: '【任务】对话题做深度研究，输出结构化分析报告。\n\n【要求】\n- 多源交叉验证：至少引用 2 个不同来源\n- 列出正方和反方观点（如果存在争议）\n- 每条论点标注来源（标题 + 链接或出处）\n- 结论基于证据，不基于猜测\n\n【澄清协议】以下情况先调用 request_clarification：话题涉及专业领域术语但用户未说明背景、话题存在多种解释框架、用户未明确分析深度（入门/专业/学术）。',
      },
      {
        id: 'academic', label: '学者', desc: '文献级严谨 · 定义/方法/局限/展望', accent: '#4C1D95',
        prompt: '【任务】以学术规范对话题进行系统性分析，产出接近文献综述水平的内容。\n\n【要求】\n- 严格定义核心概念（给出 2-3 种代表性定义并比较）\n- 梳理研究方法论（定量/定性/案例等）\n- 分析局限性（数据、方法、视角）\n- 指出未来研究方向\n- 所有引用必须可查证\n\n【澄清协议】以下情况先问清楚：用户未说明目标读者是学术/专业/大众、话题的学科归属不明确、用户未说明需要的引用风格（APA/MLA/GB/T）。',
      },
    ],
  },
  code: {
    title: '选择代码脚手架',
    accent: '#10B981',
    profiles: [
      {
        id: 'cli', label: '命令行', desc: '单文件脚本 · 带参数解析 · 自含依赖', accent: '#059669',
        prompt: '【任务】生成一个完整可运行的命令行工具脚本。\n\n【要求】\n- 单文件解决，不额外拆分文件\n- 必须含参数解析（argparse/opt 等）\n- 依赖必须注明，且只依赖标准库或用户已明确的库\n- 包含 --help 说明\n- 代码必须可直接运行\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明目标语言（Python/Shell/Go/Rust 等）、未说明运行环境（Linux/macOS/Windows）、脚本涉及文件IO但未说明路径规范。',
      },
      {
        id: 'web', label: 'Web 全栈', desc: '前后端最小闭环 · 本地即跑', accent: '#0D9488',
        prompt: '【任务】根据用户需求构建一个前后端完整可运行的 Web 应用。\n\n【工作流程】\n1. 确认技术栈偏好（前端框架 / 后端语言 / 数据库）\n2. 生成完整代码，确保本地可运行\n3. 说明启动方式和依赖安装\n\n【产物要求】\n- 代码完整，不依赖未声明的外部库\n- 前后端分离，提供启动说明\n- 遵循所选技术栈的最佳实践\n- 不要在聊天里复述完整源码——告知用户文件路径和启动方式即可\n\n【澄清协议】以下情况必须先调用 request_clarification：用户未指定前端框架（React/Vue/Angular/纯HTML等）、后端语言和框架（Node/Flask/FastAPI/Django等）、数据库选型（MySQL/PostgreSQL/SQLite/MongoDB等）、是否需要用户认证和 API 接口设计。',
      },
      {
        id: 'algo', label: '算法题', desc: '推导 · 多解法 · 复杂度 · 边界', accent: '#047857',
        prompt: '【任务】帮助用户理解、推导和实现算法。\n\n【工作流程】\n1. 理解问题：确认输入输出、边界条件、特殊用例\n2. 推导思路：分析最优解法及时间 / 空间复杂度\n3. 提供多解法：穷举 → 优化 → 最优，讲解每种取舍\n4. 给出完整代码实现\n5. 验证边界情况和极端用例\n\n【产物要求】\n- 代码必须完整可运行\n- 每种解法标注时间 / 空间复杂度\n- 要有边界条件处理\n- 讲解帮助理解，不是直接给答案\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明编程语言偏好、未明确输入数据规模和取值范围、题目来自在线评测（OJ）但未提供链接或原始描述。',
      },
      {
        id: 'lib', label: '库/模块', desc: '抽象 API · 单测示例 · 可复用', accent: '#065F46',
        prompt: '【任务】生成一个可复用的库或模块代码。\n\n【要求】\n- 抽象为独立 API 模块，不耦合具体业务逻辑\n- 包含完整的函数 / 类文档注释\n- 提供单元测试示例\n- 导出接口清晰，附使用示例\n- 不依赖未声明的第三方库\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明目标语言（Python/JS/Go/Rust 等）、未说明是否需要类型注解、未说明调用方式偏好（同步/异步）。',
      },
    ],
  },
  writing: {
    title: '选择书写体裁',
    accent: '#FB7299',
    profiles: [
      {
        id: 'weixin', label: '公众号', desc: '长文 · 有节奏的小标题', accent: '#FB7299',
        prompt: '【任务】按公众号风格撰写一篇结构化长文。\n\n【要求】\n- 长度 1500-3000 字\n- 有节奏感的小标题（3-5 个）\n- 开头抓人（痛点 / 故事 / 数据）\n- 结尾有行动号召或情感共鸣\n- 语言风格：专业但不晦涩，接地气\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明文章主题和核心观点、未说明目标读者是谁（职场/学生/专业/大众）、未说明希望突出的重点（案例/数据/情感/方法论）。',
      },
      {
        id: 'xhs', label: '小红书', desc: '短段 · emoji 点缀 · 标签后缀', accent: '#EF4444',
        prompt: '【任务】按小红书风格生成一篇吸引人的短内容。\n\n【要求】\n- 每段控制在 3 行以内，留白透气\n- 善用 emoji 点缀（但不过度）\n- 结尾加标签（#标签名 格式，3-8 个）\n- 开头要有钩子（数字 / 痛点 / 反差）\n- 总字数控制在 500-800 字\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明内容主题和核心卖点、未说明目标人群（学生党/职场人/宝妈等）、未说明内容调性（干货/情感/搞笑/种草）。',
      },
      {
        id: 'email', label: '邮件', desc: '简洁 · 语气考究 · 结尾有 CTA', accent: '#DC2626',
        prompt: '【任务】撰写一封专业邮件。\n\n【要求】\n- 主题行清晰，不超过 50 字\n- 正文简洁，3 段以内（背景 / 核心内容 / 行动）\n- 语气得体（尊重但不卑微）\n- 结尾有明确 CTA（希望对方做什么）\n- 格式规范，适合商务场景\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明收件人和发件人关系（上下级/平级/客户/合作方）、未说明邮件目的（申请/汇报/感谢/通知/邀请）、未说明紧急程度（常规/加急）。',
      },
      {
        id: 'story', label: '短篇故事', desc: '场景+对白+余韵 · 1500 字内', accent: '#B91C1C',
        prompt: '【任务】撰写一个完整的短篇故事。\n\n【要求】\n- 长度 800-1500 字\n- 有具体场景和人物\n- 有对白，不要通篇叙述\n- 结尾有余韵，不说破，留给读者想象\n- 情感真实，不矫情\n\n【澄清协议】以下情况先调用 request_clarification：用户未说明故事主题或核心情感（爱情/成长/悬疑/温情等）、未说明目标读者年龄层（儿童/青少年/成人）、未说明故事背景时代（现代/民国/科幻/奇幻）。',
      },
    ],
  },
}

const selectedMode = ref<{ kind: 'research' | 'code' | 'writing'; profile: ModeProfile } | null>(null)

// 当前活动的"档位"面板（PPT 不走这条） — 给模板一个已窄化的句柄
const activeModeKind = computed<'research' | 'code' | 'writing' | null>(() => {
  const k = activePicker.value
  return k && k !== 'ppt' ? k : null
})

function openCapsule(kind: PickerKind) {
  activePicker.value = activePicker.value === kind ? null : kind
}

function selectModeProfile(kind: 'research' | 'code' | 'writing', profile: ModeProfile) {
  selectedMode.value = { kind, profile }
  activePicker.value = null
  setTimeout(() => textareaRef.value?.focus(), 80)
}

function clearSelectedMode() {
  selectedMode.value = null
}

function modeKindLabel(kind: 'research' | 'code' | 'writing'): string {
  return kind === 'research' ? '深研' : kind === 'code' ? '造物' : '书写'
}

defineExpose({ openCapsule })

interface PptTheme {
  id: string            // 随 [PPT:xxx] 提示词送给后端
  label: string         // 胶囊标签 / 气泡里显示
  desc: string          // 卡片副标题
  image: string         // 缩略图 + 送给模型的 mood board（/public 下静态资源）
  tint: string          // 卡片底部遮罩色（cover 风封面字叠加用）
}

// 8 个真图主题（/public/ppt-themes/*.webp，Unsplash CC0）
const PPT_THEMES: PptTheme[] = [
  { id: 'nordic_mist',    label: 'Nordic Mist',    desc: '雪原冷峰 · 商务汇报',  image: '/ppt-themes/nordic_mist.webp',    tint: 'rgba(12,22,38,0.72)' },
  { id: 'editorial_mono', label: 'Editorial Mono', desc: '黑白建筑 · 极致克制',  image: '/ppt-themes/editorial_mono.webp', tint: 'rgba(0,0,0,0.70)' },
  { id: 'kyoto_dusk',     label: 'Kyoto Dusk',     desc: '京都暮色 · 东方沉静',  image: '/ppt-themes/kyoto_dusk.webp',     tint: 'rgba(40,18,18,0.66)' },
  { id: 'aurora_night',   label: 'Aurora Night',   desc: '北极光 · 科技发布',    image: '/ppt-themes/aurora_night.webp',   tint: 'rgba(8,18,38,0.62)' },
  { id: 'pacific_fog',    label: 'Pacific Fog',    desc: '海雾晨光 · 学术冷静',  image: '/ppt-themes/pacific_fog.webp',    tint: 'rgba(20,32,48,0.58)' },
  { id: 'desert_gold',    label: 'Desert Gold',    desc: '沙漠金辉 · 品牌温度',  image: '/ppt-themes/desert_gold.webp',    tint: 'rgba(52,24,8,0.58)' },
  { id: 'gallery_white',  label: 'Gallery White',  desc: '美术馆白 · 极简留白',  image: '/ppt-themes/gallery_white.webp',  tint: 'rgba(0,0,0,0.50)' },
  { id: 'neon_district',  label: 'Neon District',  desc: '都市霓虹 · 潮流视觉',  image: '/ppt-themes/neon_district.webp',  tint: 'rgba(8,10,28,0.60)' },
]

// 画廊列表（保留旧 pptThemesWithUri 命名便于模板兼容，不再预合成 SVG）
const pptThemesWithUri = PPT_THEMES

// 当前主题的图片 dataURI（会放进 pendingImages），用 ref 精确跟踪，避免误删用户的其他附件
const pptThemeImageUri = ref<string | null>(null)

/** 选中主题 → fetch 本地 webp → base64 dataURI → 加入 pendingImages 作为 mood board */
async function selectPptTheme(theme: PptTheme) {
  activePicker.value = null
  try {
    const res = await fetch(theme.image)
    if (!res.ok) throw new Error(String(res.status))
    const blob = await res.blob()
    const dataUri = await new Promise<string>((resolve, reject) => {
      const r = new FileReader()
      r.onload = () => resolve(r.result as string)
      r.onerror = () => reject(r.error)
      r.readAsDataURL(blob)
    })
    // 如果之前已有主题图，先从 pending 移除
    if (pptThemeImageUri.value) {
      pendingImages.value = pendingImages.value.filter(i => i !== pptThemeImageUri.value)
    }
    pptThemeImageUri.value = dataUri
    pendingImages.value.unshift(dataUri)
    selectedPptTheme.value = { id: theme.id, label: theme.label }
  } catch (err) {
    ElMessage.error('加载主题图片失败')
    return
  }
  setTimeout(() => textareaRef.value?.focus(), 100)
}

function clearPptTheme() {
  if (pptThemeImageUri.value) {
    pendingImages.value = pendingImages.value.filter(i => i !== pptThemeImageUri.value)
    pptThemeImageUri.value = null
  }
  selectedPptTheme.value = null
}


const hasAttachments = () =>
  pendingImages.value.length > 0 ||
  pendingFiles.value.some(f => !f.uploading && !f.error)
const hasUploading = () => pendingFiles.value.some(f => f.uploading)
const canSend = () =>
  (input.value.trim() || hasAttachments()) && !props.loading && !hasUploading()

function handleSend() {
  if (!canSend()) return
  let text = input.value
  // 选了胶囊模式但没打字，自动填一个最小占位（确保 intent 能送达后端）
  if (!text.trim() && selectedMode.value) {
    text = selectedMode.value.profile.label
  }
  // intent：API 路由前缀 | intentLabel：气泡里显示的意图标签
  let intent = ''
  let intentLabel = ''
  if (selectedPptTheme.value) {
    intent = `[PPT:${selectedPptTheme.value.id}]`
    intentLabel = `做 PPT · ${selectedPptTheme.value.label}`
  } else if (selectedMode.value) {
    intent = selectedMode.value.profile.prompt
    intentLabel = `${modeKindLabel(selectedMode.value.kind)} · ${selectedMode.value.profile.label}`
  }
  // 只发送"已上传成功"的文件（有 id，且未标记 error/uploading）
  const readyFiles: UploadedFile[] = pendingFiles.value
    .filter(f => !f.uploading && !f.error && f.id)
    .map(f => ({
      id: f.id, name: f.name, size: f.size,
      path: f.path, language: f.language, mime: f.mime,
    }))
  emit('send', {
    text,
    images: [...pendingImages.value],
    agentMode: agentMode.value,
    files: readyFiles,
    intent,
    intentLabel,
  })
  input.value = ''
  pendingImages.value = []
  pendingFiles.value = []
  selectedPptTheme.value = null
  pptThemeImageUri.value = null
  selectedMode.value = null
  if (textareaRef.value) textareaRef.value.style.height = 'auto'
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSend() }
}

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

function compressImage(dataUrl: string, maxPx = 1280, quality = 0.82): Promise<string> {
  return new Promise((resolve) => {
    const img = new Image()
    img.onload = () => {
      let { width, height } = img
      if (width > maxPx || height > maxPx) {
        if (width >= height) { height = Math.round(height * maxPx / width); width = maxPx }
        else { width = Math.round(width * maxPx / height); height = maxPx }
      }
      const canvas = document.createElement('canvas')
      canvas.width = width; canvas.height = height
      canvas.getContext('2d')!.drawImage(img, 0, 0, width, height)
      resolve(canvas.toDataURL('image/jpeg', quality))
    }
    img.onerror = () => resolve(dataUrl)
    img.src = dataUrl
  })
}

async function addImageFile(file: File) {
  if (!file.type.startsWith('image/')) return
  const reader = new FileReader()
  reader.onload = async ev => {
    const raw = ev.target?.result as string
    if (!raw) return
    pendingImages.value.push(await compressImage(raw))
  }
  reader.readAsDataURL(file)
}

/** 非图片文件：调用 /api/files/upload 上传到沙箱 + artifacts。 */
async function addAttachmentFile(file: File) {
  // 前端尺寸校验（后端也会再校验一次）
  if (file.size > MAX_FILE_SIZE_BYTES) {
    ElMessage.warning(`文件过大（>${MAX_FILE_SIZE_BYTES / 1024 / 1024}MB）：${file.name}`)
    return
  }
  if (pendingFiles.value.length >= MAX_FILES_PER_MSG) {
    ElMessage.warning(`单次最多附 ${MAX_FILES_PER_MSG} 个文件`)
    return
  }
  // 需要当前对话 ID 才能上传；若没有，请求父组件创建，并轮询等待 prop 更新
  if (!props.currentConvId) {
    emit('ensureConv')
    const deadline = Date.now() + 8000
    while (!props.currentConvId && Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 50))
    }
  }
  const convId = props.currentConvId
  if (!convId) {
    ElMessage.error('无法上传：对话未就绪，请稍候重试')
    return
  }
  const _localId = `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
  const entry: PendingFile = {
    _localId, id: 0, name: file.name, size: file.size, uploading: true,
  }
  pendingFiles.value.push(entry)
  try {
    const meta = await apiUploadFile(convId, file)
    const idx = pendingFiles.value.findIndex(f => f._localId === _localId)
    if (idx >= 0) {
      pendingFiles.value[idx] = { ...entry, ...meta, uploading: false, error: undefined }
    }
  } catch (exc: any) {
    const idx = pendingFiles.value.findIndex(f => f._localId === _localId)
    if (idx >= 0) {
      pendingFiles.value[idx] = { ...entry, uploading: false, error: exc?.message || '上传失败' }
    }
    ElMessage.error(`上传失败：${file.name} — ${exc?.message || ''}`)
  }
}

function routeIncomingFile(file: File) {
  if (file.type.startsWith('image/')) addImageFile(file)
  else addAttachmentFile(file)
}

function handlePaste(e: ClipboardEvent) {
  for (const item of Array.from(e.clipboardData?.items || [])) {
    if (item.kind !== 'file') continue
    const f = item.getAsFile()
    if (!f) continue
    e.preventDefault()
    routeIncomingFile(f)
  }
}
function handleFileSelect(e: Event) {
  const target = e.target as HTMLInputElement
  for (const f of Array.from(target.files || [])) routeIncomingFile(f)
  target.value = ''
}
function handleDrop(e: DragEvent) {
  e.preventDefault()
  for (const f of Array.from(e.dataTransfer?.files || [])) routeIncomingFile(f)
}
function removeImage(i: number) { pendingImages.value.splice(i, 1) }
function removePendingFile(localId: string) {
  const idx = pendingFiles.value.findIndex(f => f._localId === localId)
  if (idx >= 0) pendingFiles.value.splice(idx, 1)
}
function fmtFileSize(n: number): string {
  if (n >= 1024 * 1024) return (n / 1024 / 1024).toFixed(1) + 'MB'
  if (n >= 1024) return (n / 1024).toFixed(1) + 'KB'
  return n + 'B'
}

// ── 上传文件预览模态（input 阶段，发送前/后都可点 chip 预览） ────────────────
const previewVisible = ref(false)
const previewFile = ref<{ id: number; name: string; size: number; path?: string } | null>(null)
function openPendingPreview(f: PendingFile) {
  if (f.uploading || f.error || !f.id) return  // 仅就绪后可预览
  // 注：不传 language —— 渲染器派发完全靠文件名后缀，与后端 detect_language 解耦
  previewFile.value = {
    id: f.id, name: f.name, size: f.size || 0, path: f.path,
  }
  previewVisible.value = true
}
</script>

<template>
  <div class="input-root" :class="{ centered }" @dragover.prevent @drop="handleDrop">
    <div class="input-card" :class="{ 'is-loading': loading }">

      <!-- 已选主题标签 + 意图模式标签 + 图片预览 + 附件文件 -->
      <div v-if="selectedPptTheme || selectedMode || pendingImages.length > 0 || pendingFiles.length > 0" class="attachments-bar">
        <!-- PPT 主题标签 -->
        <div v-if="selectedPptTheme" class="ppt-tag">
          <span class="ppt-tag-label">📊 {{ selectedPptTheme.label }}</span>
          <button class="ppt-tag-close" @click="clearPptTheme" title="取消PPT模式">
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <!-- 意图模式标签（深研 / 造物 / 书写） -->
        <div
          v-if="selectedMode"
          class="mode-tag"
          :style="{ '--tag-accent': selectedMode.profile.accent }"
        >
          <span class="mode-tag-kind">{{ modeKindLabel(selectedMode.kind) }}</span>
          <span class="mode-tag-sep">·</span>
          <span class="mode-tag-label">{{ selectedMode.profile.label }}</span>
          <button class="mode-tag-close" @click="clearSelectedMode" title="取消此意图">
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <!-- 图片预览 -->
        <div v-for="(img, i) in pendingImages" :key="i" class="img-thumb">
          <img :src="img" alt="图片" />
          <button class="img-remove" @click="removeImage(i)" title="移除">
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <!-- 文件附件（非图片）-->
        <div
          v-for="f in pendingFiles" :key="f._localId"
          class="file-chip"
          :class="{
            'file-chip--uploading': f.uploading,
            'file-chip--error': !!f.error,
            'file-chip--clickable': !f.uploading && !f.error && !!f.id,
          }"
          :title="f.error ? f.error : (f.uploading ? '上传中...' : `预览 ${f.name}`)"
          @click="openPendingPreview(f)"
        >
          <el-icon v-if="f.uploading" class="file-chip-ico spin"><Loading /></el-icon>
          <svg v-else class="file-chip-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
            <path d="M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8z"/>
            <polyline points="14 2 14 8 20 8"/>
          </svg>
          <span class="file-chip-name">{{ f.name }}</span>
          <span class="file-chip-size">{{ fmtFileSize(f.size) }}</span>
          <button class="file-chip-close" @click.stop="removePendingFile(f._localId)" title="移除">
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
      </div>

      <!-- 上传文件预览模态：发送前可点 chip 预览自己上传的内容 -->
      <UploadedFilePreview v-model="previewVisible" :file="previewFile" />

      <div class="textarea-area">
        <textarea
          ref="textareaRef" v-model="input"
          @keydown="handleKeydown" @paste="handlePaste" @input="autoResize"
          :placeholder="centered ? '随便问点什么吧~ (●ˇ∀ˇ●)' : '发消息... （Enter 发送 · Shift+Enter 换行 · 支持粘贴截图）'"
          :disabled="loading" rows="1" class="the-textarea"
        />
      </div>

      <div class="toolbar">
        <div class="tl">
          <input ref="fileInputRef" type="file" accept="image/*" multiple style="display:none" @change="handleFileSelect" />
          <input ref="attachInputRef" type="file" multiple style="display:none" @change="handleFileSelect" />
          <el-tooltip content="上传图片 / 粘贴截图 (Ctrl+V)" placement="top" :show-after="400">
            <button class="tool-btn" @click="fileInputRef?.click()" :disabled="loading">
              <el-icon><Picture /></el-icon>
            </button>
          </el-tooltip>
          <el-tooltip content="上传文件（代码 / 文档 / 压缩包等）" placement="top" :show-after="400">
            <button class="tool-btn" @click="attachInputRef?.click()" :disabled="loading">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
                <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.49"/>
              </svg>
            </button>
          </el-tooltip>

          <!-- ═══ Agent / Chat 翻牌切换 ═══ -->
          <button
            class="mode-flip"
            :class="{ 'mode-flip--ani': flipping }"
            @click="toggleAgent"
            :disabled="loading"
            :title="agentMode ? 'Agent 模式（点击切换）' : 'Chat 模式（点击切换）'"
          >
            <span class="mode-flip-inner">
              <!-- 内容随 agentMode 实时切换，动画只是视觉挤压弹回 -->
              <template v-if="agentMode">
                <svg class="mode-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#00AEEC" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <rect x="4" y="8" width="16" height="12" rx="3"/>
                  <circle cx="9" cy="14" r="1.3" fill="#00AEEC" stroke="none"/>
                  <circle cx="15" cy="14" r="1.3" fill="#00AEEC" stroke="none"/>
                  <line x1="12" y1="4" x2="12" y2="8"/>
                  <circle cx="12" cy="3" r="1.5"/>
                </svg>
                <span class="mode-txt" style="color:#00AEEC">Agent</span>
              </template>
              <template v-else>
                <svg class="mode-ico" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="#FB7299" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                  <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>
                  <line x1="8" y1="9" x2="16" y2="9"/>
                  <line x1="8" y1="13" x2="13" y2="13"/>
                </svg>
                <span class="mode-txt" style="color:#FB7299">Chat</span>
              </template>
            </span>
          </button>

          <span v-if="pendingImages.length > 0" class="img-badge">{{ pendingImages.length }} 张图片</span>
        </div>

        <div class="tr">
          <span v-if="input.length > 20" class="char-count">{{ input.length }}</span>
          <el-tooltip :content="loading ? '生成中...' : (canSend() ? '发送 (Enter)' : '请输入内容')" placement="top" :show-after="300">
            <button class="send-btn" :class="{ active: canSend(), loading }" @click="handleSend" :disabled="!canSend()">
              <el-icon v-if="!loading" class="send-icon"><Promotion /></el-icon>
              <el-icon v-else class="spin"><Loading /></el-icon>
            </button>
          </el-tooltip>
        </div>
      </div>
    </div>

    <div class="input-footer">
      <Transition name="mode-tip">
        <div v-if="tipVisible" class="mode-tip-bar">{{ tipText }}</div>
      </Transition>
      <Transition name="mode-hint">
        <span v-if="!tipVisible" class="hint">Enter 发送 · Shift+Enter 换行</span>
      </Transition>
    </div>

    <!-- ═══ PPT 主题画廊（在输入框下方展开，URI 预计算避免卡顿） ═══ -->
    <Transition name="ppt-panel">
      <div v-if="activePicker === 'ppt'" class="ppt-gallery">
        <div class="ppt-gallery-header">
          <span class="ppt-gallery-title">选择 PPT 主题风格</span>
          <button class="ppt-gallery-close" @click="activePicker = null">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="ppt-gallery-grid">
          <button
            v-for="t in pptThemesWithUri" :key="t.id"
            class="ppt-gallery-card"
            :class="{ 'ppt-gallery-card--selected': selectedPptTheme?.id === t.id }"
            :style="{ '--cover-tint': t.tint }"
            @click="selectPptTheme(t)"
          >
            <img class="ppt-gallery-img" :src="t.image" :alt="t.label" loading="lazy" decoding="async" />
            <div class="ppt-gallery-overlay" aria-hidden="true"></div>
            <div class="ppt-gallery-caption">
              <span class="ppt-gallery-name">{{ t.label }}</span>
              <span class="ppt-gallery-desc">{{ t.desc }}</span>
            </div>
          </button>
        </div>
      </div>
    </Transition>

    <!-- ═══ 其他意图胶囊（深研 / 造物 / 书写）的档位面板 ═══ -->
    <Transition name="ppt-panel">
      <div
        v-if="activeModeKind"
        class="mode-picker"
        :style="{ '--picker-accent': MODE_META[activeModeKind].accent }"
      >
        <div class="mode-picker-header">
          <span class="mode-picker-title">{{ MODE_META[activeModeKind].title }}</span>
          <button class="mode-picker-close" @click="activePicker = null">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="mode-picker-grid">
          <button
            v-for="p in MODE_META[activeModeKind].profiles" :key="p.id"
            class="mode-picker-card"
            :class="{ 'mode-picker-card--selected': selectedMode?.kind === activeModeKind && selectedMode?.profile.id === p.id }"
            :style="{ '--card-accent': p.accent }"
            @click="selectModeProfile(activeModeKind, p)"
          >
            <span class="mode-picker-name">{{ p.label }}</span>
            <span class="mode-picker-desc">{{ p.desc }}</span>
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
.input-root { width: 100%; }
.input-root.centered { max-width: 680px; margin: 0 auto; }

.input-card {
  background: var(--cf-card, #fff);
  border: 1.5px solid var(--cf-border, #DFE3E8);
  border-radius: var(--cf-radius-md, 14px);
  box-shadow: var(--cf-shadow-xs);
  overflow: hidden;
  transition: box-shadow 0.3s, border-color 0.3s;
}
.input-card:focus-within {
  border-color: var(--cf-bili-blue, #00AEEC);
  box-shadow: var(--cf-shadow-sm), 0 0 0 3px rgba(0,174,236,0.08), 0 0 16px rgba(0,174,236,0.06);
}
.input-card.is-loading { opacity: 0.75; }

.img-previews { display: flex; flex-wrap: wrap; gap: 8px; padding: 12px 14px 0; }
.img-thumb { position: relative; width: 68px; height: 68px; border-radius: 12px; overflow: hidden; border: 1.5px solid var(--cf-border); }
.img-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
.img-remove {
  position: absolute; top: 3px; right: 3px; width: 18px; height: 18px; border-radius: 50%;
  background: rgba(0,0,0,0.6); color: #fff; border: none; cursor: pointer;
  display: flex; align-items: center; justify-content: center; padding: 0;
}
.img-remove:hover { background: rgba(242,93,89,0.9); }

/* 文件附件 chip */
.file-chip {
  display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 8px 0 10px;
  background: #F4F5F7; border: 1.5px solid var(--cf-border, #DFE3E8);
  border-radius: 10px; font-size: 12px; color: var(--cf-text-2, #61666D);
  max-width: 240px;
  transition: all 0.15s;
}
.file-chip:hover { background: #EBECEF; }
.file-chip-ico { color: #00AEEC; flex-shrink: 0; }
.file-chip-name {
  max-width: 140px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  font-weight: 500; color: var(--cf-text-1, #18191C);
}
.file-chip-size { color: var(--cf-text-4, #9499A0); font-variant-numeric: tabular-nums; }
.file-chip-close {
  width: 16px; height: 16px; border-radius: 50%; border: none; background: transparent;
  color: var(--cf-text-3, #9499A0); cursor: pointer;
  display: flex; align-items: center; justify-content: center; padding: 0;
  transition: all 0.1s;
}
.file-chip-close:hover { background: rgba(0,0,0,0.08); color: #F25D59; }
.file-chip--uploading { opacity: 0.7; border-style: dashed; }
.file-chip--uploading .file-chip-ico { color: #FB7299; }
.file-chip--error { border-color: #F25D59; background: #FFF4F3; color: #F25D59; }
.file-chip--error .file-chip-ico { color: #F25D59; }
.file-chip--clickable { cursor: pointer; }
.file-chip--clickable:hover { border-color: #00AEEC; background: #E3F6FD; }

.textarea-area { padding: 14px 18px 6px; }
.the-textarea {
  width: 100%; background: none; border: none; outline: none;
  font-size: 14.5px; font-family: inherit; font-weight: 400; line-height: 1.65;
  color: var(--cf-text-1); resize: none; max-height: 220px; overflow-y: auto;
}
.the-textarea::placeholder { color: var(--cf-text-4); }

.toolbar { display: flex; align-items: center; justify-content: space-between; padding: 6px 14px 10px; }
.tl, .tr { display: flex; align-items: center; gap: 6px; }

.tool-btn {
  width: 30px; height: 30px; border-radius: 8px; background: none; border: none;
  color: #00AEEC; cursor: pointer; display: flex; align-items: center; justify-content: center; font-size: 17px;
  transition: all 0.15s; opacity: 0.7;
}
.tool-btn:hover:not(:disabled) { opacity: 1; background: rgba(0,174,236,0.08); }
.tool-btn:disabled { opacity: 0.25; cursor: not-allowed; }

.img-badge { font-size: 11px; color: #00AEEC; background: rgba(0,174,236,0.06); padding: 2px 8px; border-radius: 10px; font-weight: 500; }
.char-count { font-size: 11px; color: var(--cf-text-4); font-variant-numeric: tabular-nums; }

.send-btn {
  width: 32px; height: 32px; border-radius: 10px;
  background: #E3E5E7; color: #9499A0;
  border: none; cursor: pointer;
  display: flex; align-items: center; justify-content: center; font-size: 15px;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.send-btn.active {
  background: #00AEEC;
  color: #fff;
  box-shadow: 0 2px 8px rgba(0,174,236,0.3);
}
.send-btn.active:hover { transform: scale(1.06); box-shadow: 0 3px 12px rgba(0,174,236,0.35); }
.send-btn:disabled:not(.active) { cursor: not-allowed; opacity: 0.5; }
.send-icon { font-size: 14px; }
.spin { font-size: 15px; animation: spin 0.8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }

/* ═══════════════════════════════════════════════════════════════════
   翻牌切换 — 无边框、极浅色、Bilibili 简笔画线条风
   用 scaleX 压扁→切换内容→弹回，不用 3D 翻转，逻辑简单不出错
   ═══════════════════════════════════════════════════════════════════ */
.mode-flip {
  display: inline-flex;
  align-items: center;
  height: 28px;
  padding: 0 8px;
  margin-left: 2px;                /* 标准间距，由 .tl 的 gap: 6px 控制 */
  border: none;
  border-radius: 8px;
  background: transparent;
  cursor: pointer;
  transition: background 0.15s, transform 0.15s;
  position: relative;
}
.mode-flip:hover:not(:disabled) {
  background: rgba(0,0,0,0.03);
}
.mode-flip:active:not(:disabled) {
  transform: scale(0.94);
}
.mode-flip:disabled { opacity: 0.4; cursor: not-allowed; }

.mode-flip-inner {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  transition: transform 0.32s cubic-bezier(0.34,1.56,0.64,1);
}

/* 压扁动画 */
.mode-flip--ani .mode-flip-inner {
  animation: flip-squash 0.32s cubic-bezier(0.34,1.56,0.64,1);
}
@keyframes flip-squash {
  0%   { transform: scaleX(1) scaleY(1); }
  45%  { transform: scaleX(0) scaleY(1.15); }
  55%  { transform: scaleX(0) scaleY(1.15); }
  100% { transform: scaleX(1) scaleY(1); }
}

.mode-ico {
  flex-shrink: 0;
  display: block;
}

.mode-txt {
  font-size: 12.5px;
  font-weight: 600;
  letter-spacing: 0.2px;
  line-height: 1;
  white-space: nowrap;
}

/* ── 底部提示 ── */
.input-footer { position: relative; height: 28px; margin-top: 6px; }

.mode-tip-bar {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  padding: 0 14px; border-radius: 10px;
  background: rgba(0,0,0,0.025);
  color: #999;
  font-size: 11.5px;
  font-weight: 400;
  letter-spacing: 0.1px;
  white-space: nowrap;
  animation: tip-fade 0.25s ease-out;
}
@keyframes tip-fade {
  from { opacity: 0; transform: translateY(4px); }
  to { opacity: 1; transform: translateY(0); }
}

.hint {
  position: absolute; inset: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 12px; color: #9499A0; pointer-events: none; font-weight: 400;
}

.mode-tip-enter-active, .mode-tip-leave-active { transition: opacity 0.2s; }
.mode-tip-enter-from, .mode-tip-leave-to { opacity: 0; }
.mode-hint-enter-active, .mode-hint-leave-active { transition: opacity 0.2s; }
.mode-hint-enter-from, .mode-hint-leave-to { opacity: 0; }

/* ═══════════════════════════════════════════════════════════════════
   附件栏（PPT 主题标签 + 图片预览）
   ═══════════════════════════════════════════════════════════════════ */
.attachments-bar {
  display: flex; flex-wrap: wrap; gap: 8px; padding: 10px 14px 0; align-items: center;
}

/* PPT 主题标签 */
.ppt-tag {
  display: inline-flex; align-items: center; gap: 6px;
  height: 32px; padding: 0 12px 0 10px;
  background: linear-gradient(135deg, #FFF8F0, #FFF3E0);
  border: 1.5px solid #FFD6A5; border-radius: 10px;
  font-size: 12px; font-weight: 600; color: #E65100;
  box-shadow: 0 2px 8px rgba(255,152,0,0.15);
  transition: all 0.15s;
}
.ppt-tag:hover {
  box-shadow: 0 4px 12px rgba(255,152,0,0.25);
  transform: translateY(-1px);
}
.ppt-tag-colors { display: flex; gap: 2px; }
.ppt-tag-dot { width: 8px; height: 8px; border-radius: 50%; border: 1px solid rgba(0,0,0,0.1); }
.ppt-tag-label { white-space: nowrap; }
.ppt-tag-close {
  width: 18px; height: 18px; border-radius: 50%; border: none; background: transparent;
  color: #E65100; cursor: pointer; display: flex; align-items: center; justify-content: center;
  margin-left: 2px; transition: all 0.1s;
}
.ppt-tag-close:hover { background: rgba(230,81,0,0.12); transform: scale(1.1); }

/* ═══ PPT 按钮 ═══ */
.ppt-btn {
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1) !important;
}
.ppt-btn--active {
  color: #FF9800 !important;
  opacity: 1 !important;
  background: rgba(255,152,0,0.1) !important;
  box-shadow: 0 0 12px rgba(255,152,0,0.2);
}

/* ═══ PPT 主题画廊（输入框下方） ═══ */
.ppt-gallery {
  margin-top: 12px;
  background: linear-gradient(145deg, #ffffff, #f8f9fa);
  border: 1.5px solid #E3E5E7;
  border-radius: 16px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.1), 0 2px 8px rgba(0,0,0,0.04);
  padding: 16px 18px;
  overflow: hidden;
}
.ppt-gallery-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 14px;
  padding-bottom: 12px;
  border-bottom: 1px solid #F1F2F3;
}
.ppt-gallery-title {
  font-size: 14px; font-weight: 700; color: #18191C;
  display: flex; align-items: center; gap: 8px;
}
.ppt-gallery-title::before {
  content: '';
  display: inline-block;
  width: 4px; height: 16px;
  background: linear-gradient(180deg, #00AEEC, #FB7299);
  border-radius: 2px;
}
.ppt-gallery-close {
  width: 28px; height: 28px; border-radius: 8px; border: none; background: transparent;
  color: #9499A0; cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.15s;
}
.ppt-gallery-close:hover { background: #F1F2F3; color: #18191C; }

.ppt-gallery-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 14px;
}

.ppt-gallery-card {
  --cover-tint: rgba(0,0,0,0.6);
  display: block;
  padding: 0; border: 2.5px solid transparent; border-radius: 14px;
  background: #1a1a1a; cursor: pointer;
  transition: all 0.22s cubic-bezier(0.34,1.56,0.64,1);
  overflow: hidden;
  position: relative;
  /* cover 封面布局：图片填满，文字叠加在底部暗渐变上 */
  aspect-ratio: 16/9;
}
.ppt-gallery-card:hover {
  border-color: rgba(255,255,255,0.5);
  box-shadow: 0 8px 28px rgba(0,0,0,0.30), 0 0 0 1px rgba(255,255,255,0.08);
  transform: translateY(-5px) scale(1.02);
}
.ppt-gallery-card:active {
  transform: translateY(-2px) scale(0.99);
}
.ppt-gallery-card--selected {
  border-color: #FF9800 !important;
  box-shadow: 0 8px 28px rgba(255,152,0,0.28), 0 0 0 2px rgba(255,152,0,0.2) !important;
  transform: translateY(-4px) scale(1.015);
}

.ppt-gallery-img {
  position: absolute; inset: 0;
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
  transition: transform 0.35s ease;
}
.ppt-gallery-card:hover .ppt-gallery-img {
  transform: scale(1.07);
}

/* 底部暗渐变叠加层（文字可读性保护） */
.ppt-gallery-overlay {
  position: absolute; inset: 0;
  background: linear-gradient(
    to top,
    var(--cover-tint) 0%,
    rgba(0,0,0,0.15) 55%,
    rgba(0,0,0,0.05) 100%
  );
  transition: opacity 0.25s;
}
.ppt-gallery-card:hover .ppt-gallery-overlay {
  opacity: 0.85;
}

/* 文字区：左下角叠在图上 */
.ppt-gallery-caption {
  position: absolute;
  bottom: 0; left: 0; right: 0;
  padding: 32px 12px 12px;
  display: flex; flex-direction: column; gap: 2px;
}
.ppt-gallery-name {
  font-size: 12px; font-weight: 700;
  color: #ffffff;
  letter-spacing: 0.3px;
  line-height: 1.2;
  text-shadow: 0 1px 4px rgba(0,0,0,0.5);
  transition: color 0.15s;
}
.ppt-gallery-card--selected .ppt-gallery-name { color: #FFB74D; }
.ppt-gallery-desc {
  font-size: 10px; font-weight: 500;
  color: rgba(255,255,255,0.72);
  text-shadow: 0 1px 3px rgba(0,0,0,0.4);
}

/* 画廊动画 */
.ppt-panel-enter-active { transition: opacity 0.2s, max-height 0.25s ease-out; }
.ppt-panel-leave-active { transition: opacity 0.15s, max-height 0.2s ease-in; }
.ppt-panel-enter-from { opacity: 0; max-height: 0; }
.ppt-panel-leave-to { opacity: 0; max-height: 0; }
.ppt-panel-enter-to, .ppt-panel-leave-from { max-height: 400px; }

/* ═══════════════════════════════════════════════════════════════════
   意图芯片（深研 / 造物 / 书写）
   ═══════════════════════════════════════════════════════════════════ */
.mode-tag {
  --tag-accent: #8B5CF6;
  display: inline-flex; align-items: center; gap: 4px;
  height: 32px; padding: 0 10px 0 12px;
  background: color-mix(in srgb, var(--tag-accent) 8%, #fff);
  border: 1.5px solid color-mix(in srgb, var(--tag-accent) 45%, #fff);
  border-radius: 10px;
  font-size: 12px; font-weight: 600;
  color: var(--tag-accent);
  box-shadow: 0 2px 8px color-mix(in srgb, var(--tag-accent) 20%, transparent);
  transition: all 0.15s;
}
.mode-tag:hover {
  transform: translateY(-1px);
  box-shadow: 0 4px 12px color-mix(in srgb, var(--tag-accent) 30%, transparent);
}
.mode-tag-kind { font-weight: 700; letter-spacing: 0.3px; }
.mode-tag-sep { opacity: 0.5; margin: 0 2px; }
.mode-tag-label { font-weight: 500; }
.mode-tag-close {
  width: 18px; height: 18px; border-radius: 50%; border: none; background: transparent;
  color: var(--tag-accent); cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  margin-left: 2px; transition: all 0.1s;
}
.mode-tag-close:hover {
  background: color-mix(in srgb, var(--tag-accent) 15%, transparent);
  transform: scale(1.1);
}

/* ═══════════════════════════════════════════════════════════════════
   意图档位面板（复用 ppt-panel 过渡动画）
   ═══════════════════════════════════════════════════════════════════ */
.mode-picker {
  --picker-accent: #8B5CF6;
  margin-top: 12px;
  background: linear-gradient(145deg,
    #ffffff,
    color-mix(in srgb, var(--picker-accent) 4%, #fafbfd)
  );
  border: 1.5px solid color-mix(in srgb, var(--picker-accent) 25%, #E3E5E7);
  border-radius: 16px;
  box-shadow:
    0 8px 32px color-mix(in srgb, var(--picker-accent) 12%, rgba(0,0,0,0.06)),
    0 2px 8px rgba(0,0,0,0.04);
  padding: 16px 18px;
  overflow: hidden;
}
.mode-picker-header {
  display: flex; align-items: center; justify-content: space-between;
  margin-bottom: 14px; padding-bottom: 12px;
  border-bottom: 1px solid color-mix(in srgb, var(--picker-accent) 15%, #F1F2F3);
}
.mode-picker-title {
  font-size: 14px; font-weight: 700; color: #18191C;
  display: flex; align-items: center; gap: 8px;
}
.mode-picker-title::before {
  content: '';
  display: inline-block;
  width: 4px; height: 16px;
  background: linear-gradient(180deg,
    var(--picker-accent),
    color-mix(in srgb, var(--picker-accent) 60%, #FB7299)
  );
  border-radius: 2px;
}
.mode-picker-close {
  width: 28px; height: 28px; border-radius: 8px; border: none; background: transparent;
  color: #9499A0; cursor: pointer; display: flex; align-items: center; justify-content: center;
  transition: all 0.15s;
}
.mode-picker-close:hover { background: #F1F2F3; color: #18191C; }

.mode-picker-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 12px;
}
.mode-picker-card {
  --card-accent: #8B5CF6;
  position: relative;
  display: flex; flex-direction: column; align-items: flex-start;
  gap: 4px;
  padding: 12px 14px;
  background: #fff;
  border: 1.5px solid #E3E5E7;
  border-radius: 12px;
  font-family: inherit;
  cursor: pointer;
  text-align: left;
  transition: all 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
  overflow: hidden;
}
.mode-picker-card::after {
  /* 左边缘微光带 —— 颜色即档位身份 */
  content: '';
  position: absolute;
  left: 0; top: 0; bottom: 0;
  width: 3px;
  background: var(--card-accent);
  opacity: 0.55;
  transition: opacity 0.2s, width 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.mode-picker-card:hover {
  border-color: var(--card-accent);
  transform: translateY(-3px);
  box-shadow: 0 6px 18px color-mix(in srgb, var(--card-accent) 22%, transparent);
}
.mode-picker-card:hover::after { width: 5px; opacity: 1; }
.mode-picker-card:active { transform: translateY(-1px) scale(0.98); }

.mode-picker-card--selected {
  border-color: var(--card-accent) !important;
  background: color-mix(in srgb, var(--card-accent) 6%, #fff);
  box-shadow: 0 6px 20px color-mix(in srgb, var(--card-accent) 25%, transparent) !important;
}
.mode-picker-card--selected::after { width: 5px; opacity: 1; }

.mode-picker-name {
  font-size: 13.5px; font-weight: 700; color: #18191C;
  letter-spacing: 0.2px;
  transition: color 0.15s;
}
.mode-picker-card:hover .mode-picker-name,
.mode-picker-card--selected .mode-picker-name {
  color: var(--card-accent);
}
.mode-picker-desc {
  font-size: 11.5px; color: #61666D;
  line-height: 1.4;
  font-weight: 400;
}
</style>
