<script setup lang="ts">
import { ref, computed, nextTick, onMounted, watch } from 'vue'
import type { ConversationInfo } from '../types'
import {
  Plus, Search, ChatDotRound, Delete, Connection, Check, Close, Select,
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
  batchDelete: [ids: string[]]
}>()

const searchQuery = ref('')

const filteredConversations = computed(() =>
  props.conversations.filter(c =>
    c.title.toLowerCase().includes(searchQuery.value.toLowerCase())
  )
)

// ── 单个删除（el-popconfirm 确认后触发） ──
const singleDeleting = ref(false)
async function doDelete(id: string) {
  singleDeleting.value = true
  deletingIds.value.add(id)
  // 等退出动画完成再通知父组件删除数据
  await new Promise(r => setTimeout(r, 300))
  emit('delete', id)
  // 不清 deletingIds——等 conversations prop 更新后 item 自然消失
  // Vue 的 TransitionGroup leave 动画会接管
  singleDeleting.value = false
}

// ── 批量选择模式 ──
const batchMode = ref(false)
const selectedIds = ref<Set<string>>(new Set())
const batchDeleting = ref(false)  // 批量删除进行中

function toggleBatchMode() {
  batchMode.value = !batchMode.value
  if (!batchMode.value) {
    selectedIds.value = new Set()
  }
}

