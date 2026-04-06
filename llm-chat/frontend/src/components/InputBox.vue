<script setup lang="ts">
import { ref } from 'vue'
import type { SendPayload } from '../types'
import { Picture, Promotion, Loading } from '@element-plus/icons-vue'

const props = defineProps<{
  loading: boolean
  centered?: boolean
}>()

const emit = defineEmits<{ send: [payload: SendPayload] }>()

const input = ref('')
const pendingImages = ref<string[]>([])
const fileInputRef = ref<HTMLInputElement>()
const textareaRef = ref<HTMLTextAreaElement>()

// ── Agent 模式开关 ──────────────────────────────────────────────────────────
const agentMode = ref(true)
const tipVisible = ref(false)
const tipText = ref('')
let tipTimer: ReturnType<typeof setTimeout> | null = null

function toggleAgent() {
  agentMode.value = !agentMode.value
  tipText.value = agentMode.value
    ? '⚡ 已切换为 Agent 模式 · 支持规划、搜索、多步推理'
    : '💬 已切换为普通对话 · 更快速、更直接'
  tipVisible.value = true
  if (tipTimer) clearTimeout(tipTimer)
  tipTimer = setTimeout(() => { tipVisible.value = false }, 1800)
}

const canSend = () => (input.value.trim() || pendingImages.value.length > 0) && !props.loading

function handleSend() {
  if (!canSend()) return
  emit('send', { text: input.value, images: [...pendingImages.value], agentMode: agentMode.value })
  input.value = ''
  pendingImages.value = []
  if (textareaRef.value) textareaRef.value.style.height = 'auto'
}

function handleKeydown(e: KeyboardEvent) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault()
    handleSend()
  }
}

function autoResize() {
  const el = textareaRef.value
  if (!el) return
  el.style.height = 'auto'
  el.style.height = Math.min(el.scrollHeight, 200) + 'px'
}

/**
 * 将图片压缩到指定最大边长和 JPEG 质量，防止大图片超出 nginx/后端 body 限制。
 * 原图 ≤ 目标尺寸时仅做格式转换（统一为 JPEG），不放大。
 */
function compressImage(dataUrl: string, maxPx = 1280, quality = 0.82): Promise<string> {
  return new Promise((resolve) => {
    const img = new Image()
    img.onload = () => {
      let { width, height } = img
      if (width > maxPx || height > maxPx) {
        if (width >= height) {
          height = Math.round(height * maxPx / width)
          width = maxPx
        } else {
          width = Math.round(width * maxPx / height)
          height = maxPx
        }
      }
      const canvas = document.createElement('canvas')
      canvas.width = width
      canvas.height = height
      canvas.getContext('2d')!.drawImage(img, 0, 0, width, height)
      resolve(canvas.toDataURL('image/jpeg', quality))
    }
    img.onerror = () => resolve(dataUrl)   // 降级：压缩失败时返回原图
    img.src = dataUrl
  })
}

async function addImageFile(file: File) {
  if (!file.type.startsWith('image/')) return
  const reader = new FileReader()
  reader.onload = async ev => {
    const raw = ev.target?.result as string
    if (!raw) return
    const compressed = await compressImage(raw)
    // 开发期可在控制台看到压缩比
    if (import.meta.env.DEV) {
      const ratio = Math.round((1 - compressed.length / raw.length) * 100)
      console.debug(`[InputBox] 图片压缩: ${Math.round(raw.length/1024)}KB → ${Math.round(compressed.length/1024)}KB (压缩率 ${ratio}%)`)
    }
    pendingImages.value.push(compressed)
  }
  reader.readAsDataURL(file)
}

function handlePaste(e: ClipboardEvent) {
  for (const item of Array.from(e.clipboardData?.items || [])) {
    if (item.type.startsWith('image/')) {
      e.preventDefault()
      const f = item.getAsFile()
      if (f) addImageFile(f)
    }
  }
}

function handleFileSelect(e: Event) {
  for (const f of Array.from((e.target as HTMLInputElement).files || [])) addImageFile(f)
  if (fileInputRef.value) fileInputRef.value.value = ''
}

function handleDrop(e: DragEvent) {
  e.preventDefault()
  for (const f of Array.from(e.dataTransfer?.files || [])) addImageFile(f)
}

function removeImage(i: number) { pendingImages.value.splice(i, 1) }
</script>

