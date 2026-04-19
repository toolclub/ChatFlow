<script setup lang="ts">
import { ref, onMounted } from 'vue'
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
})

function toggleAgent() {
  if (flipping.value) return
  flipping.value = true
  // 压扁到 0 时切换状态，再弹回来
  setTimeout(() => {
    agentMode.value = !agentMode.value
    localStorage.setItem(AGENT_MODE_KEY, String(agentMode.value))
  }, 160)
  setTimeout(() => { flipping.value = false }, 320)

  tipText.value = agentMode.value
    ? 'Chat · 轻快直接'
    : 'Agent · 规划搜索推理'
  tipVisible.value = true
  if (tipTimer) clearTimeout(tipTimer)
  tipTimer = setTimeout(() => { tipVisible.value = false }, 2000)
}

// ── PPT 主题选择器（图片缩略图版） ──
const pptPanelOpen = ref(false)
const selectedPptTheme = ref<{ id: string; label: string } | null>(null)

interface PptTheme {
  id: string
  label: string
  desc: string
  bg: string       // 背景色
  primary: string   // 标题色
  accent: string    // 装饰色
  textColor: string // 正文色
}

const PPT_THEMES: PptTheme[] = [
  // 商务风格
  { id: 'corp_blue',      label: '企业蓝',      desc: '商务汇报',   bg: '#FFFFFF', primary: '#1E3A5F', accent: '#2563EB', textColor: '#64748B' },
  { id: 'exec_dark',      label: 'Executive',   desc: '高管提案',   bg: '#0F172A', primary: '#F8FAFC', accent: '#F59E0B', textColor: '#94A3B8' },
  { id: 'startup',        label: 'Startup',     desc: '创业路演',   bg: '#FFFFFF', primary: '#7C3AED', accent: '#06B6D4', textColor: '#64748B' },
  // 动漫风格
  { id: 'sakura',         label: '樱花物语',    desc: '日系唯美',   bg: '#FFF0F5', primary: '#E91E8C', accent: '#FFB7C5', textColor: '#8B7355' },
  { id: 'neon_tokyo',     label: '霓虹东京',    desc: '赛博朋克',   bg: '#0D0D1A', primary: '#FF2D78', accent: '#00FFFF', textColor: '#B0B0B0' },
  { id: 'fantasy',        label: '梦幻森林',    desc: '童话魔法',   bg: '#F0FDF4', primary: '#059669', accent: '#A855F7', textColor: '#6B7280' },
  // 创意风格
  { id: 'watercolor',     label: '水彩艺术',   desc: '创意设计',   bg: '#FEFCE8', primary: '#D946EF', accent: '#F59E0B', textColor: '#92400E' },
  { id: 'geometric',       label: '几何艺术',    desc: '抽象装饰',   bg: '#FAFAFA', primary: '#18181B', accent: '#EF4444', textColor: '#525252' },
  { id: 'retro',          label: '复古潮流',    desc: '怀旧情怀',   bg: '#FEF3C7', primary: '#B45309', accent: '#DC2626', textColor: '#78350F' },
  // 自然风格
  { id: 'mountain',       label: '山水禅意',    desc: '东方美学',   bg: '#F5F5F4', primary: '#1C1917', accent: '#78716C', textColor: '#57534E' },
  { id: 'ocean',          label: '深海探秘',    desc: '海洋生态',   bg: '#F0F9FF', primary: '#0369A1', accent: '#0891B2', textColor: '#0C4A6E' },
  { id: 'aurora',         label: '极光星夜',    desc: '北欧风情',   bg: '#0F1729', primary: '#E0F2FE', accent: '#A78BFA', textColor: '#7DD3FC' },
]

