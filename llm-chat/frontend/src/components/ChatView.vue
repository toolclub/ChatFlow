<script setup lang="ts">
import { nextTick, watch, ref, computed, onUnmounted } from 'vue'
import type { Message, SendPayload, AgentStatus, CognitiveState, FileArtifact } from '../types'
import MessageItem from './MessageItem.vue'
import InputBox from './InputBox.vue'
import { Check, Loading } from '@element-plus/icons-vue'
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
  currentConvId?: string | null
  creating?: boolean
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
  ensureConv: [complete: (id: string | null) => void]
}>()

const messagesContainer = ref<HTMLDivElement>()
const messagesInner = ref<HTMLDivElement>()
let scrollFrame: number | null = null
let resizeObserver: ResizeObserver | null = null
let userScrolledUp = false
let disposed = false

function pauseFollowing(event: Event) {
  // Expanding history is an explicit reading action. Preserve the reader's
  // position even if the expanded section changes the document height.
  if ((event.target as Element)?.closest('button, summary')) userScrolledUp = true
}

function scheduleScroll() {
  if (disposed || scrollFrame !== null) return
  scrollFrame = requestAnimationFrame(() => {
    scrollFrame = null
    scrollToBottom()
  })
}

watch(messagesInner, element => {
  resizeObserver?.disconnect()
  if (!element) return
  resizeObserver = new ResizeObserver(scheduleScroll)
  resizeObserver.observe(element)
  scheduleScroll()
}, { flush: 'post' })

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
let progressResetTimer: ReturnType<typeof setTimeout> | null = null

watch(() => props.loading, (val) => {
  if (progressTimer) clearInterval(progressTimer)
  if (progressResetTimer) clearTimeout(progressResetTimer)
  if (val) {
    progress.value = 5
    progressTimer = setInterval(() => {
      if (progress.value < 90) {
        progress.value += Math.random() * 3
      }
    }, 400)
  } else {
    if (progressTimer) clearInterval(progressTimer)
    progress.value = 100
    progressResetTimer = setTimeout(() => { progress.value = 0 }, 600)
  }
})

// Height observation includes streamed step content, file cards and image loads.
// Queue at most one scroll per animation frame, instead of one per SSE event.
watch(() => props.currentConvId, async () => {
  userScrolledUp = false
  await nextTick()
  scheduleScroll()
})
watch(() => props.messages.length, (length, previous) => {
  if (previous === 0 && length > 0) userScrolledUp = false
  scheduleScroll()
})

onUnmounted(() => {
  disposed = true
  resizeObserver?.disconnect()
  if (scrollFrame !== null) cancelAnimationFrame(scrollFrame)
  if (progressTimer) clearInterval(progressTimer)
  if (progressResetTimer) clearTimeout(progressResetTimer)
})

// ── 意图胶囊：点击不自动发送，改为打开输入框上的选配面板，
//    选中后附上"意图芯片"；发送时会追加「澄清协议」指令，促使模型先问再做 ──
type CapsuleKind = 'ppt' | 'research' | 'code' | 'writing'
const capsules: { kind: CapsuleKind; label: string; desc: string; accent: string }[] = [
  { kind: 'ppt',      label: '做 PPT',  desc: '一键出片 · 选主题',     accent: '#00AEEC' },
  { kind: 'research', label: '做研究',  desc: '多档位 · 刨根问底',     accent: '#FB7299' },
  { kind: 'code',     label: '写代码',  desc: '挑一套工程骨架',        accent: '#00AEEC' },
  { kind: 'writing',  label: '搞创作',  desc: '切一种书写体裁',        accent: '#FB7299' },
]

const inputBoxRef = ref<InstanceType<typeof InputBox>>()
function tapCapsule(kind: CapsuleKind) {
  inputBoxRef.value?.openCapsule(kind)
}

