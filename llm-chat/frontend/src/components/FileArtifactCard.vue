<script setup lang="ts">
import type { FileArtifact } from '../types'
import { isPreviewable } from '../types'
import { Download, View } from '@element-plus/icons-vue'

const props = defineProps<{
  file: FileArtifact
}>()

const emit = defineEmits<{
  select: [file: FileArtifact]
  download: [file: FileArtifact]
}>()

const LANG_ICON: Record<string, string> = {
  html: '🌐', svg: '🎨', css: '🎨', javascript: '⚡', typescript: '⚡',
  python: '🐍', ruby: '💎', go: '🔷', rust: '🦀', java: '☕',
  shell: '🖥️', json: '📋', yaml: '📋', xml: '📋', markdown: '📝',
  sql: '🗃️', vue: '💚', text: '📄', pptx: '📊', pdf: '📕',
  archive: '📦',
}
const LANG_LABEL: Record<string, string> = {
  html: 'HTML', svg: 'SVG', css: 'CSS', javascript: 'JavaScript', typescript: 'TypeScript',
  python: 'Python', ruby: 'Ruby', go: 'Go', rust: 'Rust', java: 'Java',
  shell: 'Shell', json: 'JSON', yaml: 'YAML', xml: 'XML', markdown: 'Markdown',
  sql: 'SQL', vue: 'Vue', text: 'Text', pptx: 'PowerPoint', pdf: 'PDF',
  archive: '压缩包',
}

/** 根据语言返回 el-tag 的 type */
const LANG_TAG_TYPE: Record<string, string> = {
  html: '', javascript: 'warning', typescript: 'warning',
  python: 'success', go: 'info', rust: 'danger',
  java: 'warning', pptx: 'warning', pdf: 'danger',
  vue: 'success', archive: 'info',
  shell: 'info', json: '', yaml: '', xml: '',
  css: '', svg: '', ruby: 'danger', markdown: '',
  sql: 'info', text: 'info',
}

const icon = LANG_ICON[props.file.language] || '📄'
const langLabel = LANG_LABEL[props.file.language] || props.file.language
const langTagType = LANG_TAG_TYPE[props.file.language] || 'info'
const previewable = isPreviewable(props.file.language)
const isPpt = props.file.language === 'pptx'
const isArchive = props.file.language === 'archive'
const isDownloadable = isArchive || (props.file as any).downloadable

// 文件大小：二进制文件用 size 字段，文本用 content.length
const sizeKb = props.file.size
  ? (props.file.size / 1024).toFixed(1)
  : (props.file.content.length / 1024).toFixed(1)

