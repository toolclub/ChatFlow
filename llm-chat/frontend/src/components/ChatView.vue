<script setup lang="ts">
import { nextTick, watch, ref, computed, onMounted, onUnmounted } from 'vue'
import type { Message, SendPayload, AgentStatus, CognitiveState } from '../types'
import MessageItem from './MessageItem.vue'
import InputBox from './InputBox.vue'
import { Lightning, EditPen, DataAnalysis, Grid, TrendCharts, Check, Loading } from '@element-plus/icons-vue'
import AgentStatusBubble from './AgentStatusBubble.vue'
import ClarificationCard from './ClarificationCard.vue'

const props = defineProps<{
  messages: Message[]
  loading: boolean
  agentStatus: AgentStatus
  cognitive: CognitiveState
  hasCognitiveContent?: boolean
  panelOpen?: boolean
}>()

const emit = defineEmits<{
  send: [payload: SendPayload]
  stop: []
  togglePanel: []                 // 切换认知面板展开/折叠
  clarificationSubmit: [answers: Record<string, string | string[]>]
}>()

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
        <!-- Sparkle AI 图标（与 Logo / Avatar 统一） -->
        <svg class="header-logo-icon" width="16" height="16" viewBox="0 0 24 24" fill="none">
          <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z" fill="#374151"/>
          <path d="M19.5 4C19.5 4 20.1 6.6 22 7.5C20.1 8.4 19.5 11 19.5 11C19.5 11 18.9 8.4 17 7.5C18.9 6.6 19.5 4 19.5 4Z" fill="#374151" opacity="0.4"/>
        </svg>
        <span class="header-title">AI 对话</span>
      </div>
      <div class="header-right">
        <!-- 认知面板切换按钮（有历史内容时显示） -->
        <el-tooltip v-if="hasCognitiveContent" :content="panelOpen ? '折叠认知面板' : '展开认知面板'" placement="bottom">
          <el-button
            text size="small"
            :type="panelOpen ? 'primary' : 'default'"
            style="padding:4px 7px;font-size:13px;"
            @click="emit('togglePanel')"
          >
            <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor" style="opacity:0.8">
              <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z"/>
            </svg>
          </el-button>
        </el-tooltip>

        <!-- 统一状态标签（单元素 transition，避免闪烁） -->
        <transition name="tag-swap" mode="out-in">
          <el-tag
            v-if="agentStatus.state === 'idle'"
            key="idle"
            type="success" effect="plain" round :closable="false" class="s-tag"
          ><span class="s-dot s-dot--green"></span>就绪</el-tag>

          <el-tag
            v-else-if="agentStatus.state === 'done'"
            key="done"
            type="success" effect="plain" round :closable="false" class="s-tag"
          ><el-icon style="margin-right:3px"><Check /></el-icon>完成</el-tag>

          <el-tag
            v-else-if="agentStatus.state === 'saving'"
            key="saving"
            effect="plain" round :closable="false" class="s-tag s-tag--saving"
          ><el-icon class="s-spin" style="margin-right:4px"><Loading /></el-icon>保存中</el-tag>

          <el-tag
            v-else-if="agentStatus.state === 'reflecting'"
            key="reflecting"
            effect="plain" round :closable="false" class="s-tag s-tag--reflect"
          ><el-icon class="s-spin" style="margin-right:4px"><Loading /></el-icon>反思中</el-tag>

          <el-tag
            v-else-if="agentStatus.state === 'tool'"
            key="tool"
            type="warning" effect="plain" round :closable="false" class="s-tag"
          ><el-icon class="s-spin" style="margin-right:4px"><Loading /></el-icon>{{ agentStatus.tool || '工具' }}</el-tag>

          <el-tag
            v-else
            :key="agentStatus.state"
            type="info" effect="plain" round :closable="false" class="s-tag"
            :style="agentStatus.state === 'planning' ? 'color:#8b5cf6;border-color:#c4b5fd' : ''"
          >
            <el-icon class="s-spin" style="margin-right:4px"><Loading /></el-icon>
            {{ agentStatus.state === 'routing' ? '分析中'
             : agentStatus.state === 'planning' ? '规划中'
             : '推理中' }}
          </el-tag>
        </transition>

        <!-- 停止按钮 -->
        <el-button
          v-if="loading"
          size="small"
          type="danger"
          plain
          round
          class="stop-btn"
          @click="emit('stop')"
        >
          <svg width="8" height="8" viewBox="0 0 10 10" fill="currentColor" style="margin-right:5px;flex-shrink:0">
            <rect x="1" y="1" width="8" height="8" rx="1.5"/>
          </svg>
          停止
        </el-button>
      </div>
    </div>

    <!-- ── 空状态 ── -->
    <div v-if="messages.length === 0" class="empty-view">
      <div class="hero">
        <div class="hero-icon-wrap">
          <!-- 大号 Sparkle — 干净科技感 -->
          <svg width="34" height="34" viewBox="0 0 48 48" fill="none">
            <path d="M24 4C24 4 26.5 17 36 22C26.5 27 24 40 24 40C24 40 21.5 27 12 22C21.5 17 24 4 24 4Z" fill="#111827"/>
            <path d="M39 6C39 6 40.2 12 44 14C40.2 16 39 22 39 22C39 22 37.8 16 34 14C37.8 12 39 6 39 6Z" fill="#111827" opacity="0.3"/>
            <path d="M10 34C10 34 10.8 37.5 13 39C10.8 40.5 10 44 10 44C10 44 9.2 40.5 7 39C9.2 37.5 10 34 10 34Z" fill="#111827" opacity="0.25"/>
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
          <!-- 澄清卡片：附加在最后一条 assistant 消息下方 -->
          <template
            v-if="messages.length > 0 && messages[messages.length-1].role === 'assistant'
                  && messages[messages.length-1].clarification"
          >
            <div class="clarification-wrap">
              <ClarificationCard
                :data="messages[messages.length-1].clarification!"
                :loading="loading"
                @submit="emit('clarificationSubmit', $event)"
              />
            </div>
          </template>
          <!-- 阶段状态气泡（替换旧的三点打字指示器） -->
          <div
            v-if="loading && messages.length > 0 && messages[messages.length-1].role === 'assistant' && !messages[messages.length-1].content"
            class="status-bubble-wrap"
          >
            <div class="assistant-avatar-dot"></div>
            <AgentStatusBubble :status="agentStatus" :cognitive="cognitive" />
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