// ── 只在 Agent 模式下露出胶囊（Chat 模式保留纯粹输入体验）──
const agentMode = ref(localStorage.getItem('cf_agent_mode') !== 'false')
function onAgentChange(mode: boolean) {
  agentMode.value = mode
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

        <!-- 统一状态标签 — Bilibili 风格圆角胶囊（idle 时不显示） -->
        <transition name="tag-swap" mode="out-in">
          <el-tag
            v-if="agentStatus.state === 'done'"
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
            v-else-if="agentStatus.state !== 'idle'"
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

    <!-- 创建会话时覆盖内容而不卸载输入框，保留首次上传与未发送草稿。 -->
    <div v-if="creating" class="creating-view" role="status" aria-live="polite">
      <div class="creating-icon-wrap">
        <svg width="42" height="42" viewBox="0 0 32 32" fill="none">
          <path d="M16 3C16 3 17.6 11 23.5 14C17.6 17 16 25 16 25C16 25 14.4 17 8.5 14C14.4 11 16 3 16 3Z" fill="#00AEEC"/>
          <path d="M25.5 6C25.5 6 26.2 9.2 28.3 10.2C26.2 11.2 25.5 14.4 25.5 14.4C25.5 14.4 24.8 11.2 22.7 10.2C24.8 9.2 25.5 6 25.5 6Z" fill="#FB7299" opacity="0.6"/>
        </svg>
      </div>
      <div class="creating-text">
        ChatFlow<span class="creating-dots"></span>
      </div>
    </div>

    <!-- ── 空状态 — Bilibili 可爱风 ── -->
    <div v-if="messages.length === 0" class="empty-view">
      <el-empty :image-size="0" description="" class="hero-empty">
        <template #image>
          <div class="hero-icon-wrap">
            <svg width="42" height="42" viewBox="0 0 32 32" fill="none">
              <path d="M16 3C16 3 17.6 11 23.5 14C17.6 17 16 25 16 25C16 25 14.4 17 8.5 14C14.4 11 16 3 16 3Z" fill="#00AEEC"/>
              <path d="M25.5 6C25.5 6 26.2 9.2 28.3 10.2C26.2 11.2 25.5 14.4 25.5 14.4C25.5 14.4 24.8 11.2 22.7 10.2C24.8 9.2 25.5 6 25.5 6Z" fill="#FB7299" opacity="0.6"/>
            </svg>
          </div>
        </template>
        <template #description>
          <h1 class="hero-title">hi~ 有什么可以帮你的？</h1>
          <p class="hero-sub">你的 AI 小助手 · 随时为你服务</p>
        </template>
      </el-empty>

      <InputBox
        ref="inputBoxRef"
        :loading="loading || !!creating"
        :centered="true"
        :currentConvId="currentConvId"
        @send="emit('send', $event)"
        @ensureConv="emit('ensureConv', $event)"
        @agent-change="onAgentChange"
      />

      <!-- 意图胶囊：只在 Agent 模式下露出 —— Chat 模式的快直接体验不掺合 -->
      <Transition name="capsule-tray">
        <div v-if="agentMode" class="capsules">
          <button
            v-for="c in capsules"
            :key="c.kind"
            class="capsule"
            :style="{ '--capsule-accent': c.accent }"
            @click="tapCapsule(c.kind)"
          >
            <span class="capsule-ico" aria-hidden="true">
              <!-- 做 PPT：幻灯片 + 角落亮点 -->
              <svg v-if="c.kind === 'ppt'" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <rect x="3" y="4" width="18" height="13" rx="2.5"/>
                <path d="M9 20h6M12 17v3"/>
                <path d="M7.5 13.5l3-3 2.5 2.5 3.5-4" stroke="#FB7299"/>
                <circle cx="18.5" cy="6" r="1" fill="#FB7299" stroke="none"/>
              </svg>
              <!-- 做研究：放大镜 + 星芒 -->
              <svg v-else-if="c.kind === 'research'" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <circle cx="10.5" cy="10.5" r="5.5"/>
                <path d="M15 15l5 5"/>
                <path d="M10.5 8.2v4.6M8.2 10.5h4.6" stroke="#00AEEC"/>
                <path d="M19 4l.7 1.3L21 6l-1.3.7L19 8l-.7-1.3L17 6l1.3-.7z" fill="#00AEEC" stroke="none"/>
              </svg>
              <!-- 写代码：<> 斜杠 + 光标 -->
              <svg v-else-if="c.kind === 'code'" width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <path d="M8 7l-5 5 5 5"/>
                <path d="M16 7l5 5-5 5"/>
                <path d="M14 5l-4 14" stroke="#FB7299"/>
              </svg>
              <!-- 搞创作：羽毛笔 + 墨点 -->
              <svg v-else width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round">
                <path d="M4 20l3-1 10.5-10.5a2.5 2.5 0 00-3.5-3.5L3.5 15.5 4 20z"/>
                <path d="M13 7l4 4"/>
                <circle cx="6" cy="19" r="1.1" fill="#00AEEC" stroke="none"/>
              </svg>
            </span>
            <span class="capsule-text">
              <span class="capsule-label">{{ c.label }}</span>
              <span class="capsule-desc">{{ c.desc }}</span>
            </span>
            <span class="capsule-ring"></span>
            <span class="capsule-sparkle" aria-hidden="true"></span>
          </button>
        </div>
      </Transition>
    </div>

    <!-- ── 对话视图 ── -->
    <div v-else class="chat-body">
      <div class="messages-scroll" ref="messagesContainer" @scroll="onMessagesScroll">
        <div class="messages-inner" ref="messagesInner" @click.capture="pauseFollowing">
          <MessageItem
            v-for="(msg, i) in messages"
            :key="msg.ui_id || msg.message_id || `${msg.role}:${msg.timestamp}:${i}`"
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
            <el-button type="primary" round class="continue-btn" @click="emit('continue')">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0;margin-right:4px">
                <polyline points="13 17 18 12 13 7"/>
                <polyline points="6 17 11 12 6 7"/>
              </svg>
              继续
            </el-button>
            <span class="continue-hint">上次响应被中断，点击从断点继续</span>
            <el-button class="continue-dismiss" circle size="small" @click="emit('dismissContinue')" title="忽略">
              <svg width="10" height="10" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M12 4L4 12M4 4l8 8"/>
              </svg>
            </el-button>
          </div>
        </div>
      </div>

      <div class="bottom-input">
        <InputBox :loading="loading || !!creating" :currentConvId="currentConvId" @send="emit('send', $event)" @ensureConv="emit('ensureConv', $event)" />
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
  background: var(--cf-card, #ffffff);
  position: relative;
  border-radius: var(--cf-radius-lg);
  overflow: hidden;
  border: 1px solid var(--cf-border-soft);
  box-shadow: var(--cf-shadow-sm), var(--cf-shadow-glow, none);
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
  background: var(--cf-glass-bg, #ffffff);
  backdrop-filter: var(--cf-glass, none);
  -webkit-backdrop-filter: var(--cf-glass, none);
  border-bottom: 1px solid var(--cf-border-soft);
  flex-shrink: 0;
  z-index: 10;
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
:deep(.hero-empty) {
  padding: 0 !important;
  display: flex;
  flex-direction: column;
  align-items: center;
}
/* image-size=0 会把 .el-empty__image 宽度设为 0，导致自定义 80px 图标左对齐偏移。
   这里显式撑开为 auto + 居中，保证图标与下方文字视觉对齐。 */
:deep(.hero-empty .el-empty__image) {
  width: auto !important;
  height: auto !important;
  margin: 0 auto 8px !important;
  display: flex;
  justify-content: center;
}
:deep(.hero-empty .el-empty__description) {
  margin-top: 0;
  text-align: center;
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
  font-size: 26px;
  font-weight: 800;
  letter-spacing: -0.3px;
  line-height: 1.3;
  color: #18191C;
  margin-bottom: 8px;
}
.hero-sub {
  font-size: 14px;
  color: #61666D;
  font-weight: 500;
  letter-spacing: 0.2px;
}

/* ── 意图胶囊 — Bilibili 蓝粉线条风 ── */
.capsules {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  justify-content: center;
  max-width: 680px;
}
.capsule {
  --capsule-accent: #00AEEC;
  position: relative;
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 16px 8px 10px;
  background: #fff;
  border: 1.5px solid #E3E5E7;
  border-radius: 24px;
  font-family: inherit;
  cursor: pointer;
  overflow: hidden;
  transition:
    transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1),
    border-color 0.2s,
    box-shadow 0.2s;
}
.capsule:hover {
  border-color: var(--capsule-accent);
  transform: translateY(-2px) scale(1.03);
  box-shadow:
    0 6px 20px color-mix(in srgb, var(--capsule-accent) 25%, transparent),
    0 2px 8px rgba(0,0,0,0.04);
}
.capsule:active { transform: scale(0.97); }

/* 左侧图标圆角底板 */
.capsule-ico {
  width: 32px; height: 32px;
  border-radius: 10px;
  display: flex; align-items: center; justify-content: center;
  flex-shrink: 0;
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--capsule-accent) 8%, #f0f9ff),
    color-mix(in srgb, var(--capsule-accent) 4%, #fff)
  );
  color: var(--capsule-accent);
  transition: transform 0.25s cubic-bezier(0.34, 1.56, 0.64, 1),
              background 0.2s;
}
.capsule:hover .capsule-ico {
  transform: rotate(-6deg) scale(1.08);
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--capsule-accent) 16%, #f0f9ff),
    color-mix(in srgb, var(--capsule-accent) 8%, #fff)
  );
}

