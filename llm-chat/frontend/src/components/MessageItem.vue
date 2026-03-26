<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import { marked } from 'marked'
import type { Message, ToolCallRecord } from '../types'
import { CopyDocument, Check, User, Search, Clock, Cpu, Document } from '@element-plus/icons-vue'

// 工具元信息
const TOOL_META: Record<string, { label: string; icon: any; color: string }> = {
  web_search:        { label: '搜索了网络',  icon: Search,   color: '#6366f1' },
  fetch_webpage:     { label: '阅读了网页',  icon: Document, color: '#0ea5e9' },
  get_current_time:  { label: '获取了时间',  icon: Clock,    color: '#0ea5e9' },
  calculator:        { label: '执行了计算',  icon: Cpu,      color: '#10b981' },
}
function toolMeta(name: string) {
  return TOOL_META[name] ?? { label: `调用了 ${name}`, icon: Cpu, color: '#6b7280' }
}

function hostname(url: string) {
  try { return new URL(url).hostname.replace('www.', '') }
  catch { return url }
}
function faviconUrl(url: string) {
  try { return `https://www.google.com/s2/favicons?domain=${new URL(url).hostname}&sz=16` }
  catch { return '' }
}

// 折叠状态（done 后自动折叠）
const collapsed = ref<Record<number, boolean>>({})
function toggle(i: number) { collapsed.value[i] = !collapsed.value[i] }
function isCollapsed(i: number) { return collapsed.value[i] ?? false }

const props = defineProps<{ message: Message }>()

// 工具完成时自动折叠
watch(
  () => props.message.toolCalls?.map(t => t.done),
  (dones, prev) => {
    dones?.forEach((done, i) => {
      if (done && !prev?.[i]) {
        setTimeout(() => { collapsed.value[i] = true }, 1200)
      }
    })
  },
  { deep: true }
)

const copied = ref(false)

const renderedContent = computed(() => {
  if (props.message.role === 'assistant') {
    return marked.parse(props.message.content || '') as string
  }
  return ''
})

async function copy() {
  try {
    await navigator.clipboard.writeText(props.message.content)
    copied.value = true
    setTimeout(() => { copied.value = false }, 2000)
  } catch {}
}
</script>

