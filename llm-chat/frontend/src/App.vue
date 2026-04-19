<script setup lang="ts">
import { onMounted, computed, ref, watch } from 'vue'
import { useChat } from './composables/useChat'
import type { FileArtifact } from './types'
import Sidebar from './components/Sidebar.vue'
import ChatView from './components/ChatView.vue'
import CognitivePanel from './components/CognitivePanel.vue'

const chat = useChat()

onMounted(async () => {
  await chat.loadConversations()
  await chat.restoreFromHash()
})

// ── 面板折叠/展开（用户可手动控制） ──────────────────────────────────────────
const panelOpen = ref(false)

// 是否存在值得展示的认知内容（计划或日志）
const hasCognitiveContent = computed(() => {
  const cog = chat.cognitive.value
  return cog.plan.length > 0 || cog.traceLog.length > 0 || cog.historyEvents.length > 0 || cog.artifacts.length > 0
})

// 面板展示条件：
//   1. 正在规划中（planning 状态）→ 自动弹出
//   2. 有认知内容 AND 用户未折叠
// 不包括 routing/thinking/tool 状态，避免简单问题也触发面板一闪
const showCognitivePanel = computed(() => {
  if (selectedFile.value && panelOpen.value) return true  // 文件预览时强制展开
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
  panelOpen.value = false  // 默认关，由 planning 状态或用户手动打开
  selectedFile.value = null
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

// 当前对话标题（来自对话列表，就是第一条消息 / 后端生成的摘要标题）
const currentConvTitle = computed(() => {
  if (!chat.currentConvId.value) return ''
  const conv = chat.conversations.value.find(c => c.id === chat.currentConvId.value)
  return conv?.title ?? ''
})

// ── 文件预览状态 ──────────────────────────────────────────────────────────────
const selectedFile = ref<FileArtifact | null>(null)
const fileLoading = ref(false)

async function onSelectFile(file: FileArtifact) {
  // 立即打开面板 + 显示 loading 占位（先让用户看到反馈）
  selectedFile.value = file
  panelOpen.value = true
  if (file.language === 'pptx') {
    panelWidth.value = Math.max(panelWidth.value, 520)
  }

  // 二进制打包/归档文件（jar/tar/zip...）面板里只展示下载入口，
  // 不拉取 base64 内容（可能 >50MB，会把浏览器卡死）
  if (file.language === 'archive') return

  // 如果是元数据模式（无 content），按需从后端加载完整内容
  const needsLoad = !file.content || (file.language === 'pptx' && !file.slides_html?.length)
  if (!needsLoad) return

  fileLoading.value = true
  try {
    // 来源1：cognitive.artifacts（SSE 实时推送的，有完整内容）
    const fromCog = chat.cognitive.value.artifacts.find(
      (a: FileArtifact) => a.name === file.name && a.content
    )
    if (fromCog) {
      selectedFile.value = fromCog
    } else if (file.id) {
      // 来源2：按 ID 从后端加载完整内容
      const full = await import('./api').then(m => m.fetchArtifactContent(file.id!))
      if (full) selectedFile.value = full as FileArtifact
    }
  } catch {}
  fileLoading.value = false
}

watch(() => chat.currentConvId.value, () => {
  selectedFile.value = null
})

// ── 面板拖拽缩放 ──────────────────────────────────────────────────────────────
const panelWidth = ref(400)
const isDragging = ref(false)

// 拖拽遮罩：防止 iframe 等子元素吃掉 mouse 事件
let _dragOverlay: HTMLElement | null = null

function onDragStart(e: MouseEvent) {
  e.preventDefault()
  isDragging.value = true
  document.body.style.cursor = 'col-resize'
  document.body.style.userSelect = 'none'

  // 创建全屏透明遮罩，覆盖所有 iframe 和子元素
  _dragOverlay = document.createElement('div')
  _dragOverlay.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:99999;cursor:col-resize;'
  document.body.appendChild(_dragOverlay)

  const startX = e.clientX
  const startW = panelWidth.value
  const maxW = window.innerWidth - 240 - 400

  function onMove(ev: MouseEvent) {
    const delta = startX - ev.clientX
    panelWidth.value = Math.min(Math.max(startW + delta, 300), maxW)
  }
  function onUp() {
    isDragging.value = false
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
    if (_dragOverlay) { _dragOverlay.remove(); _dragOverlay = null }
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}
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
      @batch-delete="chat.batchRemoveConversations($event)"
    />

    <!-- 全局加载遮罩（刷新恢复数据时）— Bilibili 颜文字风格 -->
    <div v-if="chat.initialLoading.value" class="global-loading-overlay">
      <div class="global-loading-content">
        <div class="bili-kaomoji">(｡･ω･｡)</div>
        <div class="bili-loading-dots">正在恢复对话<span class="dots-anim"></span></div>
      </div>
    </div>

    <!-- 主内容区 -->
    <div class="main-area">
      <!-- 左侧：对话视图 -->
      <ChatView
        :messages="chat.messages.value"
        :loading="chat.loading.value"
        :agentStatus="chat.agentStatus.value"
        :cognitive="chat.cognitive.value"
        :has-cognitive-content="hasCognitiveContent"
        :panel-open="panelOpen"
        :conv-title="currentConvTitle"
        :can-continue="chat.canContinue.value"
        :current-conv-id="chat.currentConvId.value"
        :class="showCognitivePanel ? 'chat-with-panel' : 'chat-full'"
        @send="chat.send($event)"
        @stop="chat.stopConversation()"
        @toggle-panel="panelOpen = !panelOpen"
        @clarification-submit="chat.submitClarification($event)"
        @continue="chat.continueLast()"
        @dismiss-continue="chat.dismissContinue()"
        @regenerate="chat.regenerate()"
        @edit-message="chat.editMessage($event.index, $event.content)"
        @select-file="onSelectFile($event)"
        @ensure-conv="chat.newConversation()"
      />

      <!-- 右侧：认知面板（内含拖拽手柄） -->
      <div
        v-if="showCognitivePanel"
        class="panel-wrapper"
        :style="{ width: panelWidth + 'px' }"
      >
        <!-- 拖拽手柄 — absolute 在左边缘 -->
        <div
          class="panel-drag-handle"
          :class="{ 'panel-drag-handle--active': isDragging }"
          @mousedown.prevent="onDragStart"
        ></div>
        <CognitivePanel
          :cognitive="chat.cognitive.value"
          :loading="chat.loading.value"
          :user-message="currentGoal"
          :selected-file="selectedFile"
          :file-loading="fileLoading"
          style="width:100%;height:100%;"
          @collapse="panelOpen = false"
          @modify-plan="chat.applyModifiedPlan($event)"
          @close-file="selectedFile = null"
        />
      </div>
    </div>
  </div>
</template>

<style>
:root {
  /* ══ Bilibili × 科技感 — 浅色设计系统 ══ */

  /* 背景 & 表面 — 极浅蓝灰底色增加科技氛围 */
  --cf-bg:          #F0F2F5;
  --cf-sidebar:     #ffffff;
  --cf-card:        #ffffff;
  --cf-hover:       #E8EBF0;
  --cf-active:      #E3F6FD;

  /* 边框 — 加入微蓝调，不再纯灰 */
  --cf-border:      #DFE3E8;
  --cf-border-soft: #EBF0F5;
  --cf-border-glow: rgba(0, 174, 236, 0.15); /* 科技感微光边框 */

  /* 文字 — Bilibili 标准色阶 */
  --cf-text-1: #18191C;
  --cf-text-2: #61666D;
  --cf-text-3: #9499A0;
  --cf-text-4: #C9CCD0;
  --cf-text-5: #E3E5E7;

  /* 主色 — Bilibili 蓝粉双色 */
  --cf-bili-blue:  #00AEEC;
  --cf-bili-blue-d:#0095CC;
  --cf-bili-pink:  #FB7299;
  --cf-indigo:     #00AEEC;
  --cf-indigo-d:   #0095CC;
  --cf-purple:     #FB7299;
  --cf-green:      #00B578;
  --cf-red:        #F25D59;
  --cf-amber:      #FF9736;

  /* 阴影 — 增加蓝调底色，更有科技质感 */
  --cf-shadow-xs: 0 1px 3px rgba(0, 20, 40, 0.04);
  --cf-shadow-sm: 0 2px 8px rgba(0, 20, 40, 0.06);
  --cf-shadow-md: 0 4px 16px rgba(0, 20, 40, 0.08);
  --cf-shadow-lg: 0 8px 28px rgba(0, 20, 40, 0.10);
  --cf-shadow-glow: 0 0 20px rgba(0, 174, 236, 0.08); /* 科技微光 */

  /* 圆角 */
  --cf-radius-sm: 10px;
  --cf-radius-md: 14px;
  --cf-radius-lg: 18px;
  --cf-radius-xl: 24px;

  /* 毛玻璃 */
  --cf-glass: blur(12px) saturate(180%);
  --cf-glass-bg: rgba(255, 255, 255, 0.72);

  /* 间距 */
  --cf-sidebar-w: 240px;
}

body.dark {
  --cf-bg:          #17181A;
  --cf-sidebar:     #1F2023;
  --cf-card:        #1F2023;
  --cf-hover:       #2B2C30;
  --cf-active:      #1A2F3A;

  --cf-border:      #323335;
  --cf-border-soft: #2B2C30;

  --cf-text-1: #E6E7E9;
  --cf-text-2: #A2A7AE;
  --cf-text-3: #7A7C82;
  --cf-text-4: #505255;
  --cf-text-5: #3B3C3F;

  --cf-bili-blue:  #23ADE5;
  --cf-bili-blue-d:#1A9AD0;
  --cf-bili-pink:  #F67C9B;
  --cf-indigo:     #23ADE5;
  --cf-indigo-d:   #1A9AD0;
  --cf-purple:     #F67C9B;
  --cf-green:      #3DDC84;
  --cf-red:        #F4726D;
  --cf-amber:      #FFB040;

  --cf-shadow-xs: 0 1px 4px rgba(0,0,0,0.25);
  --cf-shadow-sm: 0 2px 8px rgba(0,0,0,0.35);
  --cf-shadow-md: 0 4px 16px rgba(0,0,0,0.45);
  --cf-shadow-lg: 0 8px 28px rgba(0,0,0,0.55);
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
  -moz-osx-font-smoothing: grayscale;
}

/* 全局滚动条 — Bilibili 风格细滚动条 */
::-webkit-scrollbar { width: 4px; height: 4px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: #C9CCD0; border-radius: 99px; }
::-webkit-scrollbar-thumb:hover { background: #9499A0; }

#app { height: 100%; }

/* ── Dark mode: body & scrollbar ── */
body.dark {
  background: var(--cf-bg);
  color: var(--cf-text-1);
}
body.dark ::-webkit-scrollbar-thumb { background: #3B3C3F; }
body.dark ::-webkit-scrollbar-thumb:hover { background: #505255; }

/* Dark mode overrides for components with hardcoded colors */
body.dark .chat-view,
body.dark .messages-scroll { background: #1F2023; }
body.dark .chat-header { background: #1F2023; border-bottom-color: var(--cf-border); box-shadow: 0 1px 6px rgba(0,0,0,0.3); }
body.dark .user-bubble { background: #1A2F3A; color: #E6E7E9; border-color: #1E3A48; }
body.dark .ai-avatar { background: #1F2023; border-color: #323335; }
body.dark .user-avatar { background: #2B2C30; border-color: #323335; }
body.dark .hero-title { background: linear-gradient(135deg, #E6E7E9 0%, #23ADE5 50%, #F67C9B 100%); -webkit-background-clip: text; background-clip: text; }
body.dark .hero-icon-wrap { background: #1F2023; border-color: #323335; }
body.dark .header-brand-icon { background: #1F2023; border-color: #323335; }
body.dark .logo-icon { background: #1F2023; border-color: #323335; }

/* Dark code blocks */
body.dark .markdown-body .code-block { background: #1A1B1D; border-color: #323335; }
body.dark .markdown-body .code-header { background: #222325; border-bottom-color: #323335; }
body.dark .markdown-body .code-lang-badge { color: #A2A7AE; }
body.dark .markdown-body .code-pre { background: #1A1B1D; }
body.dark .markdown-body code { background: #252730; color: #23ADE5; border-color: #323335; }
body.dark .markdown-body th { background: #222325; color: #E6E7E9; }
body.dark .markdown-body th, body.dark .markdown-body td { border-color: #323335; }
body.dark .markdown-body tr:nth-child(even) td { background: #1A1B1D; }
body.dark .markdown-body tr:hover td { background: #1A2F3A; }
body.dark .markdown-body blockquote { background: #1A2530; color: #A2A7AE; border-left-color: #00AEEC; }
body.dark .markdown-body h1, body.dark .markdown-body h2, body.dark .markdown-body h3 { color: #E6E7E9; }
body.dark .markdown-body h2 { border-bottom-color: #323335; }
body.dark .markdown-body strong { color: #E6E7E9; }
body.dark .markdown-body a { color: #23ADE5; text-decoration-color: #0095CC; }

/* Dark mode for tool blocks and think blocks */
body.dark .tool-block { background: #1F2023; border-color: #323335; }
body.dark .tool-block-sources .tool-header-flat { background: #1F2023; border-color: #323335; border-left-color: #00AEEC; }
body.dark .think-block { background: #1F1F2A; border-color: #323345; }
body.dark .think-body { background: #1A1A25; border-top-color: #323345; }

/* Dark mode for step borders */
body.dark .section-wrap.has-step { border-left-color: #323335; }

/* Dark SVG fills in logo/icons */
body.dark .sidebar-logo svg path,
body.dark .header-brand-icon svg path,
body.dark .hero-icon-wrap svg path,
body.dark .ai-avatar svg path { fill: #E6E7E9; }
body.dark .user-avatar svg circle { fill: #A2A7AE; }
body.dark .user-avatar svg path { stroke: #A2A7AE; }

/* Dark workflow card */
body.dark .wf-card { background: #1F2025; border-color: #323340; }
body.dark .wf-card-header { background: #222230; border-bottom-color: #323340; }

/* Dark mode model status in sidebar */
body.dark .model-status { background: linear-gradient(135deg, #0A1A1A, #0A2020); border-color: #1A3A3A; }
body.dark .status-text { color: #3DDC84; }
body.dark .status-icon { color: #3DDC84; }

/* Dark input area */
body.dark .input-card { background: #1F2023; border-color: #323335; }
body.dark .the-textarea { color: #E6E7E9; }
body.dark .input-card:focus-within { border-color: #00AEEC; box-shadow: var(--cf-shadow-lg), 0 0 0 4px rgba(0,174,236,0.15); }

/* Dark continue button */
body.dark .continue-btn { background: #1F2023; }

/* Dark empty state suggestions */
body.dark .sug-card { background: #1F2023; border-color: #323335; color: var(--cf-text-2); }

/* Dark cognitive panel */
body.dark .ai-avatar--breathing { background: #1A2530 !important; }
body.dark .file-code-view { background: #1A1B1D; }
body.dark .file-code-pre { color: #E6E7E9; }
body.dark .file-info-bar { background: rgba(0,174,236,0.05); border-bottom-color: #323335; }
body.dark .file-name-badge { color: #E6E7E9; }
body.dark .file-actions-bar { border-bottom-color: #323335; }
body.dark .file-action-btn { background: #1F2023; border-color: #323335; color: #A2A7AE; }
body.dark .file-action-btn:hover { background: #1A2F3A; border-color: #23ADE5; color: #23ADE5; }
body.dark .file-action-btn.active { background: #1A2F3A; border-color: #23ADE5; color: #23ADE5; }
body.dark .artifact-card { background: #1F2023; border-color: #323335; }
body.dark .artifact-card:hover { border-color: #23ADE5; }
body.dark .artifact-name { color: #E6E7E9; }
body.dark .artifact-icon { background: linear-gradient(135deg, #1A2F3A 0%, #2A1F2A 100%); }

/* Dark sandbox terminal */
body.dark .sandbox-block { background: #1A1B1D; border-color: #323335; }
body.dark .term-titlebar { background: #222325; border-bottom-color: #323335; }
body.dark .term-body { background: #1A1B1D; }
body.dark .term-title { color: #E6E7E9; }
body.dark .term-code-inline { background: #222325; border-color: #323335; color: #A2A7AE; }
</style>

<style scoped>
/* 整体布局 */
.global-loading-overlay {
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  z-index: 9999;
  background: var(--cf-bg, #F1F2F3);
  display: flex; align-items: center; justify-content: center;
}
.global-loading-content {
  display: flex; flex-direction: column; align-items: center; gap: 12px;
}
.bili-kaomoji {
  font-size: 42px; color: var(--cf-text-2, #61666D);
  animation: bili-bounce 1.2s ease-in-out infinite;
}
@keyframes bili-bounce {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-8px); }
}
.bili-loading-dots {
  font-size: 14px; color: var(--cf-text-3, #9499A0);
}
.dots-anim::after {
  content: '';
  animation: dots 1.5s steps(3, end) infinite;
}
@keyframes dots {
  0% { content: ''; }
  33% { content: '.'; }
  66% { content: '..'; }
  100% { content: '...'; }
}

.app {
  display: flex;
  height: 100vh;
  overflow: hidden;
  background: var(--cf-bg);
  padding: 12px;
  gap: 12px;
}
.main-area {
  flex: 1;
  display: flex;
  min-width: 0;
  gap: 0;  /* gap 由手柄自身的 padding 提供 */
  overflow: hidden;
}

/* 对话视图 */
.chat-with-panel { flex: 1; min-width: 380px; overflow: hidden; }
.chat-full       { flex: 1; min-width: 0; }

/* 面板容器 — flex: none，宽度由 JS 内联 style 控制 */
.panel-wrapper {
  flex: none;
  position: relative;
  min-width: 300px;
  max-height: 100%;
  overflow: hidden;
  margin-left: 4px;
}

/* 拖拽手柄 — absolute 覆盖在面板左边缘 */
.panel-drag-handle {
  position: absolute;
  left: -6px;
  top: 0;
  bottom: 0;
  width: 12px;
  cursor: col-resize;
  z-index: 50;
  display: flex;
  align-items: center;
  justify-content: center;
}
.panel-drag-handle::before {
  content: '';
  width: 3px;
  height: 36px;
  border-radius: 2px;
  background: #D0D1D3;
  transition: all 0.15s;
}
.panel-drag-handle:hover::before,
.panel-drag-handle--active::before {
  background: #00AEEC;
  height: 50px;
  box-shadow: 0 0 8px rgba(0,174,236,0.3);
}
</style>