/* 文字区 */
.capsule-text {
  display: flex; flex-direction: column; gap: 1px;
}
.capsule-label {
  font-size: 13.5px;
  font-weight: 700;
  color: #18191C;
  letter-spacing: 0.3px;
  line-height: 1.2;
  transition: color 0.18s;
}
.capsule-desc {
  font-size: 11px;
  color: #9499A0;
  font-weight: 500;
  letter-spacing: 0.1px;
  line-height: 1.2;
  white-space: nowrap;
}
.capsule:hover .capsule-label { color: var(--capsule-accent); }
.capsule:hover .capsule-desc  { color: var(--capsule-accent); opacity: 0.75; }

/* 悬浮内光 */
.capsule-ring {
  position: absolute; inset: 0;
  border-radius: inherit;
  pointer-events: none;
  background: radial-gradient(65% 85% at 100% 100%,
    color-mix(in srgb, var(--capsule-accent) 12%, transparent),
    transparent 60%);
  opacity: 0; transition: opacity 0.25s;
}
.capsule:hover .capsule-ring { opacity: 1; }

/* 右上角微光点 */
.capsule-sparkle {
  position: absolute; top: 5px; right: 8px;
  width: 4px; height: 4px;
  border-radius: 50%;
  background: var(--capsule-accent);
  opacity: 0;
  transition: opacity 0.25s, transform 0.25s;
  transform: scale(0);
}
.capsule:hover .capsule-sparkle {
  opacity: 0.7; transform: scale(1);
}

