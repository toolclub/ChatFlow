<script setup lang="ts">
import { ref, computed } from 'vue'
import type { ClarificationData, ClarificationItem } from '../types'

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

// 检查必填项（选择题至少选一个；选了"其他"时补充文本框必须填写）
const canSubmit = computed(() => {
  return effectiveItems.value.every((item: ClarificationItem) => {
    if (item.type === 'text') return true  // 文本输入可选
    const val = answers.value[item.id]
    const hasVal = Array.isArray(val) ? val.length > 0 : !!val
    if (!hasVal) return false
    // 选了"其他"时，补充说明必填
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
  // 合并"其他"的补充文本到答案中
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
  <div class="clarification-card">
    <!-- 卡片头部 -->
    <div class="card-header">
      <div class="card-icon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none">
          <circle cx="12" cy="12" r="10" stroke="currentColor" stroke-width="1.8"/>
          <path d="M12 8v1m0 7v-5m0 0c0-1.5 2-2.5 2-4a2 2 0 00-4 0" stroke="currentColor"
                stroke-width="1.8" stroke-linecap="round"/>
        </svg>
      </div>
      <span class="card-title">{{ data.question }}</span>
    </div>

    <!-- 问题项列表 -->
    <div class="card-body">
      <div v-for="item in effectiveItems" :key="item.id" class="item-block">
        <div class="item-label">{{ item.label }}</div>

        <!-- 单选 -->
        <div v-if="item.type === 'single_choice'" class="options-grid">
          <button
            v-for="opt in item.options"
            :key="opt"
            class="opt-btn"
            :class="{ selected: isSelected(item.id, opt) }"
            @click="answers[item.id] = opt"
          >
            <span class="opt-radio">
              <svg v-if="isSelected(item.id, opt)" width="10" height="10" viewBox="0 0 10 10">
                <circle cx="5" cy="5" r="4" fill="currentColor"/>
              </svg>
            </span>
            {{ opt }}
          </button>
        </div>
        <!-- 选了"其他"时显示补充输入框 -->
        <textarea
          v-if="item.type === 'single_choice' && selectedIsOther(item.id)"
          v-model="otherText[item.id]"
          class="text-input other-input"
          placeholder="请补充说明你的具体需求..."
          rows="2"
        />

        <!-- 多选 -->
        <div v-else-if="item.type === 'multi_choice'" class="options-grid">
          <button
            v-for="opt in item.options"
            :key="opt"
            class="opt-btn opt-btn--multi"
            :class="{ selected: isSelected(item.id, opt) }"
            @click="toggleMulti(item.id, opt)"
          >
            <span class="opt-check">
              <svg v-if="isSelected(item.id, opt)" width="10" height="10" viewBox="0 0 10 10">
                <polyline points="2,5 4,7 8,3" stroke="currentColor" stroke-width="1.8"
                          stroke-linecap="round" stroke-linejoin="round" fill="none"/>
              </svg>
            </span>
            {{ opt }}
          </button>
        </div>

        <!-- 文本输入 -->
        <textarea
          v-else-if="item.type === 'text'"
          v-model="(answers[item.id] as string)"
          class="text-input"
          :placeholder="item.placeholder || '请输入...'"
          rows="2"
        />
      </div>
    </div>

    <!-- 提交按钮 -->
    <div class="card-footer">
      <button
        class="submit-btn"
        :disabled="!canSubmit || loading"
        @click="handleSubmit"
      >
        <svg v-if="loading" class="spin-icon" width="13" height="13" viewBox="0 0 24 24">
          <circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2.5"
                  stroke-dasharray="28 28" fill="none"/>
        </svg>
        <svg v-else width="13" height="13" viewBox="0 0 24 24" fill="none">
          <path d="M5 12h14M13 6l6 6-6 6" stroke="currentColor" stroke-width="2"
                stroke-linecap="round" stroke-linejoin="round"/>
        </svg>
        确认并继续
      </button>
    </div>
  </div>
</template>

<style scoped>
.clarification-card {
  margin-top: 14px;
  background: #ffffff;
  border: 1.5px solid #e0deff;
  border-radius: 14px;
  overflow: hidden;
  box-shadow: 0 2px 16px rgba(99, 102, 241, 0.10), 0 1px 4px rgba(0,0,0,0.05);
  max-width: 620px;
}

/* ── 头部 ── */
.card-header {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 14px 16px 12px;
  background: linear-gradient(135deg, #f5f3ff 0%, #ede9fe 100%);
  border-bottom: 1px solid #e0deff;
}
.card-icon {
  flex-shrink: 0;
  width: 28px; height: 28px;
  border-radius: 8px;
  background: #6366f1;
  color: #fff;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-top: 1px;
}
.card-title {
  font-size: 13.5px;
  font-weight: 600;
  color: #3730a3;
  line-height: 1.5;
}

/* ── 内容区 ── */
.card-body {
  padding: 16px 16px 4px;
  display: flex;
  flex-direction: column;
  gap: 16px;
}
.item-block {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.item-label {
  font-size: 12.5px;
  font-weight: 600;
  color: #374151;
}

/* ── 选项网格 ── */
.options-grid {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}
.opt-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 6px 13px;
  border: 1.5px solid #d1d5db;
  border-radius: 20px;
  background: #f9fafb;
  font-size: 12.5px;
  font-weight: 500;
  color: #374151;
  cursor: pointer;
  transition: all 0.16s cubic-bezier(0.34, 1.56, 0.64, 1);
  font-family: inherit;
  line-height: 1.4;
}
.opt-btn:hover {
  border-color: #a5b4fc;
  background: #eef2ff;
  color: #4338ca;
  transform: translateY(-1px);
}
.opt-btn.selected {
  border-color: #6366f1;
  background: #ede9fe;
  color: #4338ca;
  font-weight: 600;
}

/* 单选圆点 / 多选方框 */
.opt-radio,
.opt-check {
  width: 14px; height: 14px;
  border-radius: 50%;
  border: 1.5px solid currentColor;
  display: flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  color: #6366f1;
  opacity: 0.7;
}
.opt-btn--multi .opt-check {
  border-radius: 4px;
}
.opt-btn.selected .opt-radio,
.opt-btn.selected .opt-check {
  background: #6366f1;
  border-color: #6366f1;
  color: #fff;
  opacity: 1;
}

/* ── 文本输入 ── */
.text-input {
  width: 100%;
  padding: 8px 12px;
  border: 1.5px solid #d1d5db;
  border-radius: 8px;
  font-size: 13px;
  font-family: inherit;
  color: #374151;
  background: #f9fafb;
  resize: vertical;
  min-height: 62px;
  outline: none;
  transition: border-color 0.15s;
  box-sizing: border-box;
}
.text-input:focus {
  border-color: #6366f1;
  background: #fff;
  box-shadow: 0 0 0 3px rgba(99,102,241,0.1);
}
.text-input::placeholder { color: #9ca3af; }
.other-input {
  margin-top: 4px;
  border-color: #a5b4fc;
  background: #f5f3ff;
}

/* ── 底部 ── */
.card-footer {
  padding: 12px 16px 14px;
  display: flex;
  justify-content: flex-end;
}
.submit-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 8px 20px;
  background: #6366f1;
  color: #fff;
  border: none;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  cursor: pointer;
  transition: all 0.18s;
  box-shadow: 0 2px 8px rgba(99,102,241,0.3);
}
.submit-btn:hover:not(:disabled) {
  background: #4f46e5;
  box-shadow: 0 4px 14px rgba(99,102,241,0.4);
  transform: translateY(-1px);
}
.submit-btn:disabled {
  background: #c7d2fe;
  box-shadow: none;
  cursor: not-allowed;
  transform: none;
}
.spin-icon {
  animation: spin 0.8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
</style>
