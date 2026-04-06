<script setup lang="ts">
import { nextTick, watch, ref, computed, onMounted, onUnmounted } from 'vue'
import type { Message, SendPayload, AgentStatus, CognitiveState } from '../types'
import MessageItem from './MessageItem.vue'
import InputBox from './InputBox.vue'
import { Lightning, EditPen, DataAnalysis, Grid, TrendCharts, Check, Loading } from '@element-plus/icons-vue'
import ClarificationCard from './ClarificationCard.vue'

const props = defineProps<{
  messages: Message[]
  loading: boolean
  agentStatus: AgentStatus
  cognitive: CognitiveState
  hasCognitiveContent?: boolean
  panelOpen?: boolean
  convTitle?: string        // 当前对话标题（第一条用户消息 / 后端生成摘要）
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
        <!-- 品牌图标：白底 + 双星，与 favicon 一致 -->
        <div class="header-brand-icon">
          <svg width="16" height="16" viewBox="0 0 32 32" fill="none">
            <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#111827"/>
            <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#111827" opacity="0.5"/>
          </svg>
        </div>

        <!-- 标题区 -->
        <div class="header-title-block">
          <span class="header-app-name">ChatFlow</span>
          <template v-if="convTitle">
            <span class="header-title-sep">/</span>
            <span class="header-conv-title">{{ convTitle }}</span>
          </template>
          <template v-else>
            <span class="header-title-sep">/</span>
            <span class="header-conv-title header-conv-title--placeholder">新对话</span>
          </template>
        </div>
      </div>
      <div class="header-right">
        <!-- 认知面板切换按钮（有历史内容时显示） -->
        <button
          v-if="hasCognitiveContent"
          class="ghost-btn"
          :class="{ 'ghost-btn--active': panelOpen }"
          :title="panelOpen ? '折叠认知面板' : '展开认知面板'"
          @click="emit('togglePanel')"
        >
          <svg width="11" height="11" viewBox="0 0 24 24" fill="currentColor">
            <path d="M12 3C12 3 13.2 8.8 18 11C13.2 13.2 12 19 12 19C12 19 10.8 13.2 6 11C10.8 8.8 12 3 12 3Z"/>
          </svg>
          <span>{{ panelOpen ? '收起' : '计划' }}</span>
        </button>

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
            :style="agentStatus.state === 'planning'       ? 'color:#8b5cf6;border-color:#c4b5fd'
                  : agentStatus.state === 'vision_analyze' ? 'color:#0ea5e9;border-color:#7dd3fc'
                  : ''"
          >
            <el-icon class="s-spin" style="margin-right:4px"><Loading /></el-icon>
            {{ agentStatus.state === 'routing'        ? '分析中'
             : agentStatus.state === 'planning'       ? '规划中'
             : agentStatus.state === 'vision_analyze' ? '图像解析中'
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
          <!-- 与 favicon 一致：主星 + 副星（白底黑星） -->
          <svg width="38" height="38" viewBox="0 0 32 32" fill="none">
            <path d="M16 3C16 3 17.6 11 23.5 14C17.6 17 16 25 16 25C16 25 14.4 17 8.5 14C14.4 11 16 3 16 3Z" fill="#111827"/>
            <path d="M25.5 6C25.5 6 26.2 9.2 28.3 10.2C26.2 11.2 25.5 14.4 25.5 14.4C25.5 14.4 24.8 11.2 22.7 10.2C24.8 9.2 25.5 6 25.5 6Z" fill="#111827" opacity="0.5"/>
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
            :is-last-loading="loading && i === messages.length - 1 && msg.role === 'assistant'"
            :agent-status="(loading && i === messages.length - 1 && msg.role === 'assistant') ? agentStatus : undefined"
            :cognitive="(loading && i === messages.length - 1 && msg.role === 'assistant') ? cognitive : undefined"
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
          <!-- 状态气泡已移入 MessageItem 内联渲染，与头像对齐 -->
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
  background: #ffffff;
  position: relative;
  border-radius: var(--cf-radius-lg);
  overflow: hidden;
  border: 1px solid var(--cf-border-soft);
  box-shadow: var(--cf-shadow-sm);
  height: 100%;
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
  background: linear-gradient(90deg, #6B9EFF, #6366f1, #6B9EFF) !important;
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
  padding: 0 24px;
  height: 56px;
  background: #ffffff;
  border-bottom: 1px solid var(--cf-border);
  flex-shrink: 0;
  z-index: 10;
  box-shadow: 0 1px 6px rgba(0,0,0,0.04);
}
.header-left {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  flex: 1;
  overflow: hidden;
}
.header-brand-icon {
  width: 30px; height: 30px;
  border-radius: 8px;
  background: #ffffff;
  border: 1.5px solid #e4e4e7;
  box-shadow: 0 1px 4px rgba(0,0,0,0.08);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.header-title-block {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  overflow: hidden;
}
.header-app-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--cf-text-1);
  letter-spacing: -0.3px;
  flex-shrink: 0;
}
.header-title-sep {
  font-size: 13px;
  color: var(--cf-text-4);
  font-weight: 300;
  flex-shrink: 0;
  line-height: 1;
}
.header-conv-title {
  font-size: 13.5px;
  font-weight: 500;
  color: var(--cf-text-2);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
  letter-spacing: -0.1px;
}
.header-conv-title--placeholder {
  color: var(--cf-text-4);
  font-weight: 400;
}
.header-right {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-shrink: 0;
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

/* ── Ghost 按钮 ── */
.ghost-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 12px;
  background: transparent;
  border: 1px solid var(--cf-border);
  border-radius: var(--cf-radius-sm);
  color: var(--cf-indigo);
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.2s ease-out;
  letter-spacing: 0.1px;
}
.ghost-btn:hover {
  background: rgba(107,158,255,0.1);
  border-color: var(--cf-indigo);
  transform: translateY(-1px);
  box-shadow: var(--cf-shadow-xs);
}
.ghost-btn:active { transform: translateY(0); }
.ghost-btn--active {
  background: rgba(107,158,255,0.1);
  border-color: var(--cf-indigo);
  color: var(--cf-indigo);
}
.ghost-btn--active:hover { background: rgba(107,158,255,0.15); }

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
  border-radius: 20px;
  background: #ffffff;
  border: 1.5px solid #e4e4e7;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 2px;
  box-shadow: 0 4px 20px rgba(0,0,0,0.08), 0 1px 4px rgba(0,0,0,0.05);
}
.hero-title {
  font-size: 30px;
  font-weight: 800;
  letter-spacing: -0.8px;
  line-height: 1.2;
  background: linear-gradient(135deg, #111827 0%, #6B9EFF 100%);
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
  background: rgba(107,158,255,0.08);
  border-color: var(--cf-indigo);
  color: var(--cf-indigo);
  transform: translateY(-3px) scale(1.02);
  box-shadow: 0 6px 20px rgba(107,158,255,0.18);
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
  background: #ffffff;
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


/* ── 标签切换过渡 ── */
.tag-swap-enter-active,
.tag-swap-leave-active { transition: opacity .15s, transform .15s; }
.tag-swap-enter-from   { opacity: 0; transform: translateY(-4px) scale(.94); }
.tag-swap-leave-to     { opacity: 0; transform: translateY(4px)  scale(.94); }

:deep(.s-tag--saving)  { color: #64748b !important; border-color: #cbd5e1 !important; }
:deep(.s-tag--reflect) { color: #059669 !important; border-color: #6ee7b7 !important; }
</style>
