<script setup lang="ts">
import { ref, computed, nextTick, onMounted } from 'vue'
import type { ConversationInfo } from '../types'
import {
  Plus, Search, ChatDotRound, Delete, Connection,
} from '@element-plus/icons-vue'

const props = defineProps<{
  conversations: ConversationInfo[]
  currentConvId: string | null
  activeConvIds?: Set<string>
}>()

const emit = defineEmits<{
  newChat: []
  select: [id: string]
  delete: [id: string]
}>()

const searchQuery = ref('')

const filteredConversations = computed(() =>
  props.conversations.filter(c =>
    c.title.toLowerCase().includes(searchQuery.value.toLowerCase())
  )
)

// 删除确认
const pendingDelete = ref<string | null>(null)
function confirmDelete(id: string) {
  pendingDelete.value = id
  setTimeout(() => { pendingDelete.value = null }, 2500)
}
function doDelete(id: string) {
  pendingDelete.value = null
  emit('delete', id)
}

import * as api from '../api'

// 重命名
const editingId = ref<string | null>(null)
const editTitle = ref('')
function startRename(id: string, title: string) {
  editingId.value = id
  editTitle.value = title
  nextTick(() => {
    const input = document.querySelector('.rename-input') as HTMLInputElement
    input?.focus()
    input?.select()
  })
}
async function finishRename(id: string) {
  const trimmed = editTitle.value.trim()
  if (trimmed && editingId.value === id) {
    await api.renameConversation(id, trimmed)
    emit('select', id)
  }
  editingId.value = null
}
function cancelRename() { editingId.value = null }

// ── 暗色模式 ──
const isDark = ref(localStorage.getItem('cf_dark') === '1')
function toggleDark() {
  isDark.value = !isDark.value
  document.body.classList.toggle('dark', isDark.value)
  localStorage.setItem('cf_dark', isDark.value ? '1' : '0')
}
onMounted(() => { document.body.classList.toggle('dark', isDark.value) })
</script>

<template>
  <div class="sidebar">

    <!-- Logo — Bilibili 风格 -->
    <div class="sidebar-logo">
      <div class="logo-icon">
        <!-- Bilibili 风格小电视 / 可爱星星 -->
        <svg width="22" height="22" viewBox="0 0 32 32" fill="none">
          <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#00AEEC"/>
          <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#FB7299" opacity="0.7"/>
        </svg>
      </div>
      <span class="logo-text">ChatFlow</span>
      <span class="logo-version">AI</span>
    </div>

    <!-- 新对话按钮 -->
    <div class="sidebar-actions">
      <button class="new-chat-btn" @click="emit('newChat')">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
          <path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
        新对话
      </button>
    </div>

    <!-- 搜索 -->
    <div class="sidebar-search">
      <el-input
        v-model="searchQuery"
        placeholder="搜索对话..."
        :prefix-icon="Search"
        size="small"
        clearable
        class="search-input"
      />
    </div>

    <!-- 对话列表标题 -->
    <div class="section-label">
      <el-icon class="section-icon"><ChatDotRound /></el-icon>
      <span>对话历史</span>
      <el-badge :value="filteredConversations.length" class="conv-count" type="info" />
    </div>

    <!-- 对话列表 -->
    <div class="conv-list">
      <el-empty
        v-if="filteredConversations.length === 0"
        :description="searchQuery ? '无匹配结果' : '暂无对话'"
        :image-size="48"
        style="padding: 20px 0;"
      />
      <div
        v-for="conv in filteredConversations"
        :key="conv.id"
        class="conv-item"
        :class="{ active: conv.id === currentConvId }"
        @click="emit('select', conv.id)"
      >
        <el-icon class="conv-icon"><ChatDotRound /></el-icon>
        <input v-if="editingId === conv.id"
          v-model="editTitle"
          class="rename-input"
          @blur="finishRename(conv.id)"
          @keydown.enter="finishRename(conv.id)"
          @keydown.escape="cancelRename"
          @click.stop
        />
        <span v-else class="conv-title" @dblclick.stop="startRename(conv.id, conv.title)">{{ conv.title }}</span>
        <span v-if="props.activeConvIds?.has(conv.id) && conv.id !== currentConvId" class="conv-active-dot" title="后台生成中"></span>

        <!-- 删除操作 -->
        <div class="conv-actions" @click.stop>
          <template v-if="pendingDelete === conv.id">
            <el-button size="small" type="danger" plain @click="doDelete(conv.id)" style="height:22px;padding:0 6px;font-size:11px;">确认</el-button>
          </template>
          <template v-else>
            <el-tooltip content="删除" placement="top" :show-after="300">
              <el-icon class="del-icon" @click="confirmDelete(conv.id)"><Delete /></el-icon>
            </el-tooltip>
          </template>
        </div>
      </div>
    </div>

    <!-- 底部：运行状态 -->
    <div class="sidebar-footer">
      <button class="dark-toggle" @click="toggleDark" :title="isDark ? '切换亮色' : '切换暗色'">
        <svg v-if="isDark" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <circle cx="12" cy="12" r="5"/><path d="M12 1v2M12 21v2M4.22 4.22l1.42 1.42M18.36 18.36l1.42 1.42M1 12h2M21 12h2M4.22 19.78l1.42-1.42M18.36 5.64l1.42-1.42"/>
        </svg>
        <svg v-else width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M21 12.79A9 9 0 1111.21 3 7 7 0 0021 12.79z"/>
        </svg>
        <span>{{ isDark ? '亮色模式' : '暗色模式' }}</span>
      </button>
      <div class="model-status">
        <span class="status-dot pulse"></span>
        <span class="status-text">智能路由 · 自动选模型</span>
        <el-icon class="status-icon"><Connection /></el-icon>
      </div>
    </div>

  </div>