/** 生成精美4K风格PPT主题缩略图 */
function buildThemeSvg(t: PptTheme): string {
  const isDark = ['#0F172A','#0D0D1A','#0F1729','#1E1E2E'].includes(t.bg)

  // 通用滤镜定义
  const defs = `
    <defs>
      <filter id="shadow" x="-20%" y="-20%" width="140%" height="140%">
        <feDropShadow dx="0" dy="4" stdDeviation="6" flood-color="${isDark ? 'rgba(0,0,0,0.5)' : 'rgba(0,0,0,0.1)'}" flood-opacity="1"/>
      </filter>
      <filter id="glow" x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="3" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
      <filter id="softGlow" x="-30%" y="-30%" width="160%" height="160%">
        <feGaussianBlur stdDeviation="2" result="blur"/>
        <feMerge><feMergeNode in="blur"/><feMergeNode in="SourceGraphic"/></feMerge>
      </filter>
    </defs>`

  // 各主题专属设计
  const designs: Record<string, string> = {

    // ═══════════════════════════════════════════════════════════════
    // 商务风格
    // ═══════════════════════════════════════════════════════════════

    corp_blue: `
      <rect width="200" height="120" rx="8" fill="#FFFFFF"/>
      <!-- 顶部蓝色条带 -->
      <rect x="0" y="0" width="200" height="6" rx="0" fill="${t.primary}"/>
      <!-- 左侧装饰竖条 -->
      <rect x="20" y="25" width="5" height="35" rx="2.5" fill="${t.accent}"/>
      <!-- 标题 -->
      <rect x="32" y="28" width="80" height="10" rx="3" fill="${t.primary}"/>
      <rect x="32" y="44" width="55" height="5" rx="2.5" fill="#E2E8F0"/>
      <!-- 主内容卡片 -->
      <rect x="20" y="60" width="110" height="50" rx="6" fill="#F8FAFC" filter="url(#shadow)"/>
      <!-- 图表区域 -->
      <rect x="30" y="70" width="40" height="6" rx="2" fill="${t.accent}"/>
      <rect x="30" y="82" width="90" height="4" rx="2" fill="#E2E8F0"/>
      <rect x="30" y="92" width="75" height="4" rx="2" fill="#E2E8F0"/>
      <rect x="30" y="102" width="60" height="4" rx="2" fill="#E2E8F0"/>
      <!-- 右侧数据卡片 -->
      <rect x="140" y="30" width="45" height="80" rx="6" fill="white" filter="url(#shadow)"/>
      <circle cx="162" cy="60" r="18" fill="none" stroke="#E2E8F0" stroke-width="6"/>
      <circle cx="162" cy="60" r="18" fill="none" stroke="${t.accent}" stroke-width="6" stroke-dasharray="75 113" stroke-linecap="round" transform="rotate(-90 162 60)"/>
      <rect x="148" y="88" width="30" height="5" rx="2" fill="${t.primary}"/>
      <rect x="148" y="97" width="20" height="4" rx="2" fill="#E2E8F0"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#CBD5E1"/>
    `,

    exec_dark: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 背景光晕 -->
      <circle cx="180" cy="30" r="60" fill="${t.accent}" opacity="0.05" filter="url(#glow)"/>
      <!-- 顶部金色条带 -->
      <rect x="0" y="0" width="200" height="5" rx="0" fill="${t.accent}" filter="url(#softGlow)"/>
      <!-- 标题 -->
      <rect x="25" y="25" width="70" height="12" rx="3" fill="${t.primary}"/>
      <rect x="25" y="43" width="45" height="5" rx="2.5" fill="rgba(255,255,255,0.3)"/>
      <!-- 主卡片 -->
      <rect x="25" y="60" width="100" height="50" rx="6" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.1)" stroke-width="1"/>
      <!-- 数据展示 -->
      <rect x="35" y="72" width="35" height="8" rx="2" fill="${t.accent}" filter="url(#softGlow)"/>
      <rect x="35" y="86" width="75" height="4" rx="2" fill="rgba(255,255,255,0.15)"/>
      <rect x="35" y="96" width="60" height="4" rx="2" fill="rgba(255,255,255,0.1)"/>
      <!-- 右侧图表 -->
      <rect x="135" y="30" width="50" height="80" rx="6" fill="rgba(255,255,255,0.03)" stroke="${t.accent}" stroke-width="0.5" opacity="0.8"/>
      <rect x="145" y="42" width="30" height="5" rx="2" fill="${t.accent}"/>
      <!-- 折线图 -->
      <polyline points="145,95 155,80 165,88 175,70 185,78" fill="none" stroke="${t.accent}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
      <circle cx="155" cy="80" r="3" fill="${t.accent}"/>
      <circle cx="175" cy="70" r="3" fill="${t.accent}"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="rgba(255,255,255,0.2)"/>
    `,

    startup: `
      <rect width="200" height="120" rx="8" fill="#FFFFFF"/>
      <!-- 渐变顶部 -->
      <rect x="0" y="0" width="200" height="5" rx="0">
        <linearGradient id="startupGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="${t.primary}"/>
          <stop offset="100%" stop-color="${t.accent}"/>
        </linearGradient>
      </rect>
      <rect x="0" y="0" width="200" height="5" rx="0" fill="url(#startupGrad)"/>
      <!-- 装饰圆形 -->
      <circle cx="175" cy="30" r="35" fill="${t.accent}" opacity="0.08"/>
      <circle cx="180" cy="25" r="20" fill="${t.primary}" opacity="0.1"/>
      <!-- 标题 -->
      <rect x="20" y="20" width="60" height="10" rx="3" fill="${t.primary}"/>
      <rect x="20" y="36" width="40" height="5" rx="2.5" fill="${t.accent}" opacity="0.6"/>
      <!-- 内容 -->
      <rect x="20" y="55" width="75" height="55" rx="6" fill="#FAFAFA" filter="url(#shadow)"/>
      <rect x="30" y="65" width="30" height="6" rx="2" fill="${t.primary}"/>
      <rect x="30" y="78" width="55" height="4" rx="2" fill="#E2E8F0"/>
      <rect x="30" y="88" width="45" height="4" rx="2" fill="#E2E8F0"/>
      <rect x="30" y="98" width="50" height="4" rx="2" fill="#E2E8F0"/>
      <!-- 右侧增长图 -->
      <rect x="105" y="55" width="75" height="55" rx="6" fill="#F0F9FF" filter="url(#shadow)"/>
      <polyline points="120,95 135,75 150,82 165,60 180,70" fill="none" stroke="${t.accent}" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
      <polygon points="165,60 180,70 165,70 150,82 135,75 120,95 120,100 165,100 180,100 180,70" fill="${t.accent}" opacity="0.15"/>
      <circle cx="165" cy="60" r="4" fill="${t.accent}" filter="url(#softGlow)"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#CBD5E1"/>
    `,

    // ═══════════════════════════════════════════════════════════════
    // 动漫风格
    // ═══════════════════════════════════════════════════════════════

    sakura: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 樱花装饰 -->
      <ellipse cx="20" cy="20" rx="12" ry="10" fill="${t.accent}" opacity="0.4" transform="rotate(-15 20 20)"/>
      <ellipse cx="28" cy="28" rx="8" ry="6" fill="${t.primary}" opacity="0.3" transform="rotate(20 28 28)"/>
      <ellipse cx="180" cy="95" rx="15" ry="12" fill="${t.accent}" opacity="0.25" transform="rotate(30 180 95)"/>
      <ellipse cx="175" cy="100" rx="10" ry="8" fill="${t.primary}" opacity="0.2" transform="rotate(-10 175 100)"/>
      <!-- 樱花花瓣飘落 -->
      <ellipse cx="60" cy="35" rx="5" ry="4" fill="${t.accent}" opacity="0.5"/>
      <ellipse cx="140" cy="45" rx="4" ry="3" fill="${t.primary}" opacity="0.4"/>
      <ellipse cx="90" cy="85" rx="5" ry="4" fill="${t.accent}" opacity="0.35"/>
      <!-- 顶部粉色条带 -->
      <rect x="0" y="0" width="200" height="5" rx="0" fill="${t.primary}"/>
      <!-- 标题 -->
      <rect x="35" y="25" width="70" height="10" rx="3" fill="${t.primary}" filter="url(#softGlow)"/>
      <rect x="35" y="41" width="50" height="5" rx="2.5" fill="${t.accent}" opacity="0.7"/>
      <!-- 主卡片 -->
      <rect x="20" y="58" width="80" height="52" rx="8" fill="white" filter="url(#shadow)"/>
      <rect x="28" y="68" width="35" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="80" width="60" height="4" rx="2" fill="#FFF0F5"/>
      <rect x="28" y="90" width="50" height="4" rx="2" fill="#FFF0F5"/>
      <rect x="28" y="100" width="55" height="4" rx="2" fill="#FFF0F5"/>
      <!-- 右侧装饰 -->
      <rect x="110" y="58" width="70" height="52" rx="8" fill="white" filter="url(#shadow)"/>
      <circle cx="145" cy="84" r="20" fill="${t.accent}" opacity="0.15"/>
      <circle cx="145" cy="84" r="14" fill="${t.primary}" opacity="0.1"/>
      <circle cx="145" cy="84" r="8" fill="${t.accent}" opacity="0.2"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#FECDD3"/>
    `,

    neon_tokyo: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 背景网格 -->
      <pattern id="tokyoGrid" width="12" height="12" patternUnits="userSpaceOnUse">
        <path d="M 12 0 L 0 0 0 12" fill="none" stroke="rgba(255,45,120,0.08)" stroke-width="0.5"/>
      </pattern>
      <rect width="200" height="120" fill="url(#tokyoGrid)"/>
      <!-- 霓虹光晕 -->
      <circle cx="30" cy="90" r="50" fill="${t.primary}" opacity="0.04" filter="url(#glow)"/>
      <circle cx="170" cy="40" r="45" fill="${t.accent}" opacity="0.05" filter="url(#glow)"/>
      <!-- 霓虹顶部条 -->
      <rect x="0" y="0" width="200" height="4" fill="${t.primary}" filter="url(#softGlow)"/>
      <!-- 建筑轮廓装饰 -->
      <rect x="10" y="100" width="15" height="20" rx="1" fill="${t.accent}" opacity="0.15"/>
      <rect x="28" y="95" width="12" height="25" rx="1" fill="${t.primary}" opacity="0.1"/>
      <rect x="165" y="98" width="18" height="22" rx="1" fill="${t.accent}" opacity="0.12"/>
      <rect x="152" y="102" width="10" height="18" rx="1" fill="${t.primary}" opacity="0.08"/>
      <!-- 标题 -->
      <rect x="25" y="22" width="65" height="10" rx="3" fill="${t.primary}" filter="url(#glow)"/>
      <rect x="25" y="38" width="45" height="4" rx="2" fill="${t.accent}" opacity="0.8"/>
      <!-- 主卡片 -->
      <rect x="25" y="55" width="70" height="45" rx="5" fill="rgba(255,45,120,0.05)" stroke="${t.primary}" stroke-width="0.5" opacity="0.9"/>
      <rect x="33" y="63" width="35" height="4" rx="2" fill="${t.primary}" filter="url(#softGlow)"/>
      <rect x="33" y="74" width="50" height="3" rx="1.5" fill="rgba(0,255,255,0.3)"/>
      <rect x="33" y="82" width="40" height="3" rx="1.5" fill="rgba(0,255,255,0.2)"/>
      <rect x="33" y="90" width="45" height="3" rx="1.5" fill="rgba(255,45,120,0.25)"/>
      <!-- 右侧霓虹装饰 -->
      <rect x="105" y="55" width="75" height="45" rx="5" fill="rgba(0,255,255,0.03)" stroke="${t.accent}" stroke-width="0.5"/>
      <rect x="113" y="62" width="30" height="4" rx="2" fill="${t.accent}" filter="url(#glow)"/>
      <!-- 数据流 -->
      <path d="M113,82 L125,72 L140,78 L155,65 L170,72" fill="none" stroke="${t.primary}" stroke-width="1.5" stroke-dasharray="4 2" opacity="0.7"/>
      <path d="M113,92 L130,85 L145,90 L165,80 L175,85" fill="none" stroke="${t.accent}" stroke-width="1" stroke-dasharray="3 2" opacity="0.5"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="rgba(255,45,120,0.3)"/>
    `,

    fantasy: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 魔法光效 -->
      <circle cx="100" cy="60" r="55" fill="${t.primary}" opacity="0.05" filter="url(#glow)"/>
      <circle cx="160" cy="35" r="30" fill="${t.accent}" opacity="0.06" filter="url(#glow)"/>
      <!-- 星星装饰 -->
      <path d="M50,25 L52,30 L57,30 L53,33 L55,38 L50,35 L45,38 L47,33 L43,30 L48,30 Z" fill="${t.accent}" opacity="0.4"/>
      <path d="M170,55 L171,58 L174,58 L172,60 L173,63 L170,61 L167,63 L168,60 L166,58 L169,58 Z" fill="${t.primary}" opacity="0.35"/>
      <path d="M35,90 L36,93 L39,93 L37,95 L38,98 L35,96 L32,98 L33,95 L31,93 L34,93 Z" fill="${t.accent}" opacity="0.3"/>
      <!-- 顶部渐变条 -->
      <rect x="0" y="0" width="200" height="4" rx="0">
        <linearGradient id="magicGrad" x1="0%" y1="0%" x2="100%" y2="0%">
          <stop offset="0%" stop-color="${t.primary}"/>
          <stop offset="100%" stop-color="${t.accent}"/>
        </linearGradient>
      </rect>
      <rect x="0" y="0" width="200" height="4" rx="0" fill="url(#magicGrad)" filter="url(#softGlow)"/>
      <!-- 标题 -->
      <rect x="25" y="22" width="60" height="10" rx="3" fill="${t.primary}" filter="url(#softGlow)"/>
      <rect x="25" y="38" width="40" height="5" rx="2.5" fill="${t.accent}" opacity="0.7"/>
      <!-- 主卡片 -->
      <rect x="20" y="55" width="75" height="55" rx="8" fill="white" filter="url(#shadow)"/>
      <rect x="28" y="65" width="40" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="77" width="55" height="4" rx="2" fill="#ECFDF5"/>
      <rect x="28" y="87" width="45" height="4" rx="2" fill="#ECFDF5"/>
      <rect x="28" y="97" width="50" height="4" rx="2" fill="#ECFDF5"/>
      <!-- 魔法杖装饰 -->
      <rect x="105" y="60" width="75" height="50" rx="8" fill="white" filter="url(#shadow)"/>
      <circle cx="142" cy="85" r="18" fill="${t.accent}" opacity="0.1"/>
      <circle cx="142" cy="85" r="12" fill="${t.primary}" opacity="0.15"/>
      <circle cx="142" cy="85" r="6" fill="${t.accent}" opacity="0.25" filter="url(#softGlow)"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#D1FAE5"/>
    `,

    // ═══════════════════════════════════════════════════════════════
    // 创意风格
    // ═══════════════════════════════════════════════════════════════

    watercolor: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 水彩晕染效果 -->
      <ellipse cx="50" cy="100" rx="60" ry="40" fill="${t.primary}" opacity="0.08"/>
      <ellipse cx="150" cy="80" rx="50" ry="45" fill="${t.accent}" opacity="0.1"/>
      <ellipse cx="100" cy="60" rx="40" ry="35" fill="${t.primary}" opacity="0.06"/>
      <!-- 顶部条带 -->
      <rect x="0" y="0" width="200" height="5" rx="0" fill="${t.primary}" opacity="0.8"/>
      <!-- 标题 -->
      <rect x="25" y="22" width="55" height="10" rx="3" fill="${t.primary}"/>
      <rect x="25" y="38" width="35" height="5" rx="2.5" fill="${t.accent}" opacity="0.6"/>
      <!-- 主卡片 -->
      <rect x="20" y="55" width="80" height="55" rx="8" fill="white" filter="url(#shadow)" opacity="0.95"/>
      <rect x="28" y="65" width="40" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="77" width="60" height="4" rx="2" fill="#FEF3C7"/>
      <rect x="28" y="87" width="50" height="4" rx="2" fill="#FEF3C7"/>
      <rect x="28" y="97" width="55" height="4" rx="2" fill="#FEF3C7"/>
      <!-- 右侧水彩装饰 -->
      <rect x="108" y="55" width="72" height="55" rx="8" fill="white" filter="url(#shadow)" opacity="0.95"/>
      <ellipse cx="144" cy="82" rx="25" ry="20" fill="${t.accent}" opacity="0.15"/>
      <ellipse cx="140" cy="78" rx="18" ry="14" fill="${t.primary}" opacity="0.1"/>
      <rect x="125" y="92" width="38" height="4" rx="2" fill="${t.primary}" opacity="0.5"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#FDE68A"/>
    `,

    geometric: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 几何装饰 -->
      <polygon points="180,10 195,35 165,35" fill="${t.accent}" opacity="0.15"/>
      <rect x="10" y="90" width="25" height="25" rx="2" fill="${t.primary}" opacity="0.1" transform="rotate(15 22 102)"/>
      <circle cx="170" cy="100" r="15" fill="none" stroke="${t.accent}" stroke-width="2" opacity="0.2"/>
      <!-- 顶部条带 -->
      <rect x="0" y="0" width="200" height="5" rx="0" fill="${t.primary}"/>
      <!-- 标题 -->
      <rect x="25" y="25" width="50" height="10" rx="2" fill="${t.primary}"/>
      <rect x="25" y="41" width="35" height="5" rx="2.5" fill="${t.accent}"/>
      <!-- 主卡片 -->
      <rect x="20" y="58" width="70" height="52" rx="4" fill="white" filter="url(#shadow)"/>
      <rect x="28" y="68" width="35" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="80" width="50" height="4" rx="2" fill="#F4F4F5"/>
      <rect x="28" y="90" width="42" height="4" rx="2" fill="#F4F4F5"/>
      <rect x="28" y="100" width="48" height="4" rx="2" fill="#F4F4F5"/>
      <!-- 右侧几何装饰 -->
      <rect x="98" y="58" width="82" height="52" rx="4" fill="white" filter="url(#shadow)"/>
      <!-- 几何图形组合 -->
      <rect x="110" y="68" width="20" height="20" rx="2" fill="${t.accent}" opacity="0.2"/>
      <rect x="115" y="73" width="20" height="20" rx="2" fill="${t.primary}" opacity="0.3"/>
      <rect x="120" y="78" width="20" height="20" rx="2" fill="${t.accent}" opacity="0.4"/>
      <circle cx="160" cy="95" r="12" fill="none" stroke="${t.primary}" stroke-width="2" opacity="0.3"/>
      <polygon points="155,78 165,78 160,70" fill="${t.accent}" opacity="0.3"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#D4D4D8"/>
    `,

    retro: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 复古纹理 -->
      <pattern id="retroPat" width="8" height="8" patternUnits="userSpaceOnUse">
        <rect width="8" height="8" fill="${t.accent}" opacity="0.03"/>
      </pattern>
      <rect width="200" height="120" fill="url(#retroPat)"/>
      <!-- 复古圆形装饰 -->
      <circle cx="180" cy="25" r="30" fill="${t.accent}" opacity="0.1"/>
      <circle cx="25" cy="100" r="20" fill="${t.primary}" opacity="0.08"/>
      <!-- 顶部条带 -->
      <rect x="0" y="0" width="200" height="5" rx="0" fill="${t.primary}"/>
      <!-- 标题 -->
      <rect x="25" y="25" width="60" height="10" rx="2" fill="${t.primary}"/>
      <rect x="25" y="41" width="45" height="5" rx="2.5" fill="${t.accent}" opacity="0.7"/>
      <!-- 主卡片 -->
      <rect x="20" y="58" width="75" height="52" rx="6" fill="white" filter="url(#shadow)"/>
      <rect x="28" y="68" width="40" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="80" width="55" height="4" rx="2" fill="#FEF3C7"/>
      <rect x="28" y="90" width="45" height="4" rx="2" fill="#FEF3C7"/>
      <rect x="28" y="100" width="50" height="4" rx="2" fill="#FEF3C7"/>
      <!-- 右侧复古装饰 -->
      <rect x="103" y="58" width="77" height="52" rx="6" fill="white" filter="url(#shadow)"/>
      <circle cx="141" cy="84" r="18" fill="${t.accent}" opacity="0.15"/>
      <circle cx="141" cy="84" r="12" fill="none" stroke="${t.primary}" stroke-width="3" opacity="0.25"/>
      <circle cx="141" cy="84" r="6" fill="${t.accent}" opacity="0.3"/>
      <rect x="128" y="95" width="26" height="4" rx="2" fill="${t.primary}" opacity="0.4"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#FCD34D"/>
    `,

    // ═══════════════════════════════════════════════════════════════
    // 自然风格
    // ═══════════════════════════════════════════════════════════════

    mountain: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 水墨山峦 -->
      <path d="M0,110 Q30,70 60,90 Q90,60 120,80 Q150,50 180,75 Q195,65 200,70 L200,120 L0,120 Z" fill="${t.accent}" opacity="0.08"/>
      <path d="M0,115 Q40,90 80,105 Q120,80 160,95 Q190,82 200,88 L200,120 L0,120 Z" fill="${t.accent}" opacity="0.05"/>
      <!-- 顶部条带 -->
      <rect x="0" y="0" width="200" height="4" rx="0" fill="${t.primary}" opacity="0.6"/>
      <!-- 标题 -->
      <rect x="25" y="22" width="55" height="10" rx="3" fill="${t.primary}"/>
      <rect x="25" y="38" width="40" height="5" rx="2.5" fill="${t.accent}" opacity="0.5"/>
      <!-- 主卡片 -->
      <rect x="20" y="52" width="72" height="58" rx="6" fill="white" filter="url(#shadow)" opacity="0.95"/>
      <rect x="28" y="62" width="38" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="74" width="52" height="4" rx="2" fill="#F5F5F4"/>
      <rect x="28" y="84" width="44" height="4" rx="2" fill="#F5F5F4"/>
      <rect x="28" y="94" width="48" height="4" rx="2" fill="#F5F5F4"/>
      <!-- 右侧印章风格装饰 -->
      <rect x="100" y="52" width="80" height="58" rx="6" fill="white" filter="url(#shadow)" opacity="0.95"/>
      <rect x="110" y="62" width="30" height="4" rx="2" fill="${t.accent}" opacity="0.4"/>
      <rect x="110" y="72" width="50" height="3" rx="1.5" fill="${t.primary}" opacity="0.2"/>
      <rect x="110" y="80" width="40" height="3" rx="1.5" fill="${t.primary}" opacity="0.15"/>
      <rect x="110" y="88" width="45" height="3" rx="1.5" fill="${t.primary}" opacity="0.12"/>
      <rect x="110" y="96" width="35" height="3" rx="1.5" fill="${t.accent}" opacity="0.2"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#D6D3D1"/>
    `,

    ocean: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 波浪装饰 -->
      <path d="M0,100 Q25,92 50,100 T100,100 T150,100 T200,100 L200,120 L0,120 Z" fill="${t.accent}" opacity="0.08"/>
      <path d="M0,105 Q30,98 60,105 T120,105 T180,105 T200,105 L200,120 L0,120 Z" fill="${t.accent}" opacity="0.05"/>
      <path d="M0,110 Q35,104 70,110 T140,110 T200,110 L200,120 L0,120 Z" fill="${t.primary}" opacity="0.04"/>
      <!-- 顶部条带 -->
      <rect x="0" y="0" width="200" height="5" rx="0" fill="${t.primary}"/>
      <!-- 气泡装饰 -->
      <circle cx="175" cy="25" r="8" fill="${t.accent}" opacity="0.15"/>
      <circle cx="182" cy="35" r="5" fill="${t.primary}" opacity="0.1"/>
      <circle cx="30" cy="95" r="6" fill="${t.accent}" opacity="0.12"/>
      <!-- 标题 -->
      <rect x="25" y="22" width="60" height="10" rx="3" fill="${t.primary}"/>
      <rect x="25" y="38" width="42" height="5" rx="2.5" fill="${t.accent}" opacity="0.7"/>
      <!-- 主卡片 -->
      <rect x="20" y="52" width="75" height="58" rx="6" fill="white" filter="url(#shadow)" opacity="0.95"/>
      <rect x="28" y="62" width="35" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="74" width="55" height="4" rx="2" fill="#F0F9FF"/>
      <rect x="28" y="84" width="45" height="4" rx="2" fill="#F0F9FF"/>
      <rect x="28" y="94" width="50" height="4" rx="2" fill="#F0F9FF"/>
      <!-- 右侧海洋图表 -->
      <rect x="103" y="52" width="77" height="58" rx="6" fill="white" filter="url(#shadow)" opacity="0.95"/>
      <rect x="113" y="62" width="30" height="4" rx="2" fill="${t.accent}"/>
      <!-- 波浪线 -->
      <path d="M113,85 Q125,75 137,82 T163,75 T175,80" fill="none" stroke="${t.primary}" stroke-width="2" stroke-linecap="round" opacity="0.5"/>
      <path d="M113,95 Q128,88 143,92 T173,85 T183,90" fill="none" stroke="${t.accent}" stroke-width="1.5" stroke-linecap="round" opacity="0.4"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="#BAE6FD"/>
    `,

    aurora: `
      <rect width="200" height="120" rx="8" fill="${t.bg}"/>
      <!-- 极光效果 -->
      <ellipse cx="60" cy="40" rx="80" ry="60" fill="${t.accent}" opacity="0.04" filter="url(#glow)"/>
      <ellipse cx="150" cy="50" rx="70" ry="55" fill="${t.primary}" opacity="0.05" filter="url(#glow)"/>
      <path d="M0,80 Q50,50 100,70 Q150,40 200,65 L200,120 L0,120 Z" fill="${t.accent}" opacity="0.06"/>
      <path d="M0,90 Q60,70 120,85 Q180,65 200,80 L200,120 L0,120 Z" fill="${t.primary}" opacity="0.04"/>
      <!-- 星星 -->
      <circle cx="40" cy="25" r="1.5" fill="${t.primary}" opacity="0.6"/>
      <circle cx="80" cy="18" r="1" fill="${t.primary}" opacity="0.4"/>
      <circle cx="160" cy="30" r="1.5" fill="${t.accent}" opacity="0.5"/>
      <circle cx="185" cy="15" r="1" fill="${t.primary}" opacity="0.5"/>
      <circle cx="120" cy="22" r="1" fill="${t.accent}" opacity="0.4"/>
      <!-- 顶部条带 -->
      <rect x="0" y="0" width="200" height="4" rx="0" fill="${t.primary}" opacity="0.8"/>
      <!-- 标题 -->
      <rect x="25" y="22" width="60" height="10" rx="3" fill="${t.primary}" opacity="0.9"/>
      <rect x="25" y="38" width="45" height="5" rx="2.5" fill="${t.accent}" opacity="0.6"/>
      <!-- 主卡片 -->
      <rect x="20" y="52" width="72" height="58" rx="6" fill="rgba(255,255,255,0.05)" stroke="rgba(255,255,255,0.1)" stroke-width="1" filter="url(#shadow)"/>
      <rect x="28" y="62" width="38" height="5" rx="2" fill="${t.primary}"/>
      <rect x="28" y="74" width="52" height="4" rx="2" fill="rgba(255,255,255,0.15)"/>
      <rect x="28" y="84" width="42" height="4" rx="2" fill="rgba(255,255,255,0.1)"/>
      <rect x="28" y="94" width="48" height="4" rx="2" fill="rgba(255,255,255,0.08)"/>
      <!-- 右侧装饰 -->
      <rect x="100" y="52" width="80" height="58" rx="6" fill="rgba(255,255,255,0.03)" stroke="${t.accent}" stroke-width="0.5" opacity="0.8"/>
      <circle cx="140" cy="80" r="18" fill="none" stroke="${t.accent}" stroke-width="1.5" opacity="0.3"/>
      <circle cx="140" cy="80" r="12" fill="none" stroke="${t.primary}" stroke-width="1.5" opacity="0.25"/>
      <circle cx="140" cy="80" r="6" fill="${t.accent}" opacity="0.2"/>
      <!-- 底部页码 -->
      <rect x="85" y="112" width="30" height="3" rx="1.5" fill="rgba(255,255,255,0.15)"/>
    `
  }

  const design = designs[t.id] || designs.corp_blue
  return `<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120" viewBox="0 0 200 120">${defs}${design}</svg>`
}