/* ── Header ── */
.chat-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 11px 20px;
  background: rgba(250,250,252,0.85);
  backdrop-filter: blur(12px);
  -webkit-backdrop-filter: blur(12px);
  border-bottom: 1px solid var(--cf-border-soft);
  flex-shrink: 0;
  z-index: 10;
}
.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--cf-text-2);
}
.header-logo-icon { flex-shrink: 0; opacity: 0.75; }
.header-title {
  font-size: 14px;
  font-weight: 600;
  color: var(--cf-text-1);
  letter-spacing: -0.2px;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 7px;
}

/* ── 状态标签 ── */
:deep(.s-tag) {
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  padding: 0 11px;
  height: 27px;
  border-radius: 99px !important;
  box-shadow: 0 1px 4px rgba(0,0,0,0.07);
  display: inline-flex;
  align-items: center;
  gap: 4px;
  transition: all 0.2s;
  letter-spacing: 0.1px;
}
.s-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  flex-shrink: 0;
  margin-right: 2px;
}
.s-dot--green {
  background: #22c55e;
  animation: pulse-dot 2s ease-in-out infinite;
}
@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(34,197,94,0.5); }
  50%       { box-shadow: 0 0 0 4px rgba(34,197,94,0); }
}
.s-spin {
  font-size: 12px !important;
  animation: s-rotate 0.9s linear infinite;
  transform-origin: center;
}
@keyframes s-rotate { to { transform: rotate(360deg); } }

