<script setup lang="ts">
import { onMounted, computed, ref, watch } from 'vue'
import { useChat } from './composables/useChat'
import Sidebar from './components/Sidebar.vue'
import ChatView from './components/ChatView.vue'
import CognitivePanel from './components/CognitivePanel.vue'

const chat = useChat()

onMounted(async () => {
  await chat.loadConversations()
  await chat.restoreFromHash()
})

// ── 面板折叠/展开（用户可手动控制） ──────────────────────────────────────────
const panelOpen = ref(true)

// 是否存在值得展示的认知内容（计划或日志）
const hasCognitiveContent = computed(() => {
  const cog = chat.cognitive.value
  return cog.plan.length > 0 || cog.traceLog.length > 0 || cog.historyEvents.length > 0
})

// 面板展示条件：
//   1. 正在规划中（planning 状态）→ 自动弹出
//   2. 有认知内容 AND 用户未折叠
// 不包括 routing/thinking/tool 状态，避免简单问题也触发面板一闪
const showCognitivePanel = computed(() => {
  const status = chat.agentStatus.value.state
  if (status === 'planning') return true
  return hasCognitiveContent.value && panelOpen.value
})

// 开始规划时自动打开面板
watch(() => chat.agentStatus.value.state, (state) => {
  if (state === 'planning') panelOpen.value = true
})

// 切换会话时折叠认知面板（新会话没有历史计划）
watch(() => chat.currentConvId.value, () => {
  panelOpen.value = true  // 默认开，但 showCognitivePanel 还受 hasCognitiveContent 控制
})

// 当前目标（最新用户消息，优先用 workflowGoal 避免显示后端指令文本）
const currentGoal = computed(() => {
  const msgs = chat.messages.value
  for (let i = msgs.length - 1; i >= 0; i--) {
    const m = msgs[i]
    if (m.role === 'user') return m.workflowGoal || m.content
  }
  return ''
})
</script>

<template>
  <div class="app">
    <Sidebar
      :conversations="chat.conversations.value"
      :currentConvId="chat.currentConvId.value"
      :activeConvIds="chat.activeConvIds.value"
      @new-chat="chat.newConversation()"
      @select="chat.selectConversation($event)"
      @delete="chat.removeConversation($event)"
    />

    <!-- 主内容区 -->
    <div class="main-area">
      <!-- 左侧：对话视图 -->
      <ChatView
        :messages="chat.messages.value"
        :loading="chat.loading.value"
        :agentStatus="chat.agentStatus.value"
        :has-cognitive-content="hasCognitiveContent"
        :panel-open="panelOpen"
        :class="showCognitivePanel ? 'chat-with-panel' : 'chat-full'"
        @send="chat.send($event)"
        @stop="chat.stopConversation()"
        @toggle-panel="panelOpen = !panelOpen"
      />

      <!-- 右侧：认知面板 -->
      <transition name="panel-slide">
        <CognitivePanel
          v-if="showCognitivePanel"
          :cognitive="chat.cognitive.value"
          :loading="chat.loading.value"
          :user-message="currentGoal"
          class="cognitive-panel-slot"
          @collapse="panelOpen = false"
          @modify-plan="chat.applyModifiedPlan($event)"
        />
      </transition>
    </div>
  </div>
</template>

<style>
:root {
  --cf-bg: #f3f4f8;
  --cf-card: #ffffff;
  --cf-border: #e5e7eb;
  --cf-border-soft: #ececf1;
  --cf-text-1: #111827;
  --cf-text-2: #374151;
  --cf-text-3: #6b7280;
  --cf-text-4: #9ca3af;
  --cf-text-5: #d1d5db;
  --cf-indigo: #6366f1;
  --cf-active: #f5f3ff;
  --cf-shadow-xs: 0 1px 3px rgba(0,0,0,0.06);
  --cf-shadow-sm: 0 2px 8px rgba(0,0,0,0.08);
  --cf-sidebar-w: 220px;
}
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
html, body {
  height: 100%;
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC',
               'Hiragino Sans GB', 'Microsoft YaHei', sans-serif;
  font-size: 14px;
  color: var(--cf-text-1);
  background: var(--cf-bg);
  -webkit-font-smoothing: antialiased;
}
#app { height: 100%; }
</style>

<style scoped>
.app {
  display: flex;
  height: 100vh;
  overflow: hidden;
}
.main-area {
  flex: 1;
  display: flex;
  min-width: 0;
  overflow: hidden;
}

/* 对话视图宽度 */
.chat-with-panel { flex: 0 0 60%; min-width: 0; }
.chat-full       { flex: 1; min-width: 0; }

/* 认知面板 */
.cognitive-panel-slot {
  flex: 0 0 40%;
  min-width: 280px;
  max-width: 480px;
}

/* 面板滑入/滑出 */
.panel-slide-enter-active,
.panel-slide-leave-active {
  transition: all 0.28s cubic-bezier(0.4, 0, 0.2, 1);
  overflow: hidden;
}
.panel-slide-enter-from,
.panel-slide-leave-to {
  flex-basis: 0 !important;
  min-width: 0 !important;
  max-width: 0 !important;
  opacity: 0;
}
</style>