function getThemeSvgDataUri(t: PptTheme): string {
  return 'data:image/svg+xml,' + encodeURIComponent(buildThemeSvg(t))
}

/** 选中主题 → 将缩略图转为 PNG 加入 pendingImages */
function selectPptTheme(theme: PptTheme) {
  selectedPptTheme.value = { id: theme.id, label: theme.label }
  pptPanelOpen.value = false

  // SVG → Canvas → PNG base64 → 加入图片附件
  const svg = buildThemeSvg(theme)
  const blob = new Blob([svg], { type: 'image/svg+xml' })
  const url = URL.createObjectURL(blob)
  const img = new Image()
  img.onload = () => {
    const canvas = document.createElement('canvas')
    canvas.width = 400; canvas.height = 226
    const ctx2d = canvas.getContext('2d')!
    ctx2d.drawImage(img, 0, 0, 400, 226)
    const pngDataUri = canvas.toDataURL('image/png')
    // 替换掉之前的主题图片（如果有）
    pendingImages.value = pendingImages.value.filter(i => !i.startsWith('data:image/png;base64,iVBOR'))
    pendingImages.value.unshift(pngDataUri)
    URL.revokeObjectURL(url)
    setTimeout(() => textareaRef.value?.focus(), 100)
  }
  img.src = url
}