<template>
  <div class="input-root" :class="{ centered }" @dragover.prevent @drop="handleDrop">
    <div class="input-card" :class="{ 'is-loading': loading }">

      <!-- 图片预览 -->
      <div v-if="pendingImages.length > 0" class="img-previews">
        <div v-for="(img, i) in pendingImages" :key="i" class="img-thumb">
          <img :src="img" alt="图片" />
          <button class="img-remove" @click="removeImage(i)" title="移除">
            <svg width="8" height="8" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="3.5" stroke-linecap="round">
              <path d="M18 6L6 18M6 6l12 12"/>
            </svg>
          </button>
        </div>
      </div>

      <!-- 文本输入 -->
      <div class="textarea-area">
        <textarea
          ref="textareaRef"
          v-model="input"
          @keydown="handleKeydown"
          @paste="handlePaste"
          @input="autoResize"
          :placeholder="centered ? '分配一个任务或提问任何问题...' : '发消息... （Enter 发送 · Shift+Enter 换行 · 支持粘贴截图）'"
          :disabled="loading"
          rows="1"
          class="the-textarea"
        />
      </div>

      <!-- 底部工具栏 -->
      <div class="toolbar">
        <div class="tl">
          <input
            ref="fileInputRef"
            type="file"
            accept="image/*"
            multiple
            style="display:none"
            @change="handleFileSelect"
          />
          <el-tooltip content="上传图片 / 粘贴截图 (Ctrl+V)" placement="top" :show-after="400">
            <button class="tool-btn" @click="fileInputRef?.click()" :disabled="loading">
              <el-icon><Picture /></el-icon>
            </button>
          </el-tooltip>

          <!-- Agent 模式切换按钮 -->
          <button
            class="agent-toggle"
            :class="{ active: agentMode }"
            @click="toggleAgent"
            :disabled="loading"
            :title="agentMode ? '当前：Agent 模式（点击切换）' : '当前：普通对话（点击切换）'"
          >
            <span class="agent-icon">
              <!-- 闪电 = Agent，气泡 = Chat -->
              <svg v-if="agentMode" width="12" height="12" viewBox="0 0 24 24" fill="currentColor">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
              </svg>
              <svg v-else width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
                <path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/>
              </svg>
            </span>
            <span class="agent-label">{{ agentMode ? 'Agent' : 'Chat' }}</span>
          </button>

          <span v-if="pendingImages.length > 0" class="img-badge">
            {{ pendingImages.length }} 张图片
          </span>
        </div>

        <div class="tr">
          <span v-if="input.length > 20" class="char-count">{{ input.length }}</span>

          <el-tooltip :content="loading ? '生成中...' : (canSend() ? '发送 (Enter)' : '请输入内容')" placement="top" :show-after="300">
            <button
              class="send-btn"
              :class="{ active: canSend(), loading }"
              @click="handleSend"
              :disabled="!canSend()"
            >
              <el-icon v-if="!loading" class="send-icon"><Promotion /></el-icon>
              <el-icon v-else class="spin"><Loading /></el-icon>
            </button>
          </el-tooltip>
        </div>
      </div>
    </div>

    <!-- 切换模式提示条（内联，1.8s 后消失） -->
    <Transition name="mode-tip">
      <div v-if="tipVisible" class="mode-tip-bar">
        {{ tipText }}
      </div>
    </Transition>

    <div class="hint-row" v-if="!tipVisible">
      <span class="hint">Enter 发送 · Shift+Enter 换行</span>
    </div>
  </div>
</template>

<style scoped>
.input-root {
  width: 100%;
}
.input-root.centered {
  max-width: 680px;
  margin: 0 auto;
}

/* 卡片 */
.input-card {
  background: var(--cf-card);
  border: 1.5px solid var(--cf-border);
  border-radius: var(--cf-radius-lg);
  box-shadow: var(--cf-shadow-md);
  overflow: hidden;
  transition: box-shadow 0.2s, border-color 0.2s;
}
.input-card:focus-within {
  border-color: #c4b5fd;
  box-shadow: var(--cf-shadow-lg), 0 0 0 4px rgba(99,102,241,0.07);
}
.input-card.is-loading { opacity: 0.75; }

/* 图片预览 */
.img-previews {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 12px 14px 0;
}
.img-thumb {
  position: relative;
  width: 68px; height: 68px;
  border-radius: 10px;
  overflow: hidden;
  border: 1.5px solid var(--cf-border);
}
.img-thumb img {
  width: 100%; height: 100%;
  object-fit: cover;
  display: block;
}
.img-remove {
  position: absolute;
  top: 3px; right: 3px;
  width: 17px; height: 17px;
  border-radius: 50%;
  background: rgba(0,0,0,0.65);
  color: #fff;
  border: none;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 0;
  transition: background 0.12s;
}
.img-remove:hover { background: rgba(239,68,68,0.9); }