<template>
  <div class="msg" :class="message.role">

    <!-- 用户消息 -->
    <template v-if="message.role === 'user'">
      <div class="user-wrap">
        <!-- 图片 -->
        <div v-if="message.images?.length" class="user-imgs">
          <el-image
            v-for="(img, i) in message.images"
            :key="i"
            :src="img"
            :preview-src-list="message.images"
            :initial-index="i"
            fit="cover"
            class="user-img"
          />
        </div>
        <!-- 文字 -->
        <div v-if="message.content" class="user-bubble">{{ message.content }}</div>
      </div>
      <!-- 用户头像 -->
      <div class="user-avatar">
        <el-icon><User /></el-icon>
      </div>
    </template>

    <!-- AI 消息 -->
    <template v-else>
      <!-- AI 头像 -->
      <div class="ai-avatar">
        <svg width="13" height="13" viewBox="0 0 64 64" fill="none">
          <path d="M36 8L22 34H31L28 56L46 28H36Z" fill="white"/>
        </svg>
      </div>
      <div class="ai-content-wrap">
        <!-- 工具调用块 -->
        <div v-if="message.toolCalls?.length" class="tool-calls">
          <div v-for="(tc, i) in message.toolCalls" :key="i"
               :class="['tool-block', (tc.name === 'web_search' || tc.name === 'fetch_webpage') ? 'tool-block-sources' : '']">

            <!-- web_search：实时追加的 URL 列表 -->
            <template v-if="tc.name === 'web_search'">
              <div class="tool-header tool-header-flat">
                <span class="tool-status-icon">
                  <svg v-if="tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                    <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                  <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                    <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                  </svg>
                </span>
                <el-icon style="font-size:13px; color:#6366f1"><Search /></el-icon>
                <span class="tool-label">搜索了网络</span>
                <span class="tool-query">「{{ (tc.input as any).query }}」</span>
                <span v-if="!tc.done" class="tool-pending">搜索中...</span>
              </div>
              <!-- 逐条 URL 行（实时追加） -->
              <div v-if="tc.searchItems?.length" class="search-url-list">
                <a
                  v-for="(item, si) in tc.searchItems"
                  :key="si"
                  :href="item.url"
                  target="_blank"
                  class="search-url-row"
                  :title="item.title || item.url"
                >
                  <!-- 状态图标 -->
                  <span class="url-status">
                    <svg v-if="item.status === 'done'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="6" stroke="#22c55e" stroke-width="1.5"/>
                      <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.4" stroke-linecap="round"/>
                    </svg>
                    <svg v-else-if="item.status === 'fail'" width="12" height="12" viewBox="0 0 16 16" fill="none">
                      <circle cx="8" cy="8" r="6" stroke="#ef4444" stroke-width="1.5"/>
                      <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#ef4444" stroke-width="1.4" stroke-linecap="round"/>
                    </svg>
                    <svg v-else width="12" height="12" viewBox="0 0 16 16" fill="none" class="spin">
                      <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                    </svg>
                  </span>
                  <!-- Favicon -->
                  <img
                    :src="faviconUrl(item.url)"
                    class="url-favicon"
                    @error="($event.target as HTMLImageElement).style.display='none'"
                  />
                  <!-- URL（截断显示） -->
                  <span class="url-text">{{ item.url }}</span>
                </a>
              </div>
            </template>

            <!-- fetch_webpage：单行显示 -->
            <template v-else-if="tc.name === 'fetch_webpage'">
              <div class="tool-header tool-header-flat">
                <span class="tool-status-icon">
                  <svg v-if="tc.done && tc.fetchStatus === 'fail'" width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="6.5" stroke="#ef4444" stroke-width="1.5"/>
                    <path d="M5.5 5.5l5 5M10.5 5.5l-5 5" stroke="#ef4444" stroke-width="1.4" stroke-linecap="round"/>
                  </svg>
                  <svg v-else-if="tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                    <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                  <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                    <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                  </svg>
                </span>
                <el-icon style="font-size:13px; color:#0ea5e9"><Document /></el-icon>
                <span class="tool-label">{{ tc.done && tc.fetchStatus === 'fail' ? '读取失败' : (tc.done ? '读取了网页' : '正在阅读') }}</span>
                <span v-if="(tc.input as any).url" class="tool-query">
                  <a :href="(tc.input as any).url" target="_blank" class="fetch-url-link" @click.stop>
                    <img
                      :src="faviconUrl((tc.input as any).url)"
                      class="url-favicon"
                      style="margin-right:3px"
                      @error="($event.target as HTMLImageElement).style.display='none'"
                    />{{ (tc.input as any).url }}
                  </a>
                </span>
                <span v-if="!tc.done" class="tool-pending">读取中...</span>
              </div>
            </template>

            <!-- 其他工具：可折叠标准块 -->
            <template v-else>
              <div class="tool-header" @click="toggle(i)">
                <span class="tool-status-icon">
                  <svg v-if="tc.done" width="14" height="14" viewBox="0 0 16 16" fill="none">
                    <circle cx="8" cy="8" r="6.5" stroke="#22c55e" stroke-width="1.5"/>
                    <path d="M5 8l2 2 4-4" stroke="#22c55e" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                  <svg v-else width="14" height="14" viewBox="0 0 16 16" fill="none" class="spin">
                    <circle cx="8" cy="8" r="6" stroke="#a5b4fc" stroke-width="1.5" stroke-dasharray="20 18"/>
                  </svg>
                </span>
                <el-icon :style="{ color: toolMeta(tc.name).color }" style="font-size:13px">
                  <component :is="toolMeta(tc.name).icon" />
                </el-icon>
                <span class="tool-label">{{ toolMeta(tc.name).label }}</span>
                <span v-if="!tc.done" class="tool-pending">执行中...</span>
                <span class="tool-chevron" :class="{ open: !isCollapsed(i) }">›</span>
              </div>
              <Transition name="slide">
                <div v-show="!isCollapsed(i)" class="tool-body">
                  <div class="tool-output-plain">
                    <span class="tool-tag">结果</span>
                    <span>{{ tc.output }}</span>
                  </div>
                </div>
              </Transition>
            </template>

          </div>
        </div>

        <div class="ai-content markdown-body" v-html="renderedContent"></div>
        <!-- 操作行 -->
        <div v-if="message.content" class="ai-actions">
          <el-tooltip :content="copied ? '已复制！' : '复制内容'" placement="top" :show-after="300">
            <button class="action-btn" :class="{ copied }" @click="copy">
              <el-icon><component :is="copied ? Check : CopyDocument" /></el-icon>
              <span>{{ copied ? '已复制' : '复制' }}</span>
            </button>
          </el-tooltip>
        </div>
      </div>
    </template>

  </div>
</template>

