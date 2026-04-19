<script setup lang="ts">
/**
 * 用户上传文件预览模态（壳）
 *
 * 职责（只做这三件）：
 *   1. 调 pickRenderer(file) 选渲染器；没匹配 → UnsupportedView
 *   2. 调 prepare(source, id) 拉数据；显示 LoadingView 期间
 *   3. <component :is> 挂载渲染器组件，传 { data, file }
 *
 * 不做：
 *   - 不知道有几种文件类型
 *   - 不知道每种文件怎么渲染
 *   - 不维护"不支持列表"
 *
 * 加新格式 → 在 src/preview/renderers/ 加文件 + 在 index.ts 注册一行；本文件不动。
 */
import { ref, watch, computed, shallowRef, onBeforeUnmount } from 'vue'
import { pickRenderer, getUnsupportedHint, getDisplayLabel } from '../preview'
import { prepare } from '../preview/prepare'
import type { PreparedData, PreviewFile } from '../preview/types'
import LoadingView from '../preview/views/LoadingView.vue'
import UnsupportedView from '../preview/views/UnsupportedView.vue'

const props = defineProps<{
  modelValue: boolean
  file: PreviewFile | null
}>()
const emit = defineEmits<{ (e: 'update:modelValue', v: boolean): void }>()

const visible = computed({
  get: () => props.modelValue,
  set: (v) => emit('update:modelValue', v),
})

const renderer = computed(() => props.file ? pickRenderer(props.file) : null)
const hint = computed(() => props.file ? getUnsupportedHint(props.file) : null)
const labelTag = computed(() => props.file ? getDisplayLabel(props.file, renderer.value) : '')
const fileSizeKb = computed(() => props.file ? (props.file.size / 1024).toFixed(1) : '0')

const loading = ref(false)
const error = ref('')
// shallowRef：PreparedData 内含大 ArrayBuffer，不需要深响应
const prepared = shallowRef<PreparedData | null>(null)

function dispose() {
  if (prepared.value && prepared.value.type === 'blobUrl') {
    prepared.value.revoke()
  }
  prepared.value = null
  error.value = ''
}

async function load() {
  dispose()
  if (!props.file || !renderer.value) return
  loading.value = true
  try {
    prepared.value = await prepare(renderer.value.source, props.file.id)
  } catch (e: any) {
    error.value = e?.message || '加载失败'
  } finally {
    loading.value = false
  }
}

watch([visible, () => props.file?.id], ([v]) => {
  if (v) load()
  else dispose()
})

onBeforeUnmount(dispose)
</script>

<template>
  <el-dialog
    v-model="visible"
    :title="file?.name || '文件预览'"
    width="78vw"
    top="6vh"
    destroy-on-close
    append-to-body
    class="upload-preview-dialog"
  >
    <template #header="{ close }">
      <div class="upv-header">
        <div class="upv-header-left">
          <span class="upv-icon">📄</span>
          <span class="upv-title" :title="file?.path || file?.name">{{ file?.name }}</span>
          <el-tag size="small" effect="plain" type="primary">{{ labelTag }}</el-tag>
          <span class="upv-size">{{ fileSizeKb }} KB</span>
        </div>
        <el-button size="small" text title="关闭" @click="close">
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round">
            <path d="M12 4L4 12M4 4l8 8"/>
          </svg>
        </el-button>
      </div>
    </template>

    <div class="upv-body">
      <UnsupportedView v-if="!renderer && file" :hint="hint || undefined" />
      <LoadingView v-else-if="loading" />
      <UnsupportedView v-else-if="error" :hint="error" />
      <component
        v-else-if="renderer && prepared"
        :is="renderer.component"
        :data="prepared"
        :file="file"
      />
    </div>
  </el-dialog>
</template>

<style scoped>
.upv-header {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; padding-right: 4px;
}
.upv-header-left { display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1; }
.upv-icon { font-size: 16px; }
.upv-title {
  font-size: 14px; font-weight: 600; color: #18191C;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  max-width: 460px;
}
.upv-size { font-size: 12px; color: #9499A0; }

.upv-body {
  height: 76vh; min-height: 420px;
  display: flex; flex-direction: column; overflow: hidden;
  background: #F8F9FA; border-radius: 8px;
}
</style>

<style>
.upload-preview-dialog .el-dialog__header { padding: 12px 18px; border-bottom: 1px solid #E3E5E7; margin-right: 0; }
.upload-preview-dialog .el-dialog__body { padding: 12px 16px 16px; }
.upload-preview-dialog .el-dialog__headerbtn { display: none; }
</style>
