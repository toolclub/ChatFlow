<script setup lang="ts">
import { ref, reactive } from 'vue'
import type { PlanStep } from '../types'
import { Plus, Delete, Top, Bottom, Close } from '@element-plus/icons-vue'

const props = defineProps<{
  plan: PlanStep[]
  userMessage: string
}>()

const emit = defineEmits<{
  apply: [plan: PlanStep[]]
  close: []
}>()

// 深拷贝计划以供编辑
const editablePlan = reactive<PlanStep[]>(
  props.plan.map(step => ({ ...step, status: 'pending' as const, result: '' }))
)

function addStep(afterIndex: number) {
  editablePlan.splice(afterIndex + 1, 0, {
    id: `new-${Date.now()}`,
    title: '',
    description: '',
    status: 'pending',
    result: '',
  })
}

function removeStep(index: number) {
  if (editablePlan.length <= 1) return
  editablePlan.splice(index, 1)
}

function moveUp(index: number) {
  if (index === 0) return
  const [step] = editablePlan.splice(index, 1)
  editablePlan.splice(index - 1, 0, step)
}

function moveDown(index: number) {
  if (index === editablePlan.length - 1) return
  const [step] = editablePlan.splice(index, 1)
  editablePlan.splice(index + 1, 0, step)
}

function apply() {
  // 过滤掉空标题的步骤
  const validSteps = editablePlan.filter(s => s.title.trim())
  if (validSteps.length === 0) return
  // 重新分配 ID
  const renumbered = validSteps.map((step, i) => ({ ...step, id: String(i + 1) }))
  emit('apply', renumbered)
}

function close() {
  emit('close')
}
</script>

<template>
  <teleport to="body">
    <el-dialog
      :model-value="true"
      :close-on-click-modal="false"
      :show-close="false"
      width="580px"
      align-center
      destroy-on-close
      class="intervention-dialog"
      @close="close"
    >
      <template #header>
        <!-- 标题栏 -->
        <div class="modal-header">
          <div class="modal-title-group">
            <span class="modal-icon">🧠</span>
            <div>
              <div class="modal-title">修改执行计划</div>
              <div class="modal-subtitle">你可以添加、删除或调整步骤，Agent 将按修改后的计划执行</div>
            </div>
          </div>
          <el-button :icon="Close" circle text size="small" @click="close" />
        </div>
      </template>

      <!-- 目标展示 -->
      <el-alert
        type="info"
        :closable="false"
        class="goal-alert"
      >
        <template #title>
          <div class="goal-alert-content">
            <el-tag size="small" type="primary" effect="plain" round class="goal-chip">目标</el-tag>
            <span class="goal-text">{{ userMessage.slice(0, 120) }}{{ userMessage.length > 120 ? '...' : '' }}</span>
          </div>
        </template>
      </el-alert>

      <!-- 计划编辑列表 -->
      <el-scrollbar class="steps-scrollbar" max-height="50vh">
        <div class="steps-list">
          <transition-group name="step-list">
            <el-card
              v-for="(step, i) in editablePlan"
              :key="step.id"
              shadow="hover"
              class="step-card"
              :body-style="{ padding: '12px' }"
            >
              <div class="step-card-inner">
                <!-- 步骤序号 -->
                <div class="step-index">{{ i + 1 }}</div>

                <!-- 步骤内容 -->
                <div class="step-content">
                  <el-form label-position="top" :inline="false" size="small" class="step-form">
                    <el-form-item class="step-form-item">
                      <el-input
                        v-model="step.title"
                        placeholder="步骤标题（简短）"
                        class="step-title-input"
                      />
                    </el-form-item>
                    <el-form-item class="step-form-item">
                      <el-input
                        v-model="step.description"
                        placeholder="步骤描述（可选）"
                        type="textarea"
                        :rows="2"
                        resize="none"
                        class="step-desc-input"
                      />
                    </el-form-item>
                  </el-form>
                </div>

                <!-- 操作按钮 -->
                <el-button-group class="step-actions" size="small">
                  <el-tooltip content="上移" placement="top" :show-after="300">
                    <el-button
                      :icon="Top" text
                      :disabled="i === 0"
                      @click="moveUp(i)"
                    />
                  </el-tooltip>
                  <el-tooltip content="下移" placement="top" :show-after="300">
                    <el-button
                      :icon="Bottom" text
                      :disabled="i === editablePlan.length - 1"
                      @click="moveDown(i)"
                    />
                  </el-tooltip>
                  <el-popconfirm
                    title="确定删除此步骤？"
                    confirm-button-text="删除"
                    cancel-button-text="取消"
                    confirm-button-type="danger"
                    :disabled="editablePlan.length <= 1"
                    @confirm="removeStep(i)"
                  >
                    <template #reference>
                      <el-button
                        :icon="Delete" text type="danger"
                        :disabled="editablePlan.length <= 1"
                      />
                    </template>
                  </el-popconfirm>
                </el-button-group>
              </div>

              <!-- 在此之后插入按钮 -->
              <div class="insert-btn-wrap">
                <el-tooltip content="在此后插入步骤" placement="bottom" :show-after="300">
                  <el-button
                    class="insert-btn"
                    :icon="Plus"
                    circle
                    size="small"
                    @click="addStep(i)"
                  />
                </el-tooltip>
              </div>
            </el-card>
          </transition-group>

          <!-- 添加最后一步 -->
          <el-button
            class="add-step-btn"
            :icon="Plus"
            @click="addStep(editablePlan.length - 1)"
          >
            添加步骤
          </el-button>
        </div>
      </el-scrollbar>

      <template #footer>
        <!-- 底部操作 -->
        <div class="modal-footer">
          <el-tag type="info" effect="light" size="small" round>
            共 {{ editablePlan.filter(s => s.title.trim()).length }} 个步骤
          </el-tag>
          <div class="footer-actions">
            <el-button size="default" @click="close">取消</el-button>
            <el-button size="default" type="primary" @click="apply">
              应用修改并执行
            </el-button>
          </div>
        </div>
      </template>
    </el-dialog>
  </teleport>