function toggleSelect(id: string) {
  const next = new Set(selectedIds.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  selectedIds.value = next
}

const allSelected = computed(() =>
  filteredConversations.value.length > 0 &&
  filteredConversations.value.every(c => selectedIds.value.has(c.id))
)

function toggleSelectAll() {
  if (allSelected.value) {
    selectedIds.value = new Set()
  } else {
    selectedIds.value = new Set(filteredConversations.value.map(c => c.id))
  }
}

// 批量删除确认
const showBatchConfirm = ref(false)
function confirmBatchDelete() {
  if (selectedIds.value.size === 0) return
  showBatchConfirm.value = true
}

async function doBatchDelete() {
  showBatchConfirm.value = false
  batchDeleting.value = true
  const ids = [...selectedIds.value]
  // 全部标记为 deleting（同时触发退出动画）
  ids.forEach(id => deletingIds.value.add(id))
  // 等退出动画完成
  await new Promise(r => setTimeout(r, 350))
  // 通知父组件删除数据（API 调用）
  emit('batchDelete', ids)
  // 清理状态（conversations prop 更新后 item 自然消失）
  selectedIds.value = new Set()
  batchMode.value = false
  batchDeleting.value = false
}

function cancelBatchConfirm() {
  showBatchConfirm.value = false
}

// 退出批量模式时清空选择
watch(batchMode, (v) => {
  if (!v) selectedIds.value = new Set()
})

// conversations 更新后清理残留的 deletingIds（API 返回，数据已删除）
watch(() => props.conversations, () => {
  if (deletingIds.value.size > 0) {
    const existing = new Set(props.conversations.map(c => c.id))
    for (const id of deletingIds.value) {
      if (!existing.has(id)) deletingIds.value.delete(id)
    }
  }
})

// ── 退出动画跟踪 ──
const deletingIds = ref<Set<string>>(new Set())

import * as api from '../api'

// ── 重命名 ──
const editingId = ref<string | null>(null)
const editTitle = ref('')
function startRename(id: string, title: string) {
  if (batchMode.value) return
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
        <svg width="22" height="22" viewBox="0 0 32 32" fill="none">
          <path d="M16 4C16 4 17.5 11 23 14C17.5 17 16 24 16 24C16 24 14.5 17 9 14C14.5 11 16 4 16 4Z" fill="#00AEEC"/>
          <path d="M25 7C25 7 25.6 9.8 27.5 10.7C25.6 11.6 25 14.4 25 14.4C25 14.4 24.4 11.6 22.5 10.7C24.4 9.8 25 7 25 7Z" fill="#FB7299" opacity="0.7"/>
        </svg>
      </div>
      <span class="logo-text">ChatFlow</span>
      <span class="logo-version">AI</span>
    </div>

    <!-- 新对话 + 管理按钮 -->
    <div class="sidebar-actions">
      <button class="new-chat-btn" @click="emit('newChat')">
        <svg width="13" height="13" viewBox="0 0 16 16" fill="none">
          <path d="M8 2v12M2 8h12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>
        </svg>
        新对话
      </button>
      <button
        class="manage-btn"
        :class="{ active: batchMode }"
        @click="toggleBatchMode"
        title="批量管理"
      >
        <svg v-if="!batchMode" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
          <path d="M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2"/>
          <rect x="9" y="3" width="6" height="4" rx="1"/>
        </svg>
        <el-icon v-else><Close /></el-icon>
      </button>
    </div>

    <!-- 批量操作栏 -->
    <Transition name="batch-bar">
      <div v-if="batchMode" class="batch-bar">
        <el-checkbox
          :model-value="allSelected"
          @change="(_: any) => toggleSelectAll()"
          class="batch-checkbox"
        >全选</el-checkbox>
        <span class="batch-count">已选 {{ selectedIds.size }} 项</span>
        <el-button-group class="batch-btn-group">
          <el-button
            type="danger"
            size="small"
            round
            :icon="Delete"
            :disabled="selectedIds.size === 0 || batchDeleting"
            @click="confirmBatchDelete"
            class="batch-delete-btn"
          >删除</el-button>
        </el-button-group>
      </div>
    </Transition>

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
      <TransitionGroup name="conv-item-anim" tag="div">
        <div
          v-for="conv in filteredConversations"
          :key="conv.id"
          class="conv-item"
          :class="{
            active: conv.id === currentConvId && !batchMode,
            selected: selectedIds.has(conv.id),
            deleting: deletingIds.has(conv.id),
          }"
          @click="batchMode ? toggleSelect(conv.id) : emit('select', conv.id)"
        >
          <!-- 批量选择复选框 -->
          <Transition name="checkbox-fade">
            <el-checkbox
              v-if="batchMode"
              :model-value="selectedIds.has(conv.id)"
              @click.stop
              @change="toggleSelect(conv.id)"
              class="conv-checkbox"
            />
          </Transition>

          <el-icon v-if="!batchMode" class="conv-icon"><ChatDotRound /></el-icon>
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

          <!-- 删除操作（非批量模式） -->
          <div v-if="!batchMode" class="conv-actions" @click.stop>
            <el-popconfirm
              title="确认删除此对话？"
              confirm-button-text="删除"
              cancel-button-text="取消"
              confirm-button-type="danger"
              icon-color="#F25D59"
              width="200"
              @confirm="doDelete(conv.id)"
            >
              <template #reference>
                <el-icon class="del-icon"><Delete /></el-icon>
              </template>
            </el-popconfirm>
          </div>
        </div>
      </TransitionGroup>
    </div>

    <!-- 批量删除确认弹窗 -->
    <el-dialog
      v-model="showBatchConfirm"
      title="确认删除"
      width="360px"
      :close-on-click-modal="true"
      :close-on-press-escape="true"
      align-center
      class="batch-delete-dialog"
      @close="cancelBatchConfirm"
    >
      <div class="dialog-body">
        <el-result icon="warning" title="" sub-title="">
          <template #sub-title>
            <span class="dialog-desc">
              将永久删除 <strong>{{ selectedIds.size }}</strong> 个对话及其所有消息、文件产物和执行记录，此操作不可恢复。
            </span>
          </template>
        </el-result>
      </div>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="cancelBatchConfirm" round>取消</el-button>
          <el-button type="danger" round :loading="batchDeleting" @click="doBatchDelete">
            {{ batchDeleting ? '删除中...' : '确认删除' }}
          </el-button>
        </div>
      </template>
    </el-dialog>

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
  background: var(--cf-glass-bg, var(--cf-sidebar));
  backdrop-filter: var(--cf-glass, none);
  -webkit-backdrop-filter: var(--cf-glass, none);
  display: flex;
  flex-direction: column;
  height: 100%;
  border-radius: var(--cf-radius-lg);
  border: 1px solid var(--cf-border-soft);
  box-shadow: var(--cf-shadow-sm), var(--cf-shadow-glow, none);
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

/* ── 新对话 + 管理按钮 ── */
.sidebar-actions {
  padding: 14px 12px 6px;
  display: flex;
  gap: 8px;
}
.new-chat-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  flex: 1;
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

.manage-btn {
  width: 38px; height: 38px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  border: 1.5px solid var(--cf-border);
  background: var(--cf-card, #fff);
  color: var(--cf-text-3);
  cursor: pointer;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.manage-btn:hover {
  border-color: #00AEEC;
  color: #00AEEC;
  background: rgba(0,174,236,0.06);
}
.manage-btn.active {
  border-color: #FB7299;
  color: #FB7299;
  background: rgba(251,114,153,0.08);
}

/* ── 批量操作栏 ── */
.batch-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin: 0 8px 2px;
  background: linear-gradient(135deg, rgba(251,114,153,0.06), rgba(0,174,236,0.04));
  border: 1px solid rgba(251,114,153,0.2);
  border-radius: 12px;
}
.batch-select-all {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  color: var(--cf-text-3);
  cursor: pointer;
  user-select: none;
}
.batch-select-all:hover { color: var(--cf-text-1); }
.batch-count {
  flex: 1;
  font-size: 11px;
  color: var(--cf-text-4);
  text-align: center;
}
/* 批量操作按钮组 */
.batch-btn-group { margin-left: auto; }
:deep(.batch-delete-btn) {
  border-radius: 16px !important;
  font-size: 12px !important;
  font-weight: 600 !important;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1) !important;
}
:deep(.batch-delete-btn:hover:not(:disabled)) {
  transform: scale(1.05);
  box-shadow: 0 2px 10px rgba(242,93,89,0.3);
}
/* 批量全选 checkbox */
:deep(.batch-checkbox .el-checkbox__inner) {
  border-radius: 5px;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.batch-checkbox .el-checkbox__input.is-checked .el-checkbox__inner) {
  background: linear-gradient(135deg, #00AEEC, #FB7299);
  border-color: transparent;
}
:deep(.batch-checkbox .el-checkbox__label) {
  font-size: 12px;
  color: var(--cf-text-3);
}

/* batch-bar 动画 */
.batch-bar-enter-active { animation: slideDown 0.25s cubic-bezier(0.34,1.56,0.64,1); }
.batch-bar-leave-active { animation: slideDown 0.2s ease reverse; }
@keyframes slideDown {
  from { opacity: 0; transform: translateY(-8px) scaleY(0.9); max-height: 0; }
  to   { opacity: 1; transform: translateY(0) scaleY(1); max-height: 60px; }
}

/* ── 会话列表复选框 ── */
:deep(.conv-checkbox .el-checkbox__inner) {
  border-radius: 5px;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
:deep(.conv-checkbox .el-checkbox__input.is-checked .el-checkbox__inner) {
  background: linear-gradient(135deg, #00AEEC, #FB7299);
  border-color: transparent;
  transform: scale(1.1);
}
.checkbox-fade-enter-active { transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1); }
.checkbox-fade-leave-active { transition: all 0.15s ease; }
.checkbox-fade-enter-from,
.checkbox-fade-leave-to { opacity: 0; transform: scale(0.5); width: 0; margin-right: -8px; }

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
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
  position: relative;
  transform-origin: left center;
}
.conv-item:hover {
  background: var(--cf-hover);
  color: var(--cf-text-1);
  transform: translateX(2px);
  box-shadow: 0 2px 12px rgba(0,0,0,0.06);
}
.conv-item.active {
  background: linear-gradient(135deg, rgba(0,174,236,0.08), rgba(251,114,153,0.05));
  color: #00AEEC;
  font-weight: 500;
  border: 1px solid rgba(0,174,236,0.18);
  box-shadow: 0 1px 6px rgba(0,174,236,0.08);
}
.conv-item.active .conv-icon { color: #00AEEC; opacity: 1; }

/* 批量模式下选中态 */
.conv-item.selected {
  background: linear-gradient(135deg, rgba(0,174,236,0.06), rgba(251,114,153,0.06));
  border: 1px solid rgba(251,114,153,0.2);
}

/* 删除退出动画 */
.conv-item.deleting {
  animation: itemDelete 0.3s cubic-bezier(0.4, 0, 1, 1) forwards;
}
@keyframes itemDelete {
  0%   { opacity: 1; transform: translateX(0) scale(1); max-height: 50px; }
  50%  { opacity: 0.5; transform: translateX(30px) scale(0.95); }
  100% { opacity: 0; transform: translateX(60px) scale(0.8); max-height: 0; padding: 0 10px; margin: 0; overflow: hidden; }
}

/* TransitionGroup 列表动画 */
.conv-item-anim-enter-active { animation: itemEnter 0.3s cubic-bezier(0.34,1.56,0.64,1); }
.conv-item-anim-leave-active { animation: itemLeave 0.25s ease forwards; }
.conv-item-anim-move { transition: transform 0.3s ease; }
@keyframes itemEnter {
  from { opacity: 0; transform: translateX(-20px) scale(0.9); }
  to   { opacity: 1; transform: translateX(0) scale(1); }
}
@keyframes itemLeave {
  from { opacity: 1; transform: translateX(0) scale(1); }
  to   { opacity: 0; transform: translateX(40px) scale(0.85); }
}

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

/* ── 删除操作 ── */
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

/* ── 批量删除 el-dialog 样式 ── */
:deep(.batch-delete-dialog .el-dialog) {
  border-radius: 20px;
  overflow: hidden;
  box-shadow: 0 20px 60px rgba(0,0,0,0.15);
}
:deep(.batch-delete-dialog .el-dialog__header) {
  padding: 18px 24px 12px;
  font-weight: 700;
  border-bottom: 1px solid var(--cf-border-soft);
}
:deep(.batch-delete-dialog .el-dialog__body) {
  padding: 0 !important;
}
.dialog-body {
  text-align: center;
  padding: 8px 0;
}
:deep(.dialog-body .el-result) {
  padding: 16px 20px;
}
:deep(.dialog-body .el-result__icon svg) {
  width: 48px;
  height: 48px;
}
.dialog-desc {
  font-size: 13px;
  color: var(--cf-text-3);
  line-height: 1.6;
}
.dialog-desc strong { color: #F25D59; font-weight: 700; }
.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
:deep(.dialog-footer .el-button--danger) {
  background: linear-gradient(135deg, #F25D59, #FB7299) !important;
  border: none !important;
  transition: all 0.2s !important;
}
:deep(.dialog-footer .el-button--danger:hover) {
  transform: translateY(-1px);
  box-shadow: 0 4px 14px rgba(242,93,89,0.35);
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
