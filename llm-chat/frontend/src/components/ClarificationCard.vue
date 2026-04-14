<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ClarificationData, ClarificationItem } from '../types'
import { ArrowRight } from '@element-plus/icons-vue'

const props = defineProps<{
  data: ClarificationData
  loading?: boolean
}>()

const emit = defineEmits<{
  submit: [answers: Record<string, string | string[]>]
}>()

// 兜底 text 项：模型未提供 text 类型时自动追加
const FALLBACK_OTHER: ClarificationItem = {
  id: '__other__',
  type: 'text',
  label: '其他补充（可选）',
  placeholder: '如有其他需求或补充说明，请在此输入...',
}

// 始终保证有一个 text 类型的自由输入框
const effectiveItems = computed<ClarificationItem[]>(() => {
  const hasText = props.data.items.some(i => i.type === 'text')
  return hasText ? props.data.items : [...props.data.items, FALLBACK_OTHER]
})

// 每个 item 的答案：single_choice → string，multi_choice → string[]，text → string
const answers = ref<Record<string, string | string[]>>({})
// "其他" 选项被选中时的补充文本框
const otherText = ref<Record<string, string>>({})

// 初始化答案（含兜底项）
effectiveItems.value.forEach((item: ClarificationItem) => {
  if (item.type === 'multi_choice') {
    answers.value[item.id] = []
  } else {
    answers.value[item.id] = ''
  }
  otherText.value[item.id] = ''
})

/** 判断某个选项是否属于"其他"类（需要追加文本输入） */
function isOtherOption(opt: string): boolean {
  return /其他|other|补充说明/i.test(opt)
}

/** 当前选中值是否是"其他"选项 */
function selectedIsOther(itemId: string): boolean {
  const val = answers.value[itemId]
  if (!val || Array.isArray(val)) return false
  return isOtherOption(val)
}

// 检查必填项
const canSubmit = computed(() => {
  return effectiveItems.value.every((item: ClarificationItem) => {
    if (item.type === 'text') return true
    const val = answers.value[item.id]
    const hasVal = Array.isArray(val) ? val.length > 0 : !!val
    if (!hasVal) return false
    if (item.type === 'single_choice' && selectedIsOther(item.id)) {
      return !!otherText.value[item.id]?.trim()
    }
    return true
  })
})

function toggleMulti(itemId: string, option: string) {
  const arr = answers.value[itemId] as string[]
  const idx = arr.indexOf(option)
  if (idx >= 0) arr.splice(idx, 1)
  else arr.push(option)
}

function isSelected(itemId: string, option: string): boolean {
  const val = answers.value[itemId]
  if (Array.isArray(val)) return val.includes(option)
  return val === option
}

function handleSubmit() {
  if (!canSubmit.value || props.loading) return
  const merged: Record<string, string | string[]> = {}
  effectiveItems.value.forEach((item: ClarificationItem) => {
    const val = answers.value[item.id]
    if (item.type === 'single_choice' && typeof val === 'string' && isOtherOption(val)) {
      const extra = otherText.value[item.id]?.trim()
      merged[item.id] = extra ? `${val}：${extra}` : val
    } else {
      merged[item.id] = val
    }
  })
  emit('submit', merged)
}
</script>

<template>
  <Transition name="clarify-slide">
    <el-card class="clarification-card" shadow="hover">
      <template #header>
        <div class="card-header">
          <span class="card-icon">💬</span>
          <span class="card-title">{{ data.question }}</span>
        </div>
      </template>

      <!-- 问题项列表 -->
      <el-form label-position="top" class="card-body">
        <el-form-item
          v-for="item in effectiveItems"
          :key="item.id"
          :label="item.label"
          class="item-block"
        >
          <!-- 单选 -->
          <el-radio-group
            v-if="item.type === 'single_choice'"
            v-model="(answers[item.id] as string)"
            class="options-grid"
          >
            <el-radio-button
              v-for="opt in item.options"
              :key="opt"
              :value="opt"
              class="opt-radio-btn"
            >
              {{ opt }}
            </el-radio-button>
          </el-radio-group>
          <!-- 选了"其他"时显示补充输入框 -->
          <el-input
            v-if="item.type === 'single_choice' && selectedIsOther(item.id)"
            v-model="otherText[item.id]"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 4 }"
            placeholder="请补充说明你的具体需求..."
            class="other-input"
          />

          <!-- 多选 -->
          <el-checkbox-group
            v-else-if="item.type === 'multi_choice'"
            :model-value="(answers[item.id] as string[])"
            class="options-grid"
          >
            <el-checkbox
              v-for="opt in item.options"
              :key="opt"
              :label="opt"
              :value="opt"
              :checked="isSelected(item.id, opt)"
              class="opt-checkbox"
              @change="toggleMulti(item.id, opt)"
            />
          </el-checkbox-group>

          <!-- 文本输入 -->
          <el-input
            v-else-if="item.type === 'text'"
            v-model="(answers[item.id] as string)"
            type="textarea"
            :autosize="{ minRows: 2, maxRows: 6 }"
            :placeholder="item.placeholder || '请输入...'"
          />
        </el-form-item>
      </el-form>

      <!-- 提交按钮 -->
      <div class="card-footer">
        <el-button
          type="primary"
          round
          :disabled="!canSubmit"
          :loading="loading"
          :icon="loading ? undefined : ArrowRight"
          @click="handleSubmit"
        >
          确认并继续
        </el-button>
      </div>
    </el-card>
  </Transition>
</template>