/* 胶囊组滑入/滑出过渡 */
.capsule-tray-enter-active { transition: opacity 0.25s, transform 0.25s cubic-bezier(0.34,1.56,0.64,1); }
.capsule-tray-leave-active { transition: opacity 0.18s, transform 0.18s; }
.capsule-tray-enter-from   { opacity: 0; transform: translateY(8px) scale(0.96); }
.capsule-tray-leave-to    { opacity: 0; transform: translateY(-4px) scale(0.98); }

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
  padding: 0 40px 0 48px;  /* 左侧多留空间给头像 + 呼吸动画 */
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

/* 继续按钮 — Bilibili 风格 + 脉冲动画 */
.continue-wrap {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 48px 4px;
}
:deep(.continue-btn) {
  font-size: 13px !important;
  font-weight: 500 !important;
  padding: 6px 16px !important;
  height: auto !important;
  background: #00AEEC !important;
  border-color: #00AEEC !important;
  animation: continue-pulse 2s ease-in-out infinite !important;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1) !important;
}
:deep(.continue-btn:hover) {
  transform: translateY(-2px) scale(1.03) !important;
  box-shadow: 0 4px 16px rgba(0,174,236,0.35) !important;
  animation: none !important;
}
@keyframes continue-pulse {
  0%, 100% { box-shadow: 0 0 0 0 rgba(0,174,236,0.4); }
  50% { box-shadow: 0 0 0 8px rgba(0,174,236,0); }
}
.continue-hint {
  font-size: 12px;
  color: var(--cf-text-3);
}
:deep(.continue-dismiss) {
  border: 1px solid var(--cf-border) !important;
  color: var(--cf-text-4) !important;
  background: transparent !important;
  transition: all 0.12s !important;
}
:deep(.continue-dismiss:hover) {
  background: var(--cf-hover) !important;
  color: var(--cf-text-2) !important;
  border-color: var(--cf-border) !important;
}

/* ── 新对话加载占位 ── */
.creating-view {
  position: absolute;
  inset: 0;
  z-index: 90;
  background: var(--cf-card, #ffffff);
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 18px;
  padding: 32px;
  animation: creating-fade-in 0.15s ease-out;
}
@keyframes creating-fade-in {
  from { opacity: 0; transform: translateY(4px); }
  to   { opacity: 1; transform: translateY(0); }
}
.creating-icon-wrap {
  width: 80px; height: 80px;
  border-radius: 24px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border: 2px solid #D0EEF9;
  display: flex;
  align-items: center;
  justify-content: center;
  box-shadow: 0 4px 20px rgba(0,174,236,0.12), 0 2px 8px rgba(251,114,153,0.08);
  animation: creating-pulse 1.4s ease-in-out infinite;
}
@keyframes creating-pulse {
  0%, 100% { transform: scale(1); box-shadow: 0 4px 20px rgba(0,174,236,0.12), 0 2px 8px rgba(251,114,153,0.08); }
  50%      { transform: scale(1.06); box-shadow: 0 6px 24px rgba(0,174,236,0.22), 0 3px 12px rgba(251,114,153,0.14); }
}
.creating-text {
  font-size: 18px;
  font-weight: 700;
  color: var(--cf-text-2);
  letter-spacing: 0.2px;
  display: inline-flex;
  align-items: baseline;
}
.creating-dots::after {
  content: '';
  display: inline-block;
  width: 1.6em;
  text-align: left;
  animation: creating-dots-anim 1.2s steps(4, end) infinite;
}
@keyframes creating-dots-anim {
  0%   { content: ''; }
  25%  { content: '.'; }
  50%  { content: '..'; }
  75%  { content: '...'; }
  100% { content: ''; }
}
body.dark .creating-icon-wrap { background: #1F2023; border-color: #323335; }
body.dark .creating-text { color: var(--cf-text-2); }
</style>