<style scoped>
.msg {
  width: 100%;
  padding: 10px 0;
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

/* 用户 */
.msg.user {
  flex-direction: row-reverse;
}
.user-avatar {
  width: 28px; height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #374151 0%, #1f2937 100%);
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 13px;
  flex-shrink: 0;
  margin-top: 2px;
}
.user-wrap {
  display: flex;
  flex-direction: column;
  align-items: flex-end;
  gap: 6px;
  max-width: 68%;
}
.user-imgs {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  justify-content: flex-end;
}
.user-img {
  width: 200px;
  height: 200px;
  border-radius: var(--cf-radius-md) !important;
  border: 1.5px solid var(--cf-border);
  cursor: zoom-in;
}
.user-bubble {
  background: var(--cf-card);
  color: var(--cf-text-1);
  padding: 10px 16px;
  border-radius: 18px 6px 18px 18px;
  font-size: 14.5px;
  font-weight: 400;
  line-height: 1.65;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1.5px solid var(--cf-border);
  box-shadow: var(--cf-shadow-xs);
  letter-spacing: -0.1px;
}

/* AI */
.msg.assistant {
  flex-direction: row;
}
.ai-avatar {
  width: 28px; height: 28px;
  border-radius: 8px;
  background: linear-gradient(135deg, #312e81 0%, #6366f1 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  margin-top: 2px;
  box-shadow: 0 2px 8px rgba(99,102,241,0.3);
}
.ai-content-wrap {
  flex: 1;
  min-width: 0;
  max-width: 86%;
}
.ai-content {
  font-size: 14.5px;
  line-height: 1.75;
  color: var(--cf-text-1);
  letter-spacing: -0.1px;
}

/* ── 工具调用 ── */
.tool-calls { display: flex; flex-direction: column; gap: 6px; margin-bottom: 14px; }
.tool-block {
  border: 1px solid var(--cf-border);
  border-radius: 12px;
  overflow: hidden;
  background: var(--cf-card);
  box-shadow: var(--cf-shadow-xs);
}
/* 来源卡片：无边框阴影，更轻量 */
.tool-block-sources {
  border-color: var(--cf-border-soft);
  box-shadow: none;
  background: transparent;
  border: none;
}
.tool-header {
  display: flex;
  align-items: center;
  gap: 7px;
  padding: 9px 14px;
  cursor: pointer;
  user-select: none;
  font-size: 13px;
  color: var(--cf-text-2);
  transition: background 0.15s;
}
.tool-header:hover { background: var(--cf-active); }
/* 来源卡片标题行：不可点击，无 hover */
.tool-header-flat {
  cursor: default;
  padding: 4px 2px;
  gap: 6px;
}
.tool-header-flat:hover { background: transparent; }
.tool-status-icon { display: flex; align-items: center; flex-shrink: 0; }
.tool-label { font-weight: 600; color: var(--cf-text-1); }
.tool-query {
  color: var(--cf-text-3);
  font-size: 12px;
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.tool-pending {
  font-size: 11px;
  color: var(--cf-text-4);
  animation: blink-text 1.2s ease-in-out infinite;
}
@keyframes blink-text { 0%,100%{opacity:1} 50%{opacity:0.3} }
.tool-chevron {
  font-size: 16px;
  color: var(--cf-text-4);
  transform: rotate(90deg);
  transition: transform 0.25s;
  line-height: 1;
  flex-shrink: 0;
}
.tool-chevron.open { transform: rotate(-90deg); }

/* 展开区（其他工具） */
.tool-body {
  border-top: 1px solid var(--cf-border);
  padding: 8px 10px 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 340px;
  overflow-y: auto;
}

/* ── 搜索 URL 列表（逐条追加） ── */
.search-url-list {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 2px 4px 6px 24px;
}
.search-url-row {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 3px 8px 3px 4px;
  border-radius: 6px;
  text-decoration: none;
  color: var(--cf-text-2);
  font-size: 12px;
  transition: background 0.15s;
  animation: fade-row 0.2s ease both;
  min-width: 0;
}
.search-url-row:hover {
  background: var(--cf-active);
  color: #4f46e5;
}
@keyframes fade-row {
  from { opacity: 0; transform: translateX(-4px); }
  to   { opacity: 1; transform: translateX(0); }
}
.url-status {
  display: flex;
  align-items: center;
  flex-shrink: 0;
}
.url-favicon {
  width: 14px; height: 14px;
  border-radius: 3px;
  flex-shrink: 0;
}
.url-text {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--cf-text-3);
  font-size: 11.5px;
}
.fetch-url-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  color: var(--cf-text-3);
  text-decoration: none;
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 420px;
}
.fetch-url-link:hover { color: #4f46e5; }

/* 普通工具输出 */
.tool-output-plain {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  font-size: 12.5px;
  padding: 4px 2px;
  color: var(--cf-text-3);
}
.tool-tag {
  flex-shrink: 0;
  padding: 1px 7px;
  border-radius: 5px;
  font-size: 11px;
  font-weight: 600;
  background: #eef2ff;
  color: #4f46e5;
  border: 1px solid #e0e7ff;
}

/* 展开折叠动画 */
.slide-enter-active, .slide-leave-active {
  transition: max-height 0.3s ease, opacity 0.25s ease;
  overflow: hidden;
}
.slide-enter-from, .slide-leave-to { max-height: 0; opacity: 0; }
.slide-enter-to, .slide-leave-from { max-height: 400px; opacity: 1; }

@keyframes spin { to { transform: rotate(360deg); } }
.spin { animation: spin 1s linear infinite; transform-origin: center; }

/* 操作行 */
.ai-actions {
  display: flex;
  gap: 4px;
  margin-top: 8px;
  opacity: 0;
  transition: opacity 0.2s;
}
.msg.assistant:hover .ai-actions { opacity: 1; }

.action-btn {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  background: var(--cf-card);
  border: 1.5px solid var(--cf-border);
  border-radius: 8px;
  color: var(--cf-text-4);
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
}
.action-btn:hover {
  border-color: #a5b4fc;
  color: var(--cf-indigo);
  background: var(--cf-active);
}
.action-btn.copied {
  border-color: #bbf7d0;
  color: #16a34a;
  background: #f0fdf4;
}
</style>

<style>
/* ── Markdown 全局样式 ── */
.markdown-body { word-break: break-word; }

.markdown-body p { margin: 0 0 10px; }
.markdown-body p:last-child { margin-bottom: 0; }

.markdown-body h1, .markdown-body h2, .markdown-body h3 {
  font-weight: 700;
  margin: 20px 0 8px;
  line-height: 1.3;
  color: #111827;
  letter-spacing: -0.3px;
}
.markdown-body h1 { font-size: 1.4em; }
.markdown-body h2 { font-size: 1.2em; border-bottom: 1px solid #e4e6ef; padding-bottom: 6px; }
.markdown-body h3 { font-size: 1.05em; }

.markdown-body ul, .markdown-body ol {
  padding-left: 22px;
  margin: 6px 0 12px;
}
.markdown-body li { margin: 5px 0; line-height: 1.65; }

.markdown-body strong { font-weight: 700; color: #111827; }
.markdown-body em { font-style: italic; }

.markdown-body a {
  color: #6366f1;
  text-decoration: underline;
  text-decoration-color: #c7d2fe;
  text-underline-offset: 2px;
}
.markdown-body a:hover { text-decoration-color: #6366f1; }

.markdown-body code {
  background: #eef2ff;
  color: #4f46e5;
  padding: 2px 7px;
  border-radius: 6px;
  font-family: 'Fira Code', 'Cascadia Code', 'JetBrains Mono', Consolas, monospace;
  font-size: 13px;
  font-weight: 500;
  border: 1px solid #e0e7ff;
}

.markdown-body pre {
  background: #0f172a;
  color: #e2e8f0;
  padding: 16px 18px;
  border-radius: 12px;
  overflow-x: auto;
  margin: 12px 0;
  font-size: 13px;
  line-height: 1.65;
  border: 1px solid #1e293b;
  box-shadow: 0 4px 16px rgba(0,0,0,0.15);
}
.markdown-body pre code {
  background: none;
  padding: 0;
  color: inherit;
  font-size: inherit;
  border: none;
  font-weight: 400;
}

.markdown-body blockquote {
  border-left: 3px solid #a5b4fc;
  padding: 8px 16px;
  color: #6b7280;
  margin: 12px 0;
  background: #f5f3ff;
  border-radius: 0 8px 8px 0;
  font-style: italic;
}

.markdown-body hr {
  border: none;
  border-top: 1px solid #e4e6ef;
  margin: 18px 0;
}

.markdown-body table {
  border-collapse: collapse;
  width: 100%;
  margin: 14px 0;
  font-size: 13.5px;
  border-radius: 8px;
  overflow: hidden;
  border: 1px solid #e4e6ef;
}
.markdown-body th, .markdown-body td {
  border: 1px solid #e4e6ef;
  padding: 8px 14px;
  text-align: left;
}
.markdown-body th {
  background: #f3f4f8;
  font-weight: 600;
  color: #374151;
  font-size: 13px;
}
.markdown-body tr:nth-child(even) td { background: #f9fafb; }
.markdown-body tr:hover td { background: #eef2ff; }
</style>