<style scoped>
/* ── 卡片入场动画 ── */
.clarify-slide-enter-active {
  animation: clarify-bounce-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both;
}
@keyframes clarify-bounce-in {
  0% {
    opacity: 0;
    transform: translateY(30px) scale(0.96);
  }
  100% {
    opacity: 1;
    transform: translateY(0) scale(1);
  }
}

.clarification-card {
  margin-top: 14px;
  max-width: 620px;
  border-radius: 14px !important;
  border: 1px solid #E3E5E7;
  overflow: hidden;
  animation: clarify-bounce-in 0.5s cubic-bezier(0.34, 1.56, 0.64, 1) both;
}

.clarification-card :deep(.el-card__header) {
  padding: 13px 16px 11px;
  background: #FAFBFC;
  border-bottom: 1px solid #EBEDF0;
}

.clarification-card :deep(.el-card__body) {
  padding: 16px 16px 4px;
}

/* ── 头部 ── */
.card-header {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.card-icon {
  font-size: 18px;
  flex-shrink: 0;
  margin-top: 1px;
}
.card-title {
  font-size: 13.5px;
  font-weight: 600;
  color: #18191C;
  line-height: 1.5;
}

/* ── 表单内容区 ── */
.card-body {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.card-body :deep(.el-form-item__label) {
  font-size: 12.5px;
  font-weight: 600;
  color: #18191C;
  padding-bottom: 6px;
}

/* ── 选项网格 ── */
.options-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

/* 单选按钮样式 */
.opt-radio-btn {
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.opt-radio-btn:hover {
  transform: translateY(-1px);
}
.opt-radio-btn:active {
  transform: scale(0.96);
}

.options-grid :deep(.el-radio-button__inner) {
  border-radius: 20px !important;
  border: 1px solid #E3E5E7;
  background: #FAFBFC;
  color: #61666D;
  font-size: 12.5px;
  font-weight: 500;
  padding: 7px 14px;
  box-shadow: none;
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.options-grid :deep(.el-radio-button__inner:hover) {
  color: #18191C;
  background: #F1F2F3;
  border-color: #C9CCD0;
}
.options-grid :deep(.el-radio-button.is-active .el-radio-button__inner) {
  background: #F0FAFD;
  color: #00AEEC;
  border-color: #00AEEC;
  font-weight: 600;
  box-shadow: none;
}
/* 去掉 radio-group 的连接样式 */
.options-grid :deep(.el-radio-button:first-child .el-radio-button__inner) {
  border-radius: 20px !important;
  border-left: 1px solid #E3E5E7;
}
.options-grid :deep(.el-radio-button.is-active:first-child .el-radio-button__inner) {
  border-left-color: #00AEEC;
}
.options-grid :deep(.el-radio-button:last-child .el-radio-button__inner) {
  border-radius: 20px !important;
}
.options-grid :deep(.el-radio-button + .el-radio-button) {
  margin-left: 0;
}
.options-grid :deep(.el-radio-button__original-radio + .el-radio-button__inner) {
  border-left-width: 1px;
}

/* 多选样式 */
.opt-checkbox {
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
  margin-right: 0;
}
.opt-checkbox:hover {
  transform: translateY(-1px);
}
.opt-checkbox:active {
  transform: scale(0.96);
}

.opt-checkbox :deep(.el-checkbox__inner) {
  border-radius: 4px;
  border-color: #C9CCD0;
  transition: all 0.2s;
}
.opt-checkbox :deep(.el-checkbox__input.is-checked .el-checkbox__inner) {
  background-color: #00AEEC;
  border-color: #00AEEC;
}
.opt-checkbox :deep(.el-checkbox__input.is-checked + .el-checkbox__label) {
  color: #00AEEC;
}
.opt-checkbox :deep(.el-checkbox__label) {
  font-size: 12.5px;
  font-weight: 500;
  color: #61666D;
}

/* ── 补充输入框 ── */
.other-input {
  margin-top: 8px;
}
.other-input :deep(.el-textarea__inner) {
  border-color: #D0EEF9;
  background: #F8FCFE;
  border-radius: 10px;
}
.other-input :deep(.el-textarea__inner:focus) {
  border-color: #00AEEC;
  box-shadow: 0 0 0 2px rgba(0,174,236,0.08);
}

/* 文本输入框通用 */
.card-body :deep(.el-textarea__inner) {
  border-radius: 10px;
  font-size: 13px;
  color: #18191C;
  transition: border-color 0.15s, box-shadow 0.15s;
}
.card-body :deep(.el-textarea__inner:focus) {
  border-color: #00AEEC;
  box-shadow: 0 0 0 2px rgba(0,174,236,0.08);
}

/* ── 底部 ── */
.card-footer {
  padding: 12px 0 2px;
  display: flex;
  justify-content: flex-end;
}
.card-footer :deep(.el-button--primary) {
  background: #00AEEC;
  border-color: #00AEEC;
  font-size: 13px;
  font-weight: 600;
  box-shadow: 0 1px 4px rgba(0,174,236,0.2);
  transition: all 0.2s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.card-footer :deep(.el-button--primary:hover) {
  background: #0095CC;
  border-color: #0095CC;
  box-shadow: 0 2px 8px rgba(0,174,236,0.3);
  transform: translateY(-1px);
}
.card-footer :deep(.el-button--primary.is-disabled) {
  background: #E3E5E7;
  border-color: #E3E5E7;
  color: #C9CCD0;
  box-shadow: none;
  transform: none;
}
</style>
