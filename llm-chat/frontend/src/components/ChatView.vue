<script setup lang="ts">
import { nextTick, watch, ref, computed, onMounted, onUnmounted } from 'vue'
import type { Message, SendPayload, AgentStatus, CognitiveState, FileArtifact } from '../types'
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
  convTitle?: string
  canContinue?: boolean
}>()

const emit = defineEmits<{
  send: [payload: SendPayload]
  stop: []
  togglePanel: []
  clarificationSubmit: [answers: Record<string, string | string[]>]
  continue: []
  dismissContinue: []
  regenerate: []
  editMessage: [payload: { index: number; content: string }]
  selectFile: [file: FileArtifact]
}>()

const messagesContainer = ref<HTMLDivElement>()

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

// 流式进度模拟
const progress = ref(0)
let progressTimer: ReturnType<typeof setInterval> | null = null

watch(() => props.loading, (val) => {
  if (val) {
    userScrolledUp = false
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

watch(
  () => props.messages.length,
  async (newLen, oldLen) => {
    if (newLen < oldLen) {
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

    <!-- 顶部进度条 — Bilibili 蓝粉渐变 -->
    <div class="top-progress" :class="{ visible: props.loading || showProgress }">
      <el-progress
        :percentage="Math.min(progress, 100)"
        :show-text="false"
        :stroke-width="2.5"
        status=""
        class="gen-progress"
      />
    </div>

    <!-- 顶部 header -->
    <div class="chat-header">
      <div class="header-left">
        <div class="header-brand-icon">
          <svg width="16" height="16" viewBox="0 0 32 32" fill="none">
            <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#00AEEC"/>
            <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#FB7299" opacity="0.6"/>
          </svg>
        </div>

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
        <!-- 认知面板切换按钮 -->
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

        <!-- 统一状态标签 — Bilibili 风格圆角胶囊 -->
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
            :style="agentStatus.state === 'planning'       ? 'color:#FB7299;border-color:#FDD4E0'
                  : agentStatus.state === 'vision_analyze' ? 'color:#00AEEC;border-color:#B8E6F9'
                  : ''"
          >
            <el-icon class="s-spin" style="margin-right:4px"><Loading /></el-icon>
            {{ agentStatus.state === 'routing'        ? '分析中'
             : agentStatus.state === 'planning'       ? '规划中'
             : agentStatus.state === 'vision_analyze' ? '图像解析中'
             : '推理中' }}
          </el-tag>
        </transition>

        <!-- 停止按钮 — Bilibili 风格 -->
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
            <rect x="1" y="1" width="8" height="8" rx="2"/>
          </svg>
          停止
        </el-button>
      </div>
    </div>

    <!-- ── 空状态 — Bilibili 可爱风 ── -->
    <div v-if="messages.length === 0" class="empty-view">
      <div class="hero">
        <div class="hero-icon-wrap">
          <svg width="42" height="42" viewBox="0 0 32 32" fill="none">
            <path d="M16 3C16 3 17.6 11 23.5 14C17.6 17 16 25 16 25C16 25 14.4 17 8.5 14C14.4 11 16 3 16 3Z" fill="#00AEEC"/>
            <path d="M25.5 6C25.5 6 26.2 9.2 28.3 10.2C26.2 11.2 25.5 14.4 25.5 14.4C25.5 14.4 24.8 11.2 22.7 10.2C24.8 9.2 25.5 6 25.5 6Z" fill="#FB7299" opacity="0.6"/>
          </svg>
        </div>
        <h1 class="hero-title">hi~ 有什么可以帮你的？</h1>
        <p class="hero-sub">你的 AI 小助手 · 随时为你服务 (｡◕‿◕｡)</p>
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
            :cognitive="(i === messages.length - 1 && msg.role === 'assistant') ? cognitive : undefined"
            :message-index="i"
            @regenerate="emit('regenerate')"
            @edit-message="emit('editMessage', $event)"
            @select-file="emit('selectFile', $event)"
          />
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

          <div v-if="canContinue && !loading" class="continue-wrap">
            <button class="continue-btn" @click="emit('continue')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0">
                <polyline points="13 17 18 12 13 7"/>
                <polyline points="6 17 11 12 6 7"/>
              </svg>
              继续
            </button>
            <span class="continue-hint">上次响应被中断，点击从断点继续</span>
            <button class="continue-dismiss" @click="emit('dismissContinue')" title="忽略">
              <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M12 4L4 12M4 4l8 8"/>
              </svg>
            </button>
          </div>
        </div>
      </div>

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

/* 顶部进度条 — Bilibili 蓝粉渐变 */
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
  background: linear-gradient(90deg, #00AEEC, #FB7299, #00AEEC) !important;
  background-size: 200% !important;
  border-radius: 0 !important;
  animation: shimmer 1.5s linear infinite !important;
}
@keyframes shimmer {
  0% { background-position: 200% center; }
  100% { background-position: -200% center; }
}

/* ── Header — Bilibili 简洁风 ── */
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
  box-shadow: 0 1px 4px rgba(0,0,0,0.03);
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
  width: 32px; height: 32px;
  border-radius: 10px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border: 1.5px solid #D0EEF9;
  box-shadow: 0 1px 4px rgba(0,174,236,0.1);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.header-brand-icon:hover { transform: rotate(-8deg) scale(1.06); }
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

/* ── 状态标签 — Bilibili 圆角胶囊 ── */
:deep(.s-tag) {
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  padding: 0 12px;
  height: 28px;
  border-radius: 99px !important;
  box-shadow: 0 1px 4px rgba(0,0,0,0.05);
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
  background: #00B578;
  animation: pulse-dot 2s ease-in-out infinite;
}
@keyframes pulse-dot {
  0%, 100% { box-shadow: 0 0 0 0 rgba(0,181,120,0.5); }
  50%       { box-shadow: 0 0 0 4px rgba(0,181,120,0); }
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
  height: 28px;
  padding: 0 14px;
  font-size: 12px;
  font-weight: 600;
  display: inline-flex;
  align-items: center;
  border-radius: 99px !important;
  box-shadow: 0 1px 5px rgba(242,93,89,0.18);
  transition: all 0.15s;
}
:deep(.stop-btn:hover) {
  box-shadow: 0 3px 10px rgba(242,93,89,0.28);
  transform: translateY(-1px);
}

/* ── Ghost 按钮 — Bilibili 蓝 ── */
.ghost-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 5px 12px;
  background: transparent;
  border: 1px solid var(--cf-border);
  border-radius: 20px;
  color: #00AEEC;
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.2s ease-out;
  letter-spacing: 0.1px;
}
.ghost-btn:hover {
  background: rgba(0,174,236,0.06);
  border-color: #00AEEC;
  transform: translateY(-1px);
  box-shadow: 0 2px 8px rgba(0,174,236,0.12);
}
.ghost-btn:active { transform: translateY(0); }
.ghost-btn--active {
  background: rgba(0,174,236,0.06);
  border-color: #00AEEC;
  color: #00AEEC;
}
.ghost-btn--active:hover { background: rgba(0,174,236,0.1); }

/* ── 空状态 — Bilibili 卡通风 ── */
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
  width: 80px; height: 80px;
  border-radius: 24px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border: 2px solid #D0EEF9;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-bottom: 4px;
  box-shadow: 0 4px 20px rgba(0,174,236,0.12), 0 2px 8px rgba(251,114,153,0.08);
  animation: hero-float 3s ease-in-out infinite;
}
@keyframes hero-float {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-6px); }
}
.hero-title {
  font-size: 28px;
  font-weight: 800;
  letter-spacing: -0.5px;
  line-height: 1.2;
  background: linear-gradient(135deg, #18191C 0%, #00AEEC 50%, #FB7299 100%);
  -webkit-background-clip: text;
  -webkit-text-fill-color: transparent;
  background-clip: text;
}
.hero-sub {
  font-size: 13.5px;
  color: var(--cf-text-3);
  font-weight: 400;
  letter-spacing: 0.1px;
}

/* ── 快捷操作 — Bilibili 圆角胶囊 ── */
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
  padding: 9px 18px;
  background: var(--cf-card);
  border: 1.5px solid var(--cf-border);
  border-radius: 24px;
  font-size: 13px;
  font-weight: 500;
  color: var(--cf-text-2);
  font-family: inherit;
  cursor: pointer;
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  box-shadow: var(--cf-shadow-xs);
}
.sug-card:hover {
  background: rgba(0,174,236,0.05);
  border-color: #00AEEC;
  color: #00AEEC;
  transform: translateY(-3px) scale(1.03);
  box-shadow: 0 6px 20px rgba(0,174,236,0.15);
}
.sug-card:nth-child(2):hover { border-color: #FB7299; color: #FB7299; box-shadow: 0 6px 20px rgba(251,114,153,0.15); }
.sug-card:nth-child(4):hover { border-color: #FB7299; color: #FB7299; box-shadow: 0 6px 20px rgba(251,114,153,0.15); }
.sug-icon { font-size: 14px; }
.sug-label { font-weight: 500; }
.empty-info {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
  color: var(--cf-text-4);
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
  padding: 4px 0 8px 40px;
}

/* ── 标签切换过渡 ── */
.tag-swap-enter-active,
.tag-swap-leave-active { transition: opacity .15s, transform .15s; }
.tag-swap-enter-from   { opacity: 0; transform: translateY(-4px) scale(.94); }
.tag-swap-leave-to     { opacity: 0; transform: translateY(4px)  scale(.94); }

:deep(.s-tag--saving)  { color: #61666D !important; border-color: #C9CCD0 !important; }
:deep(.s-tag--reflect) { color: #00B578 !important; border-color: #8AE0C0 !important; }

/* 继续按钮 — Bilibili 风格 */
.continue-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 48px 4px;
}
.continue-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 16px;
  border-radius: 20px;
  border: 1.5px solid #00AEEC;
  background: #fff;
  color: #00AEEC;
  font-size: 13px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
}
.continue-btn:hover {
  background: #00AEEC;
  color: #fff;
  box-shadow: 0 2px 10px rgba(0,174,236,0.25);
}
.continue-hint {
  font-size: 12px;
  color: var(--cf-text-3);
}
.continue-dismiss {
  width: 22px; height: 22px;
  border-radius: 50%;
  border: none;
  background: transparent;
  color: var(--cf-text-4);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: all 0.12s;
}
.continue-dismiss:hover { background: var(--cf-hover); color: var(--cf-text-2); }
</style>