function downloadFile(e?: Event) {
  e?.stopPropagation()  // 不触发 select
  if (props.file.id) {
    // 通过后端 API 下载（支持二进制/打包文件）
    const a = document.createElement('a')
    a.href = `/api/artifacts/${props.file.id}/download`
    a.download = props.file.name
    a.click()
    return
  }
  // 兜底：前端直接下载（内存中有 content 时）
  const isBinary = props.file.binary || ['pptx', 'pdf', 'archive'].includes(props.file.language)
  if (isBinary) {
    try {
      const byteChars = atob(props.file.content)
      const bytes = new Uint8Array(byteChars.length)
      for (let i = 0; i < byteChars.length; i++) bytes[i] = byteChars.charCodeAt(i)
      const blob = new Blob([bytes], { type: 'application/octet-stream' })
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url; a.download = props.file.name; a.click()
      URL.revokeObjectURL(url)
    } catch (e) { console.error('下载失败:', e) }
  } else {
    const blob = new Blob([props.file.content], { type: 'text/plain' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url; a.download = props.file.name; a.click()
    URL.revokeObjectURL(url)
  }
}
</script>

<template>
  <el-card
    shadow="hover"
    class="artifact-card"
    :class="{ 'artifact-card--ppt': isPpt }"
    :body-style="{ padding: '10px 14px', display: 'flex', alignItems: 'center', gap: '10px' }"
    @click="emit('select', file)"
  >
    <div class="artifact-icon" :class="{ 'artifact-icon--ppt': isPpt }">{{ icon }}</div>
    <div class="artifact-info">
      <div class="artifact-name">{{ file.name }}</div>
      <div class="artifact-meta">
        <el-tag :type="langTagType" size="small" effect="plain" class="lang-tag">
          {{ langLabel }}
        </el-tag>
        <el-tag size="small" type="info" effect="plain" class="size-tag">
          {{ sizeKb }} KB
        </el-tag>
        <el-tag v-if="file.slide_count" size="small" type="info" effect="plain" class="size-tag">
          {{ file.slide_count }} 页
        </el-tag>
        <el-tag v-if="previewable" size="small" effect="light" class="preview-badge">
          可预览
        </el-tag>
        <el-tag v-if="isPpt" size="small" type="warning" effect="light" class="preview-badge">
          点击预览
        </el-tag>
        <el-tag v-if="isArchive" size="small" type="success" effect="light" class="preview-badge">
          可下载
        </el-tag>
      </div>
    </div>
    <!-- 下载按钮 -->
    <el-button
      v-if="file.id"
      text
      :icon="Download"
      class="dl-btn"
      title="下载"
      @click="downloadFile($event)"
    />
    <!-- 操作按钮 -->
    <el-button
      text
      :icon="isArchive ? Download : (isPpt ? View : undefined)"
      class="action-btn"
      @click.stop="isArchive ? downloadFile($event) : emit('select', file)"
    >
      <svg v-if="!isArchive && !isPpt" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round">
        <path d="M9 18l6-6-6-6"/>
      </svg>
    </el-button>
  </el-card>
</template>

<style scoped>
.artifact-card {
  max-width: 360px;
  margin: 6px 0;
  border-radius: 14px !important;
  border: 1.5px solid rgba(0,174,236,0.18) !important;
  cursor: pointer;
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1) !important;
  box-shadow: 0 1px 6px rgba(0,174,236,0.06);
  animation: artifact-bounce-in 0.45s cubic-bezier(0.34,1.56,0.64,1) both;
}

@keyframes artifact-bounce-in {
  0% {
    opacity: 0;
    transform: translateY(16px) scale(0.9);
  }
  100% {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.artifact-card:hover {
  border-color: transparent !important;
  border-image: linear-gradient(135deg, #00AEEC, #FB7299) 1 !important;
  box-shadow: 0 6px 24px rgba(0,174,236,0.18), 0 0 12px rgba(251,114,153,0.08);
  transform: translateY(-3px) scale(1.01);
}
/* Fix: border-image doesn't work with border-radius, use outline trick */
.artifact-card:hover {
  border-color: #00AEEC !important;
  box-shadow: 0 6px 24px rgba(0,174,236,0.18), 0 0 12px rgba(251,114,153,0.08), inset 0 0 0 0.5px rgba(251,114,153,0.3);
}

.artifact-card:active {
  transform: translateY(0) scale(0.99);
}

/* PPT 卡片特殊样式 */
.artifact-card--ppt {
  border-color: #FFD6A5 !important;
  box-shadow: 0 1px 6px rgba(255,152,0,0.08);
}
.artifact-card--ppt:hover {
  border-color: #FF9800 !important;
  box-shadow: 0 6px 20px rgba(255,152,0,0.18);
}

.artifact-icon {
  font-size: 24px;
  width: 40px;
  height: 40px;
  border-radius: 10px;
  background: linear-gradient(135deg, #E3F6FD 0%, #FDE8EF 100%);
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: transform 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.artifact-card:hover .artifact-icon {
  transform: scale(1.08);
}
.artifact-icon--ppt {
  background: linear-gradient(135deg, #FFF3E0 0%, #FFE0B2 100%);
}

.artifact-info {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.artifact-name {
  font-size: 13px;
  font-weight: 600;
  color: #18191C;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.artifact-meta {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.lang-tag {
  font-size: 10px !important;
  height: 20px !important;
  padding: 0 6px !important;
  border-radius: 6px !important;
}

.size-tag {
  font-size: 10px !important;
  height: 20px !important;
  padding: 0 6px !important;
  border-radius: 6px !important;
}

.preview-badge {
  font-size: 10px !important;
  height: 20px !important;
  padding: 0 6px !important;
  border-radius: 8px !important;
}

/* 下载按钮 */
.dl-btn {
  color: var(--cf-text-4, #C9CCD0) !important;
  flex-shrink: 0;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.dl-btn:hover {
  color: var(--cf-bili-blue, #00AEEC) !important;
  transform: scale(1.1);
}

/* 操作按钮 */
.action-btn {
  color: #C9CCD0 !important;
  flex-shrink: 0;
  transition: all 0.2s cubic-bezier(0.34,1.56,0.64,1);
}
.artifact-card:hover .action-btn {
  color: #00AEEC !important;
}
.artifact-card--ppt:hover .action-btn {
  color: #FF9800 !important;
}
</style>