</template>

<style scoped>
/* Dialog customization */
:deep(.intervention-dialog) {
  border-radius: 20px !important;
  overflow: hidden;
}
:deep(.intervention-dialog .el-dialog__header) {
  padding: 0;
  margin: 0;
}
:deep(.intervention-dialog .el-dialog__body) {
  padding: 0 20px;
}
:deep(.intervention-dialog .el-dialog__footer) {
  padding: 0;
}

/* 标题栏 */
.modal-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  padding: 20px 20px 16px;
  border-bottom: 1px solid #f0f0f0;
}
.modal-title-group {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.modal-icon { font-size: 22px; }
.modal-title {
  font-size: 16px;
  font-weight: 700;
  color: #111827;
  line-height: 1.2;
}
.modal-subtitle {
  font-size: 12px;
  color: #6b7280;
  margin-top: 2px;
}

/* 目标 Alert */
.goal-alert {
  margin: 16px 0 12px;
  border-radius: 12px !important;
  background: rgba(0, 174, 236, 0.04) !important;
  border: 1px solid rgba(0, 174, 236, 0.12) !important;
}
:deep(.goal-alert .el-alert__icon) {
  display: none;
}
:deep(.goal-alert .el-alert__content) {
  padding: 0;
}
.goal-alert-content {
  display: flex;
  align-items: flex-start;
  gap: 8px;
}
.goal-chip {
  flex-shrink: 0;
  margin-top: 1px;
}
.goal-text {
  font-size: 12.5px;
  color: #374151;
  line-height: 1.5;
  font-weight: 400;
}

/* 步骤列表滚动区 */
.steps-scrollbar {
  margin: 0 -20px;
  padding: 0 20px;
}

.steps-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 4px 0 12px;
}

/* 步骤卡片 */
.step-card {
  border-radius: 12px !important;
  border: 1.5px solid #e5e7eb !important;
  position: relative;
  transition: border-color 0.25s cubic-bezier(0.34,1.56,0.64,1),
              box-shadow 0.25s cubic-bezier(0.34,1.56,0.64,1);
}
.step-card:hover {
  border-color: #00AEEC !important;
}

.step-card-inner {
  display: grid;
  grid-template-columns: 28px 1fr auto;
  gap: 6px 10px;
}

.step-index {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: linear-gradient(135deg, #E3F6FD 0%, #f3f4f6 100%);
  border: 1.5px solid #B8E6F9;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #00AEEC;
  flex-shrink: 0;
  margin-top: 2px;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1);
}

.step-content {
  display: flex;
  flex-direction: column;
  gap: 0;
}

.step-form {
  margin: 0;
}
.step-form-item {
  margin-bottom: 4px !important;
}
:deep(.step-form-item:last-child) {
  margin-bottom: 0 !important;
}
:deep(.step-form-item .el-form-item__content) {
  line-height: normal;
}
:deep(.step-title-input .el-input__wrapper) {
  font-weight: 600;
  font-size: 13px;
}
:deep(.step-desc-input .el-textarea__inner) {
  font-size: 12px;
  color: #6b7280;
}

.step-actions {
  display: flex;
  flex-direction: column;
  gap: 0;
  align-items: center;
}

/* 插入按钮（步骤之间） */
.insert-btn-wrap {
  display: flex;
  justify-content: center;
  height: 0;
  overflow: visible;
  position: relative;
  z-index: 1;
  margin-top: 4px;
}
.insert-btn {
  width: 22px !important;
  height: 22px !important;
  border: 1.5px dashed #B8E6F9 !important;
  background: #fff !important;
  color: #00AEEC !important;
  font-size: 12px;
  transform: translateY(5px);
  opacity: 0;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1) !important;
}
.step-card:hover .insert-btn { opacity: 1; }
.insert-btn:hover {
  background: #E3F6FD !important;
  border-color: #00AEEC !important;
  transform: translateY(5px) scale(1.1);
}

/* 添加步骤按钮 */
.add-step-btn {
  width: 100%;
  border: 1.5px dashed #d1d5db !important;
  border-radius: 12px !important;
  color: #9ca3af !important;
  font-size: 13px;
  transition: all 0.25s cubic-bezier(0.34,1.56,0.64,1) !important;
  margin-top: 4px;
  height: 40px;
  background: transparent !important;
}
.add-step-btn:hover {
  border-color: #00AEEC !important;
  color: #00AEEC !important;
  background: rgba(0, 174, 236, 0.04) !important;
}

/* 底部操作 */
.modal-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 20px;
  border-top: 1px solid #f0f0f0;
  background: #fafafa;
}
.footer-actions {
  display: flex;
  gap: 8px;
}

/* 列表动画 */
.step-list-enter-active {
  transition: all 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
.step-list-leave-active {
  transition: all 0.2s ease;
}
.step-list-enter-from {
  opacity: 0;
  transform: translateY(-12px) scale(0.95);
}
.step-list-leave-to {
  opacity: 0;
  transform: translateX(-20px) scale(0.95);
}
.step-list-move {
  transition: transform 0.3s cubic-bezier(0.34,1.56,0.64,1);
}
</style>