/* ── 停止按钮 ── */
:deep(.stop-btn) {
  font-family: inherit;
  height: 27px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  border-radius: 99px !important;
  box-shadow: 0 1px 5px rgba(220,38,38,0.18);
  transition: all 0.15s;
}
:deep(.stop-btn:hover) {
  box-shadow: 0 3px 10px rgba(220,38,38,0.28);
  transform: translateY(-1px);
}

/* ── 空状态 ── */
.empty-view {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 32px 28px 52px;
  gap: 28px;
}
.hero {
  text-align: center;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 12px;
}
.hero-icon-wrap {
  width: 72px; height: 72px;
  border-radius: 22px;
  background: linear-gradient(145deg, #ffffff 0%, #f5f3ff 100%);
  border: 1.5px solid #e0deff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 2px;
  box-shadow: 0 4px 20px rgba(99,102,241,0.12), 0 1px 4px rgba(0,0,0,0.06);
}
.hero-title {
  font-size: 30px;
  font-weight: 800;
  color: var(--cf-text-1);
  letter-spacing: -0.8px;
  line-height: 1.2;
  background: linear-gradient(135deg, #0f172a 0%, #334155 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.hero-sub {
  font-size: 13.5px;
  color: var(--cf-text-4);
  font-weight: 400;
  letter-spacing: 0.1px;
}

/* ── 快捷操作 ── */
.suggestions {
  display: flex;
  flex-wrap: wrap;
  gap: 9px;
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
  border-radius: 24px;
  font-size: 13px;
  font-weight: 500;
  color: var(--cf-text-2);
  font-family: inherit;
  cursor: pointer;
  transition: all 0.18s cubic-bezier(0.34, 1.56, 0.64, 1);
  box-shadow: var(--cf-shadow-xs);
}
.sug-card:hover {
  background: var(--cf-active);
  border-color: #c4b5fd;
  color: var(--cf-indigo);
  transform: translateY(-3px) scale(1.02);
  box-shadow: 0 6px 20px rgba(99,102,241,0.18);
}
.sug-icon { font-size: 14px; }
.sug-label { font-weight: 500; }
.empty-info {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--cf-text-5);
}

/* ── 对话区 ── */
.chat-body {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.messages-scroll {
  flex: 1;
  overflow-y: auto;
  padding: 20px 0 12px;
}
.messages-inner {
  max-width: 1100px;
  margin: 0 auto;
  padding: 0 32px;
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.bottom-input {
  padding: 0 32px 16px;
  max-width: 1100px;
  margin: 0 auto;
  width: 100%;
}

/* ── 澄清卡片容器 ── */
.clarification-wrap {
  padding: 4px 0 8px 40px;  /* 与 assistant 消息对齐（头像宽度） */
}

/* ── 状态气泡容器 ── */
.status-bubble-wrap {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 4px 0;
}
.assistant-avatar-dot {
  width: 30px; height: 30px;
  border-radius: 50%;
  background: linear-gradient(135deg, #6366f1, #a5b4fc);
  flex-shrink: 0;
  margin-top: 2px;
  box-shadow: 0 2px 8px rgba(99,102,241,0.28);
}

/* ── 标签切换过渡 ── */
.tag-swap-enter-active,
.tag-swap-leave-active { transition: opacity .15s, transform .15s; }
.tag-swap-enter-from   { opacity: 0; transform: translateY(-4px) scale(.94); }
.tag-swap-leave-to     { opacity: 0; transform: translateY(4px)  scale(.94); }

:deep(.s-tag--saving)  { color: #64748b !important; border-color: #cbd5e1 !important; }
:deep(.s-tag--reflect) { color: #059669 !important; border-color: #6ee7b7 !important; }
</style>
