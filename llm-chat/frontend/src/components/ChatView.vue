<script setup lang="ts">
import { nextTick, watch, ref, computed, onMounted, onUnmounted } from 'vue'
import type { Message, SendPayload, AgentStatus } from '../types'
import MessageItem from './MessageItem.vue'
import InputBox from './InputBox.vue'
import { ChatDotRound, Lightning, EditPen, DataAnalysis, Grid, TrendCharts } from '@element-plus/icons-vue'

const props = defineProps<{
  messages: Message[]
  loading: boolean
  agentStatus: AgentStatus
}>()

const emit = defineEmits<{ send: [payload: SendPayload] }>()

const messagesContainer = ref<HTMLDivElement>()

// 用户是否手动向上滚动了（若是则不强制跳底）
let userScrolledUp = false

function onMessagesScroll() {
  if (!messagesContainer.value) return
  const el = messagesContainer.value
  const distFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight
  userScrolledUp = distFromBottom > 120
}

function scrollToBottom(force = false) {
  if (!messagesContainer.value) return
  if (force || !userScrolledUp) {
    messagesContainer.value.scrollTop = messagesContainer.value.scrollHeight
  }
}

// 流式进度模拟（0-95 时缓慢增加，完成后跳到100）
const progress = ref(0)
let progressTimer: ReturnType<typeof setInterval> | null = null

watch(() => props.loading, (val) => {
  if (val) {
    userScrolledUp = false   // 新请求开始时重置，确保跳到底部
    progress.value = 5
    progressTimer = setInterval(() => {
      if (progress.value < 90) {
        progress.value += Math.random() * 3
      }
    }, 400)
  } else {
    if (progressTimer) clearInterval(progressTimer)
    progress.value = 100
    setTimeout(() => { progress.value = 0 }, 600)
  }
})

watch(
  () => props.messages.length > 0 ? props.messages[props.messages.length - 1].content : '',
  async () => {
    await nextTick()
    scrollToBottom()
  },
)

// 新消息列表时强制跳底（切换对话）
watch(
  () => props.messages.length,
  async (newLen, oldLen) => {
    if (newLen < oldLen) {
      // 对话切换，重置并跳底
      userScrolledUp = false
      await nextTick()
      scrollToBottom(true)
    }
  }
)

const suggestions = [
  { icon: EditPen, label: '撰写文章', prompt: '帮我写一篇关于AI发展趋势的文章' },
  { icon: Lightning, label: '代码生成', prompt: '用 Python 实现一个 REST API 服务器' },
  { icon: DataAnalysis, label: '数据分析', prompt: '分析一份销售数据并给出可视化建议' },
  { icon: TrendCharts, label: '方案策划', prompt: '帮我制定一个产品上线推广方案' },
  { icon: Grid, label: '更多功能', prompt: '你都能做什么？列出你的所有能力' },
]

function sendSuggestion(prompt: string) {
  emit('send', { text: prompt, images: [] })
}

const showProgress = computed(() => progress.value > 0 && progress.value < 100)
</script>