</template>

<style scoped>
.sidebar {
  width: var(--cf-sidebar-w);
  flex-shrink: 0;
  background: var(--cf-sidebar);
  display: flex;
  flex-direction: column;
  height: 100%;
  border-radius: var(--cf-radius-lg);
  border: 1px solid var(--cf-border-soft);
  box-shadow: var(--cf-shadow-sm);
  overflow: hidden;
}

/* ── Logo — Bilibili 风格 ── */
.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 18px 16px 15px;
  border-bottom: 1px solid var(--cf-border-soft);
}
.logo-icon {
  width: 38px; height: 38px;
  border-radius: 12px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  border: 1.5px solid #D0EEF9;
  box-shadow: 0 2px 8px rgba(0,174,236,0.12);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.logo-icon:hover {
  transform: scale(1.08) rotate(-5deg);
}
.logo-text {
  font-size: 16px;
  font-weight: 800;
  color: var(--cf-text-1);
  letter-spacing: -0.3px;
  flex: 1;
}
.logo-version {
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.5px;
  color: #00AEEC;
  background: rgba(0,174,236,0.08);
  border: 1px solid rgba(0,174,236,0.2);
  padding: 2px 8px;
  border-radius: 20px;
  margin-left: auto;
}

/* ── 新对话 — Bilibili 蓝粉渐变 ── */
.sidebar-actions { padding: 14px 12px 6px; }
.new-chat-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  width: 100%;
  height: 38px;
  background: linear-gradient(135deg, rgba(0,174,236,0.1) 0%, rgba(251,114,153,0.08) 100%);
  border: 1.5px solid rgba(0,174,236,0.3);
  border-radius: 20px;
  color: #00AEEC;
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
  letter-spacing: 0.2px;
}
.new-chat-btn:hover {
  background: linear-gradient(135deg, rgba(0,174,236,0.18) 0%, rgba(251,114,153,0.12) 100%);
  border-color: #00AEEC;
  transform: translateY(-2px) scale(1.02);
  box-shadow: 0 4px 14px rgba(0,174,236,0.2);
}
.new-chat-btn:active { transform: translateY(0) scale(0.98); }

/* ── 搜索 ── */
.sidebar-search { padding: 6px 10px 8px; }
:deep(.search-input .el-input__wrapper) {
  background: var(--cf-bg) !important;
  border-radius: 20px !important;
  border: 1px solid var(--cf-border) !important;
  box-shadow: none !important;
  transition: border-color 0.15s !important;
}
:deep(.search-input .el-input__wrapper:hover),
:deep(.search-input .el-input__wrapper.is-focus) {
  border-color: #00AEEC !important;
}
:deep(.search-input .el-input__inner) {
  font-size: 12.5px !important;
  font-family: inherit !important;
  color: var(--cf-text-2) !important;
}

