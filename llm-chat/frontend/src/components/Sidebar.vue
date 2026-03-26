<script setup lang="ts">
import { ref, computed } from 'vue'
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
</script>

<template>
  <div class="sidebar">

    <!-- Logo -->
    <div class="sidebar-logo">
      <div class="logo-icon">
        <!-- 星光 sparkle — ChatGPT 同款极简风 -->
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <path d="M12 3C12 3 13 8.5 17 10C13 11.5 12 17 12 17C12 17 11 11.5 7 10C11 8.5 12 3 12 3Z" fill="#111827"/>
          <path d="M19 3C19 3 19.5 5.5 21.5 6.5C19.5 7.5 19 10 19 10C19 10 18.5 7.5 16.5 6.5C18.5 5.5 19 3 19 3Z" fill="#111827" opacity="0.35"/>
        </svg>
      </div>
      <span class="logo-text">ChatFlow</span>
      <span class="logo-badge">AI</span>
    </div>

    <!-- 新对话按钮 -->
    <div class="sidebar-actions">
      <el-button
        type="primary"
        class="new-chat-btn"
        @click="emit('newChat')"
        :icon="Plus"
      >
        新对话
      </el-button>
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
        <span class="conv-title">{{ conv.title }}</span>
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
  width: 248px;
  flex-shrink: 0;
  background: var(--cf-sidebar);
  display: flex;
  flex-direction: column;
  height: 100vh;
  border-right: 1px solid var(--cf-border);
}

/* Logo */
.sidebar-logo {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 18px 16px 14px;
  border-bottom: 1px solid var(--cf-border-soft);
}
.logo-icon {
  width: 30px;
  height: 30px;
  border-radius: 8px;
  background: #ffffff;
  border: 1.5px solid #e5e7eb;
  box-shadow: 0 1px 6px rgba(0,0,0,0.08), 0 0 0 0 transparent;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
}
.logo-text {
  font-size: 15px;
  font-weight: 700;
  color: var(--cf-text-1);
  letter-spacing: -0.3px;
}
.logo-badge {
  margin-left: auto;
  font-size: 9px;
  font-weight: 700;
  letter-spacing: 0.8px;
  background: #f4f4f5;
  color: #52525b;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid #e4e4e7;
}

/* 新对话 */
.sidebar-actions {
  padding: 12px 12px 6px;
}
.new-chat-btn {
  width: 100%;
  font-weight: 500;
  border-radius: var(--cf-radius-sm) !important;
  background: #ffffff !important;
  border: 1.5px solid #e5e7eb !important;
  color: #111827 !important;
  box-shadow: 0 1px 3px rgba(0,0,0,0.06) !important;
  transition: all 0.18s !important;
}
.new-chat-btn:hover {
  background: #f9fafb !important;
  border-color: #d1d5db !important;
  box-shadow: 0 2px 8px rgba(0,0,0,0.08) !important;
  transform: translateY(-1px);
}

/* 搜索 */
.sidebar-search {
  padding: 6px 12px 8px;
}
:deep(.search-input .el-input__wrapper) {
  background: var(--cf-hover) !important;
  border-radius: var(--cf-radius-sm) !important;
  border: 1px solid var(--cf-border) !important;
  box-shadow: none !important;
}
:deep(.search-input .el-input__inner) {
  font-size: 13px !important;
  font-family: inherit !important;
}

/* 区块标题 */
.section-label {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 4px 14px 6px;
  font-size: 11px;
  font-weight: 600;
  color: var(--cf-text-4);
  text-transform: uppercase;
  letter-spacing: 0.7px;
}
.section-icon { font-size: 12px; }
.conv-count {
  margin-left: auto;
}
:deep(.conv-count .el-badge__content) {
  font-size: 10px;
  height: 16px;
  line-height: 16px;
  min-width: 16px;
  padding: 0 4px;
  background: var(--cf-border) !important;
  color: var(--cf-text-3) !important;
  border: none !important;
  box-shadow: none !important;
}

/* 对话列表 */
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
  border-radius: var(--cf-radius-sm);
  cursor: pointer;
  color: var(--cf-text-3);
  transition: background 0.12s, color 0.12s;
  position: relative;
}
.conv-item:hover {
  background: var(--cf-hover);
  color: var(--cf-text-1);
}
.conv-item.active {
  background: var(--cf-active);
  color: var(--cf-indigo);
}
.conv-item.active .conv-icon {
  color: var(--cf-indigo);
}
.conv-icon {
  font-size: 13px;
  flex-shrink: 0;
  opacity: 0.5;
}
.conv-title {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 13px;
  font-weight: 400;
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
  padding: 2px;
  border-radius: 4px;
  transition: color 0.15s, background 0.15s;
}
.del-icon:hover {
  color: var(--cf-red);
  background: #fee2e2;
}

/* 后台流活跃指示器 */
.conv-active-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: #6366f1;
  flex-shrink: 0;
  animation: conv-pulse 1.2s ease-in-out infinite;
  box-shadow: 0 0 0 0 rgba(99,102,241,0.4);
}
@keyframes conv-pulse {
  0% { box-shadow: 0 0 0 0 rgba(99,102,241,0.4); opacity: 1; }
  70% { box-shadow: 0 0 0 5px rgba(99,102,241,0); opacity: 0.8; }
  100% { box-shadow: 0 0 0 0 rgba(99,102,241,0); opacity: 1; }
}

/* Footer */
.sidebar-footer {
  padding: 12px;
  border-top: 1px solid var(--cf-border-soft);
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.footer-label {
  display: flex;
  align-items: center;
  gap: 5px;
  font-size: 11px;
  font-weight: 600;
  color: var(--cf-text-4);
  text-transform: uppercase;
  letter-spacing: 0.5px;
}
.model-option {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.model-option-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--cf-green);
  flex-shrink: 0;
}

/* 模型状态行 */
.model-status {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 6px 10px;
  background: #f0fdf4;
  border: 1px solid #bbf7d0;
  border-radius: var(--cf-radius-sm);
}
.status-dot {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--cf-green);
  flex-shrink: 0;
}
.pulse {
  animation: pulse 2s ease-in-out infinite;
  box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.4);
}
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(34,197,94,0.4); }
  70% { box-shadow: 0 0 0 6px rgba(34,197,94,0); }
  100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
}
.status-text {
  font-size: 11px;
  color: #16a34a;
  font-weight: 500;
  flex: 1;
}
.status-icon {
  font-size: 12px;
  color: #16a34a;
  opacity: 0.6;
}
</style>
