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
    <div class="intervention-overlay" @click.self="close">
      <div class="intervention-modal">
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

        <!-- 目标展示 -->
        <div class="modal-goal">
          <span class="goal-chip">目标</span>
          <span class="goal-text">{{ userMessage.slice(0, 120) }}{{ userMessage.length > 120 ? '…' : '' }}</span>
        </div>

        <!-- 计划编辑列表 -->
        <div class="steps-list">
          <transition-group name="step-list">
            <div
              v-for="(step, i) in editablePlan"
              :key="step.id"
              class="step-item"
            >
              <!-- 步骤序号 -->
              <div class="step-index">{{ i + 1 }}</div>

              <!-- 步骤内容 -->
              <div class="step-content">
                <el-input
                  v-model="step.title"
                  placeholder="步骤标题（简短）"
                  size="small"
                  class="step-title-input"
                />
                <el-input
                  v-model="step.description"
                  placeholder="步骤描述（可选）"
                  size="small"
                  type="textarea"
                  :rows="2"
                  resize="none"
                  class="step-desc-input"
                />
              </div>

              <!-- 操作按钮 -->
              <div class="step-actions">
                <el-button
                  :icon="Top" circle text size="small"
                  :disabled="i === 0"
                  title="上移"
                  @click="moveUp(i)"
                />
                <el-button
                  :icon="Bottom" circle text size="small"
                  :disabled="i === editablePlan.length - 1"
                  title="下移"
                  @click="moveDown(i)"
                />
                <el-button
                  :icon="Delete" circle text size="small" type="danger"
                  :disabled="editablePlan.length <= 1"
                  title="删除"
                  @click="removeStep(i)"
                />
              </div>

              <!-- 在此之后插入按钮 -->
              <div class="insert-btn-wrap">
                <button class="insert-btn" @click="addStep(i)" title="在此后插入步骤">
                  <el-icon><Plus /></el-icon>
                </button>
              </div>
            </div>
          </transition-group>

          <!-- 添加最后一步 -->
          <button class="add-step-btn" @click="addStep(editablePlan.length - 1)">
            <el-icon><Plus /></el-icon>
            添加步骤
          </button>
        </div>

        <!-- 底部操作 -->
        <div class="modal-footer">
          <el-tag type="info" effect="light" size="small">
            共 {{ editablePlan.filter(s => s.title.trim()).length }} 个步骤
          </el-tag>
          <div class="footer-actions">
            <el-button size="default" @click="close">取消</el-button>
            <el-button size="default" type="primary" @click="apply">
              应用修改并执行
            </el-button>
          </div>
        </div>
      </div>
    </div>
  </teleport>
</template>

<style scoped>
.intervention-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.45);
  backdrop-filter: blur(4px);
  z-index: 2000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
}

.intervention-modal {
  background: #fff;
  border-radius: 20px;
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.15);
  width: 100%;
  max-width: 560px;
  max-height: 80vh;
  display: flex;
  flex-direction: column;
  overflow: hidden;
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

/* 目标 — Bilibili 风格 */
.modal-goal {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 10px 20px;
  background: rgba(0, 174, 236, 0.03);
  border-bottom: 1px solid rgba(0, 174, 236, 0.08);
}
.goal-chip {
  font-size: 11px;
  font-weight: 600;
  color: #00AEEC;
  background: rgba(0, 174, 236, 0.08);
  padding: 1px 7px;
  border-radius: 20px;
  flex-shrink: 0;
  margin-top: 1px;
}
.goal-text {
  font-size: 12.5px;
  color: #374151;
  line-height: 1.5;
}

/* 步骤列表 */
.steps-list {
  flex: 1;
  overflow-y: auto;
  padding: 12px 16px;
  display: flex;
  flex-direction: column;
  gap: 0;
}

.step-item {
  display: grid;
  grid-template-columns: 28px 1fr auto;
  grid-template-rows: auto auto;
  gap: 6px 10px;
  padding: 10px 10px;
  border: 1.5px solid #e5e7eb;
  border-radius: 10px;
  margin-bottom: 4px;
  background: #fafafa;
  position: relative;
  transition: border-color 0.2s;
}
.step-item:hover { border-color: #00AEEC; }

.step-index {
  width: 28px;
  height: 28px;
  border-radius: 50%;
  background: #f3f4f6;
  border: 1.5px solid #d1d5db;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #374151;
  flex-shrink: 0;
  margin-top: 2px;
}

.step-content {
  display: flex;
  flex-direction: column;
  gap: 5px;
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
  gap: 2px;
  align-items: center;
}

/* 插入按钮（步骤之间） */
.insert-btn-wrap {
  grid-column: 1 / -1;
  display: flex;
  justify-content: center;
  height: 0;
  overflow: visible;
  position: relative;
  z-index: 1;
}
.insert-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  border: 1.5px dashed #B8E6F9;
  background: #fff;
  color: #00AEEC;
  cursor: pointer;
  font-size: 12px;
  transition: all 0.2s;
  transform: translateY(5px);
  opacity: 0;
}
.step-item:hover .insert-btn { opacity: 1; }
.insert-btn:hover {
  background: #E3F6FD;
  border-color: #00AEEC;
}

/* 添加步骤按钮 */
.add-step-btn {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  padding: 8px;
  border: 1.5px dashed #d1d5db;
  border-radius: 10px;
  background: transparent;
  color: #9ca3af;
  font-size: 13px;
  cursor: pointer;
  transition: all 0.2s;
  margin-top: 4px;
}
.add-step-btn:hover {
  border-color: #00AEEC;
  color: #00AEEC;
  background: #E3F6FD;
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
.step-list-enter-active,
.step-list-leave-active { transition: all 0.2s ease; }
.step-list-enter-from,
.step-list-leave-to { opacity: 0; transform: translateY(-8px); }
</style>