/* ── 区块标题 ── */
.section-label {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 12px 5px;
  font-size: 10.5px;
  font-weight: 600;
  color: var(--cf-text-4);
  text-transform: uppercase;
  letter-spacing: 0.8px;
}
.section-icon { font-size: 11px; }
.conv-count { margin-left: auto; }
:deep(.conv-count .el-badge__content) {
  font-size: 10px;
  height: 16px; line-height: 16px;
  min-width: 16px; padding: 0 4px;
  background: #E3F6FD !important;
  color: #00AEEC !important;
  border: none !important; box-shadow: none !important;
}

/* ── 对话列表 ── */
.conv-list {
  flex: 1;
  overflow-y: auto;
  padding: 2px 8px;
}
.conv-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin: 1px 0;
  border-radius: 12px;
  cursor: pointer;
  color: var(--cf-text-3);
  font-size: 13px;
  transition: all 0.15s cubic-bezier(0.34,1.56,0.64,1);
  position: relative;
}
.conv-item:hover {
  background: var(--cf-hover);
  color: var(--cf-text-1);
  transform: translateX(2px);
}
.conv-item.active {
  background: linear-gradient(135deg, rgba(0,174,236,0.08), rgba(251,114,153,0.05));
  color: #00AEEC;
  font-weight: 500;
  border: 1px solid rgba(0,174,236,0.18);
  box-shadow: 0 1px 6px rgba(0,174,236,0.08);
}
.conv-item.active .conv-icon { color: #00AEEC; opacity: 1; }
.conv-icon { font-size: 13px; flex-shrink: 0; opacity: 0.45; }
.conv-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.rename-input {
  flex: 1;
  min-width: 0;
  padding: 2px 6px;
  border: 1.5px solid #00AEEC;
  border-radius: 8px;
  font-size: 13px;
  font-family: inherit;
  background: var(--cf-card);
  color: var(--cf-text-1);
  outline: none;
}
.conv-actions {
  opacity: 0;
  display: flex;
  align-items: center;
  transition: opacity 0.15s;
  flex-shrink: 0;
}
.conv-item:hover .conv-actions { opacity: 1; }
.del-icon {
  font-size: 14px;
  color: var(--cf-text-4);
  cursor: pointer;
  padding: 3px;
  border-radius: 6px;
  transition: color 0.15s, background 0.15s;
}
.del-icon:hover { color: #F25D59; background: #FDE8E7; }

/* ── 后台活跃指示 — Bilibili 蓝 ── */
.conv-active-dot {
  width: 6px; height: 6px;
  border-radius: 50%;
  background: #00AEEC;
  flex-shrink: 0;
  animation: conv-pulse 1.4s ease-in-out infinite;
}
@keyframes conv-pulse {
  0%, 100% { opacity: 1; box-shadow: 0 0 0 0 rgba(0,174,236,0.5); }
  50%       { opacity: 0.8; box-shadow: 0 0 0 5px rgba(0,174,236,0); }
}

/* ── Footer ── */
.sidebar-footer {
  padding: 12px;
  border-top: 1px solid var(--cf-border-soft);
}
.model-status {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 9px 13px;
  background: linear-gradient(135deg, #E3F6FD, #D5F5E8);
  border: 1px solid #B8E6D6;
  border-radius: 12px;
  box-shadow: var(--cf-shadow-xs);
}
.status-dot {
  width: 7px; height: 7px;
  border-radius: 50%;
  background: #00B578;
  flex-shrink: 0;
  animation: pulse-green 2s ease-in-out infinite;
}
@keyframes pulse-green {
  0%, 100% { box-shadow: 0 0 0 0 rgba(0,181,120,0.4); }
  50%       { box-shadow: 0 0 0 5px rgba(0,181,120,0); }
}
.status-text { font-size: 11.5px; color: #00875A; font-weight: 500; flex: 1; }
.status-icon { font-size: 12px; color: #00875A; opacity: 0.55; }

/* ── Dark toggle ── */
.dark-toggle {
  display: flex;
  align-items: center;
  gap: 8px;
  width: 100%;
  padding: 8px 13px;
  margin-bottom: 8px;
  background: none;
  border: 1px solid var(--cf-border-soft);
  border-radius: 12px;
  color: var(--cf-text-3);
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.15s;
}
.dark-toggle:hover {
  background: var(--cf-hover);
  color: var(--cf-text-1);
  border-color: var(--cf-border);
}
</style>