<template>
  <div class="chat-view">

    <!-- 顶部进度条（AI 生成时） -->
    <div class="top-progress" :class="{ visible: props.loading || showProgress }">
      <el-progress
        :percentage="Math.min(progress, 100)"
        :show-text="false"
        :stroke-width="2"
        status=""
        class="gen-progress"
      />
    </div>

    <!-- 顶部 header -->
    <div class="chat-header">
      <div class="header-left">
        <el-icon class="header-icon"><ChatDotRound /></el-icon>
        <span class="header-title">对话</span>
      </div>
      <div class="header-right">
        <!-- 空闲：未使用过显示"就绪"，用过后显示上次模型 -->
        <div v-if="agentStatus.state === 'idle'" class="status-chip idle">
          <span class="chip-dot"></span>
          <span>{{ agentStatus.model ? agentStatus.model : '就绪' }}</span>
        </div>
        <!-- 完成 -->
        <div v-else-if="agentStatus.state === 'done'" class="status-chip done">
          <svg width="12" height="12" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="#22c55e" stroke-width="1.5"/>
            <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round"/>
          </svg>
          <span>{{ agentStatus.model }} · 回答完成</span>
        </div>
        <!-- 路由中 -->
        <div v-else-if="agentStatus.state === 'routing'" class="status-chip routing">
          <svg class="chip-spin" width="12" height="12" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="2" stroke-dasharray="20 18"/>
          </svg>
          <span>分析问题中...</span>
        </div>
        <!-- 思考中 -->
        <div v-else-if="agentStatus.state === 'thinking'" class="status-chip thinking">
          <svg class="chip-spin" width="12" height="12" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="#6366f1" stroke-width="2" stroke-dasharray="20 18"/>
          </svg>
          <span>{{ agentStatus.model || '模型' }} · 生成中</span>
        </div>
        <!-- 工具调用 -->
        <div v-else-if="agentStatus.state === 'tool'" class="status-chip tool">
          <svg class="chip-spin" width="12" height="12" viewBox="0 0 16 16" fill="none">
            <circle cx="8" cy="8" r="6" stroke="#f59e0b" stroke-width="2" stroke-dasharray="20 18"/>
          </svg>
          <span>调用 {{ agentStatus.tool || '工具' }}...</span>
        </div>
      </div>
    </div>

    <!-- ── 空状态 ── -->
    <div v-if="messages.length === 0" class="empty-view">
      <div class="hero">
        <div class="hero-icon-wrap">
          <svg width="28" height="28" viewBox="0 0 64 64" fill="none">
            <path d="M36 8L22 34H31L28 56L46 28H36Z" fill="#6366f1"/>
            <circle cx="46" cy="18" r="4" fill="#a5b4fc" opacity="0.7"/>
          </svg>
        </div>
        <h1 class="hero-title">我能为你做什么？</h1>
        <p class="hero-sub">基于本地 AI 模型 · 数据不出本地 · 安全可靠</p>
      </div>

      <InputBox :loading="loading" :centered="true" @send="emit('send', $event)" />

      <div class="suggestions">
        <button
          v-for="s in suggestions"
          :key="s.label"
          class="sug-card"
          @click="sendSuggestion(s.prompt)"
        >
          <el-icon class="sug-icon"><component :is="s.icon" /></el-icon>
          <span class="sug-label">{{ s.label }}</span>
        </button>
      </div>

      <!-- 底部信息 -->
      <div class="empty-info">
        <el-icon><Lightning /></el-icon>
        <span>支持多轮对话 · 图片识别 · 代码高亮 · 长期记忆</span>
      </div>
    </div>

    <!-- ── 对话视图 ── -->
    <div v-else class="chat-body">
      <div class="messages-scroll" ref="messagesContainer" @scroll="onMessagesScroll">
        <div class="messages-inner">
          <MessageItem
            v-for="(msg, i) in messages"
            :key="i"
            :message="msg"
          />
          <!-- 打字指示器 -->
          <div
            v-if="loading && messages.length > 0 && messages[messages.length-1].role === 'assistant' && !messages[messages.length-1].content"
            class="typing"
          >
            <span></span><span></span><span></span>
          </div>
        </div>
      </div>

      <!-- 底部输入框 -->
      <div class="bottom-input">
        <InputBox :loading="loading" @send="emit('send', $event)" />
      </div>
    </div>

  </div>
</template>

<style scoped>
.chat-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  min-width: 0;
  background: var(--cf-bg);
  position: relative;
}