/* 文本区 */
.textarea-area {
  padding: 14px 16px 6px;
}
.the-textarea {
  width: 100%;
  background: none;
  border: none;
  outline: none;
  font-size: 14.5px;
  font-family: 'Inter', inherit;
  font-weight: 400;
  line-height: 1.65;
  color: var(--cf-text-1);
  resize: none;
  max-height: 220px;
  overflow-y: auto;
  letter-spacing: -0.1px;
}
.the-textarea::placeholder {
  color: var(--cf-text-5);
  font-weight: 400;
}

/* 工具栏 */
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 12px 10px;
}
.tl, .tr {
  display: flex;
  align-items: center;
  gap: 6px;
}
.tool-btn {
  width: 30px; height: 30px;
  border-radius: var(--cf-radius-sm);
  background: none;
  border: none;
  color: var(--cf-text-4);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  transition: background 0.12s, color 0.12s;
}
.tool-btn:hover:not(:disabled) {
  background: var(--cf-hover);
  color: var(--cf-indigo);
}
.tool-btn:disabled { opacity: 0.35; cursor: not-allowed; }

.img-badge {
  font-size: 11px;
  color: var(--cf-indigo);
  background: var(--cf-active);
  padding: 2px 8px;
  border-radius: 10px;
  font-weight: 500;
}
.char-count {
  font-size: 11px;
  color: var(--cf-text-5);
  font-variant-numeric: tabular-nums;
}

/* 发送按钮 */
.send-btn {
  width: 32px; height: 32px;
  border-radius: 10px;
  background: var(--cf-hover);
  color: var(--cf-text-4);
  border: 1.5px solid var(--cf-border);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 15px;
  transition: all 0.15s;
}
.send-btn.active {
  background: linear-gradient(135deg, #6366f1 0%, #4f46e5 100%);
  color: #fff;
  border-color: #6366f1;
  box-shadow: 0 2px 10px rgba(99,102,241,0.35);
}
.send-btn.active:hover {
  background: linear-gradient(135deg, #4f46e5 0%, #4338ca 100%);
  transform: scale(1.05);
  box-shadow: 0 4px 16px rgba(99,102,241,0.4);
}
.send-btn:disabled:not(.active) { cursor: not-allowed; }

.send-icon { font-size: 15px; }
.spin {
  font-size: 15px;
  animation: spin 0.8s linear infinite;
}
@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* Agent 模式切换按钮 */
.agent-toggle {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  height: 26px;
  padding: 0 9px;
  border-radius: 13px;
  border: 1.5px solid var(--cf-border);
  background: var(--cf-hover);
  color: var(--cf-text-4);
  font-size: 11.5px;
  font-weight: 500;
  cursor: pointer;
  transition: all 0.18s;
  white-space: nowrap;
  letter-spacing: 0.1px;
}
.agent-toggle:hover:not(:disabled) {
  border-color: #a78bfa;
  color: #a78bfa;
  background: rgba(167,139,250,0.08);
}
.agent-toggle.active {
  background: linear-gradient(135deg, rgba(99,102,241,0.12) 0%, rgba(139,92,246,0.12) 100%);
  border-color: #818cf8;
  color: #a5b4fc;
}
.agent-toggle.active:hover:not(:disabled) {
  background: linear-gradient(135deg, rgba(99,102,241,0.2) 0%, rgba(139,92,246,0.2) 100%);
  border-color: #a78bfa;
  color: #c4b5fd;
}
.agent-toggle:disabled { opacity: 0.4; cursor: not-allowed; }
.agent-icon {
  display: flex;
  align-items: center;
  line-height: 1;
}
.agent-label { line-height: 1; }

/* 切换提示条 */
.mode-tip-bar {
  margin-top: 6px;
  padding: 5px 12px;
  border-radius: 8px;
  background: rgba(99,102,241,0.08);
  border: 1px solid rgba(99,102,241,0.18);
  color: #a5b4fc;
  font-size: 11.5px;
  text-align: center;
  letter-spacing: 0.1px;
}

/* Vue Transition */
.mode-tip-enter-active,
.mode-tip-leave-active {
  transition: opacity 0.25s, transform 0.25s;
}
.mode-tip-enter-from,
.mode-tip-leave-to {
  opacity: 0;
  transform: translateY(-4px);
}

/* 提示行 */
.hint-row {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  margin-top: 8px;
}
.hint {
  font-size: 11px;
  color: var(--cf-text-5);
}
</style>