function clearPptTheme() {
  selectedPptTheme.value = null
  // 移除主题图片
  pendingImages.value = pendingImages.value.filter(i => !i.startsWith('data:image/png;base64,iVBOR'))
}

function togglePptPanel() {
  pptPanelOpen.value = !pptPanelOpen.value
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
  if (selectedPptTheme.value) {
    text = `[PPT模式 | 主题: ${selectedPptTheme.value.id} (${selectedPptTheme.value.label})]\n${text}\n请使用 create_ppt 工具，参考附带的主题风格图片生成 PPT。`
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
  })
  input.value = ''
  pendingImages.value = []
  pendingFiles.value = []
  selectedPptTheme.value = null
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

      <!-- 已选主题标签 + 图片预览 + 附件文件 -->
      <div v-if="selectedPptTheme || pendingImages.length > 0 || pendingFiles.length > 0" class="attachments-bar">
        <!-- PPT 主题标签 -->
        <div v-if="selectedPptTheme" class="ppt-tag">
          <span class="ppt-tag-label">📊 {{ selectedPptTheme.label }}</span>
          <button class="ppt-tag-close" @click="clearPptTheme" title="取消PPT模式">
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

          <!-- ═══ PPT 按钮 ═══ -->
          <el-tooltip content="创建 PPT" placement="top" :show-after="400">
            <button
              class="tool-btn ppt-btn"
              :class="{ 'ppt-btn--active': pptPanelOpen || selectedPptTheme }"
              @click="togglePptPanel"
              :disabled="loading"
            >
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">
                <rect x="2" y="3" width="20" height="14" rx="2"/>
                <line x1="8" y1="21" x2="16" y2="21"/>
                <line x1="12" y1="17" x2="12" y2="21"/>
              </svg>
            </button>
          </el-tooltip>

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

    <!-- ═══ PPT 主题画廊（在输入框下方展开） ═══ -->
    <Transition name="ppt-panel">
      <div v-if="pptPanelOpen" class="ppt-gallery">
        <div class="ppt-gallery-header">
          <span class="ppt-gallery-title">选择 PPT 主题风格</span>
          <button class="ppt-gallery-close" @click="pptPanelOpen = false">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          </button>
        </div>
        <div class="ppt-gallery-grid">
          <button
            v-for="t in PPT_THEMES" :key="t.id"
            class="ppt-gallery-card"
            :class="{ 'ppt-gallery-card--selected': selectedPptTheme?.id === t.id }"
            @click="selectPptTheme(t)"
          >
            <img class="ppt-gallery-img" :src="getThemeSvgDataUri(t)" :alt="t.label" />
            <div class="ppt-gallery-info">
              <span class="ppt-gallery-name">{{ t.label }}</span>
              <span class="ppt-gallery-desc">{{ t.desc }}</span>
            </div>
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
  display: flex; flex-direction: column;
  padding: 0; border: 2.5px solid transparent; border-radius: 12px;
  background: #fff; cursor: pointer;
  transition: all 0.22s cubic-bezier(0.34,1.56,0.64,1);
  overflow: hidden;
  position: relative;
}
/* 卡片悬浮内光 */
.ppt-gallery-card::before {
  content: '';
  position: absolute;
  inset: 0;
  border-radius: inherit;
  opacity: 0;
  transition: opacity 0.3s;
  background: linear-gradient(135deg, rgba(0,174,236,0.03), rgba(251,114,153,0.03));
  pointer-events: none;
}
.ppt-gallery-card:hover {
  border-color: #00AEEC;
  box-shadow: 0 6px 20px rgba(0,174,236,0.18), 0 0 0 1px rgba(0,174,236,0.08);
  transform: translateY(-4px);
}
.ppt-gallery-card:hover::before { opacity: 1; }
.ppt-gallery-card:active {
  transform: translateY(-2px) scale(0.98);
}
.ppt-gallery-card--selected {
  border-color: #FF9800 !important;
  box-shadow: 0 6px 20px rgba(255,152,0,0.22), 0 0 0 1px rgba(255,152,0,0.1) !important;
}
.ppt-gallery-card--selected::after {
  content: '';
  position: absolute;
  top: 6px; right: 6px;
  width: 18px; height: 18px;
  background: linear-gradient(135deg, #FF9800, #FFB74D);
  border-radius: 50%;
  display: flex; align-items: center; justify-content: center;
  box-shadow: 0 2px 6px rgba(255,152,0,0.4);
  z-index: 2;
}

.ppt-gallery-img {
  width: 100%;
  aspect-ratio: 16/9;
  object-fit: cover;
  border-radius: 10px 10px 0 0;
  display: block;
  transition: transform 0.3s;
}
.ppt-gallery-card:hover .ppt-gallery-img {
  transform: scale(1.03);
}

.ppt-gallery-info {
  padding: 8px 10px 10px;
  display: flex; flex-direction: column; gap: 2px;
}
.ppt-gallery-name {
  font-size: 12px; font-weight: 700; color: #18191C;
  transition: color 0.15s;
}
.ppt-gallery-card:hover .ppt-gallery-name { color: #00AEEC; }
.ppt-gallery-card--selected .ppt-gallery-name { color: #FF9800; }
.ppt-gallery-desc { font-size: 10.5px; color: #9499A0; }

/* 画廊动画 */
.ppt-panel-enter-active { transition: opacity 0.2s, max-height 0.25s ease-out; }
.ppt-panel-leave-active { transition: opacity 0.15s, max-height 0.2s ease-in; }
.ppt-panel-enter-from { opacity: 0; max-height: 0; }
.ppt-panel-leave-to { opacity: 0; max-height: 0; }
.ppt-panel-enter-to, .ppt-panel-leave-from { max-height: 400px; }
</style>