/* 顶部进度条 */
.top-progress {
  position: absolute;
  top: 0; left: 0; right: 0;
  z-index: 100;
  opacity: 0;
  transition: opacity 0.2s;
  pointer-events: none;
}
.top-progress.visible { opacity: 1; }
:deep(.gen-progress .el-progress-bar__outer) {
  background: transparent !important;
  border-radius: 0 !important;
}
:deep(.gen-progress .el-progress-bar__inner) {
  background: linear-gradient(90deg, #6366f1, #a5b4fc, #6366f1) !important;
  background-size: 200% !important;
  border-radius: 0 !important;
  animation: shimmer 1.5s linear infinite !important;
}
@keyframes shimmer {
  0% { background-position: 200% center; }
  100% { background-position: -200% center; }
}

/* Header */
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  background: rgba(243,244,248,0.8);
  backdrop-filter: blur(8px);
  border-bottom: 1px solid var(--cf-border-soft);
  flex-shrink: 0;
  z-index: 10;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--cf-text-2);
}
.header-icon { font-size: 16px; color: var(--cf-indigo); }
.header-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--cf-text-1);
}
.header-right {
  display: flex;
  align-items: center;
  gap: 6px;
}
.status-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 20px;
  font-size: 12px;
  font-weight: 500;
  border: 1px solid var(--cf-border);
  background: var(--cf-card);
  color: var(--cf-text-2);
  transition: all 0.3s;
  font-family: inherit;
}
.status-chip.idle   { border-color: #bbf7d0; color: #16a34a; background: #f0fdf4; }
.status-chip.done   { border-color: #bbf7d0; color: #16a34a; background: #f0fdf4; }
.status-chip.routing  { border-color: #e0e7ff; color: #6366f1; background: #eef2ff; }
.status-chip.thinking { border-color: #e0e7ff; color: #6366f1; background: #eef2ff; }
.status-chip.tool   { border-color: #fde68a; color: #b45309; background: #fffbeb; }
.chip-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: currentColor;
  flex-shrink: 0;
}
.chip-sub {
  color: currentColor;
  opacity: 0.6;
  font-size: 11px;
}
.chip-spin {
  flex-shrink: 0;
  animation: spin-ccw 1s linear infinite;
  transform-origin: center;
}
@keyframes spin-ccw {
  to { transform: rotate(360deg); }
}

/* 空状态 */
.empty-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 32px 28px 48px;
  gap: 28px;
}
.hero {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
}
.hero-icon-wrap {
  width: 64px; height: 64px;
  border-radius: 18px;
  background: linear-gradient(135deg, #eef2ff 0%, #e0e7ff 100%);
  border: 1px solid #c7d2fe;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 4px;
  box-shadow: 0 4px 16px rgba(99,102,241,0.15);
}
.hero-title {
  font-size: 28px;
  font-weight: 700;
  color: var(--cf-text-1);
  letter-spacing: -0.5px;
  line-height: 1.2;
}
.hero-sub {
  font-size: 13.5px;
  color: var(--cf-text-4);
  font-weight: 400;
}

/* 快捷操作 */
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  max-width: 680px;
}
.sug-card {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 16px;
  background: var(--cf-card);
  border: 1.5px solid var(--cf-border);
  border-radius: 22px;
  font-size: 13px;
  font-weight: 500;
  color: var(--cf-text-2);
  font-family: inherit;
  cursor: pointer;
  transition: all 0.18s;
  box-shadow: var(--cf-shadow-xs);
}
.sug-card:hover {
  background: var(--cf-active);
  border-color: #a5b4fc;
  color: var(--cf-indigo);
  transform: translateY(-2px);
  box-shadow: var(--cf-shadow-sm);
}
.sug-icon {
  font-size: 14px;
}
.sug-label {
  font-weight: 500;
}
.empty-info {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--cf-text-5);
}

/* 对话区 */
.chat-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.messages-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 0 16px;
}
.messages-inner {
  max-width: 740px;
  margin: 0 auto;
  padding: 0 24px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.bottom-input {
  padding: 0 20px 16px;
  max-width: 780px;
  margin: 0 auto;
  width: 100%;
}

/* 打字指示器 */
.typing {
  display: flex;
  gap: 5px;
  padding: 10px 0 0 44px;
}
.typing span {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: var(--cf-indigo);
  opacity: 0.4;
  animation: bounce 1.3s ease-in-out infinite;
}
.typing span:nth-child(2) { animation-delay: 0.18s; }
.typing span:nth-child(3) { animation-delay: 0.36s; }
@keyframes bounce {
  0%, 80%, 100% { transform: translateY(0); opacity: 0.3; }
  40% { transform: translateY(-7px); opacity: 1; }
}
</style>
