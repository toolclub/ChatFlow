<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import type { FormInstance, FormRules } from 'element-plus'
import { Refresh, Plus, View, Hide, Cpu } from '@element-plus/icons-vue'
import {
  fetchLlmProviders, createLlmProvider, updateLlmProvider, deleteLlmProvider,
  activateLlmProvider, deactivateLlmProvider, testLlmProvider, fetchLlmProviderModels,
} from '../api'
import type { LlmProvider, LlmProviderEffective, LlmProviderPayload } from '../api'

/**
 * 模型服务管理面板（挂载于系统管理面板，普通用户不可见）。
 * cc-switch 式全局切换：设为当前后所有用户立即使用该供应商配置。
 */

const props = defineProps<{
  adminKey: string
}>()

// ── 列表状态 ────────────────────────────────────────────────────────────────
const loading = ref(false)
const authExpired = ref(false)   // 403：adminKey 失效
const loadError = ref('')
const providers = ref<LlmProvider[]>([])
const effective = ref<LlmProviderEffective | null>(null)

const activeProvider = computed(() => providers.value.find(p => p.is_active) ?? null)

const effectiveLabel = computed(() => {
  if (!effective.value) return ''
  if (effective.value.source === 'db') {
    return activeProvider.value?.name ? `数据库配置 · ${activeProvider.value.name}` : '数据库配置'
  }
  return '.env 默认配置'
})

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e)
}

function is403(e: unknown): boolean {
  return !!e && typeof e === 'object' && (e as { status?: number }).status === 403
}

async function fetchList() {
  loading.value = true
  authExpired.value = false
  loadError.value = ''
  try {
    const data = await fetchLlmProviders(props.adminKey)
    providers.value = data.providers ?? []
    effective.value = data.effective ?? null
  } catch (e) {
    if (is403(e)) authExpired.value = true
    else loadError.value = errMsg(e)
  } finally {
    loading.value = false
  }
}

onMounted(fetchList)

// ── 行内操作：启用开关 / 测试连接 / 设为当前 / 删除 ─────────────────────────
const switchingId = ref<number | null>(null)
const testingId = ref<number | null>(null)
const activatingId = ref<number | null>(null)

function rowPayload(row: LlmProvider): LlmProviderPayload {
  return {
    name: row.name,
    base_url: row.base_url,
    api_key: row.api_key,
    chat_model: row.chat_model,
    summary_model: row.summary_model,
    router_model: row.router_model,
    vision_model: row.vision_model,
    extra_models: row.extra_models ?? [],
    remark: row.remark,
    enabled: row.enabled,
  }
}

async function toggleEnabled(row: LlmProvider) {
  const target = row.enabled   // v-model 已翻转，失败时回滚
  const prev = !target
  switchingId.value = row.id
  try {
    await updateLlmProvider(row.id, rowPayload(row), props.adminKey)
    ElMessage.success(target ? `已启用 ${row.name}` : `已停用 ${row.name}`)
  } catch (e) {
    row.enabled = prev
    ElMessage.error(errMsg(e))
  } finally {
    switchingId.value = null
  }
}

async function testRow(row: LlmProvider) {
  testingId.value = row.id
  try {
    const r = await testLlmProvider({ id: row.id }, props.adminKey)
    if (r.ok) {
      const text = (r.content || '').trim().replace(/\s+/g, ' ')
      const brief = text.length > 60 ? text.slice(0, 60) + '…' : text
      ElMessage.success(`✓ ${r.latency_ms}ms${brief ? ' · ' + brief : ''}`)
    } else {
      ElMessage.error(r.error || '连接失败')
    }
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    testingId.value = null
  }
}

async function activateRow(row: LlmProvider) {
  try {
    await ElMessageBox.confirm(
      `切换后所有用户立即开始使用 ${row.name}（${row.chat_model}），确定？`,
      '切换全局模型',
      { confirmButtonText: '切换', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return   // 用户点了取消
  }
  activatingId.value = row.id
  try {
    await activateLlmProvider(row.id, props.adminKey)
    ElMessage.success(`已切换到 ${row.name}（${row.chat_model}），所有用户立即生效`)
    await fetchList()
  } catch (e) {
    if (is403(e)) authExpired.value = true
    else ElMessage.error(errMsg(e))
  } finally {
    activatingId.value = null
  }
}

async function deactivateActive() {
  try {
    await ElMessageBox.confirm(
      '恢复后所有用户立即回到 .env 默认模型配置，确定？',
      '恢复默认配置',
      { confirmButtonText: '恢复默认', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await deactivateLlmProvider(props.adminKey)
    ElMessage.success('已恢复 .env 默认配置')
    await fetchList()
  } catch (e) {
    if (is403(e)) authExpired.value = true
    else ElMessage.error(errMsg(e))
  }
}

async function deleteRow(row: LlmProvider) {
  try {
    await ElMessageBox.confirm(
      `确定删除供应商「${row.name}」？该供应商的配置将一并删除，不可恢复。`,
      '删除供应商',
      { confirmButtonText: '删除', cancelButtonText: '取消', type: 'warning' },
    )
  } catch {
    return
  }
  try {
    await deleteLlmProvider(row.id, props.adminKey)
    ElMessage.success('已删除')
    await fetchList()
  } catch (e) {
    if (is403(e)) authExpired.value = true
    else ElMessage.error(errMsg(e))
  }
}

// ── API Key 打码 / 明文切换（仅本行） ───────────────────────────────────────
const revealedKeys = ref<Set<number>>(new Set())

function toggleReveal(id: number) {
  const next = new Set(revealedKeys.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  revealedKeys.value = next
}

function maskKey(key: string): string {
  if (!key) return '—'
  if (key.length <= 8) return '****'
  return `${key.startsWith('sk-') ? 'sk-' : ''}****${key.slice(-4)}`
}

/** 主模型列的副行：仅显示与主模型不同的角色模型 */
function subModels(row: LlmProvider): Array<{ label: string; model: string }> {
  const out: Array<{ label: string; model: string }> = []
  if (row.router_model && row.router_model !== row.chat_model) out.push({ label: '路由', model: row.router_model })
  if (row.summary_model && row.summary_model !== row.chat_model) out.push({ label: '摘要', model: row.summary_model })
  if (row.vision_model && row.vision_model !== row.chat_model) out.push({ label: '视觉', model: row.vision_model })
  return out
}

// ── 新增 / 编辑对话框 ───────────────────────────────────────────────────────
const dialogVisible = ref(false)
const saving = ref(false)
const fetchingModels = ref(false)
const modelOptions = ref<string[]>([])
const formRef = ref<FormInstance>()
const editingId = ref<number | null>(null)      // null = 新增
const editingEnabled = ref(true)                // 编辑时保留表格开关的当前值

const dialogTitle = computed(() => (editingId.value !== null ? '编辑供应商' : '新增供应商'))

const form = reactive({
  name: '',
  base_url: '',
  api_key: '',
  chat_model: '',
  summary_model: '',
  router_model: '',
  vision_model: '',
  extra_models: [] as string[],
  remark: '',
})

const formRules: FormRules = {
  name: [{ required: true, message: '请输入名称', trigger: 'blur' }],
  base_url: [{ required: true, message: '请输入接口地址（OpenAI 兼容 base_url）', trigger: 'blur' }],
  api_key: [{ required: true, message: '请输入 API Key', trigger: 'blur' }],
  chat_model: [{ required: true, message: '请填写主模型', trigger: 'change' }],
}

function openCreate() {
  editingId.value = null
  editingEnabled.value = true
  Object.assign(form, {
    name: '', base_url: '', api_key: '', chat_model: '',
    summary_model: '', router_model: '', vision_model: '',
    extra_models: [] as string[], remark: '',
  })
  modelOptions.value = []
  dialogVisible.value = true
}

function openEdit(row: LlmProvider) {
  editingId.value = row.id
  editingEnabled.value = row.enabled
  Object.assign(form, {
    name: row.name,
    base_url: row.base_url,
    api_key: row.api_key,
    chat_model: row.chat_model,
    summary_model: row.summary_model ?? '',
    router_model: row.router_model ?? '',
    vision_model: row.vision_model ?? '',
    extra_models: [...(row.extra_models ?? [])],
    remark: row.remark ?? '',
  })
  modelOptions.value = []
  dialogVisible.value = true
}

/** 用表单当前的接口地址 + API Key 拉模型列表；成功后主模型输入变为可选列表 */
async function loadModelList() {
  if (!form.base_url || !form.api_key) {
    ElMessage.warning('请先填写接口地址和 API Key')
    return
  }
  fetchingModels.value = true
  try {
    const res = await fetchLlmProviderModels(form.base_url, form.api_key, props.adminKey)
    modelOptions.value = res.models ?? []
    ElMessage.success(`已获取 ${modelOptions.value.length} 个模型`)
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    fetchingModels.value = false
  }
}

async function saveProvider() {
  try {
    await formRef.value?.validate()
  } catch {
    return
  }
  saving.value = true
  try {
    const payload: LlmProviderPayload = {
      name: form.name.trim(),
      base_url: form.base_url.trim(),
      api_key: form.api_key.trim(),
      chat_model: form.chat_model.trim(),
      summary_model: form.summary_model.trim() || null,
      router_model: form.router_model.trim() || null,
      vision_model: form.vision_model.trim() || null,
      extra_models: form.extra_models,
      remark: form.remark.trim() || null,
      enabled: editingId.value !== null ? editingEnabled.value : true,
    }
    if (editingId.value !== null) {
      await updateLlmProvider(editingId.value, payload, props.adminKey)
    } else {
      await createLlmProvider(payload, props.adminKey)
    }
    ElMessage.success('已保存')
    dialogVisible.value = false
    await fetchList()
  } catch (e) {
    ElMessage.error(errMsg(e))
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <div class="llm-provider-section">
    <div class="section-header">
      <div class="section-title">
        <el-icon><Cpu /></el-icon>
        <span>模型服务</span>
      </div>
      <div class="section-actions">
        <el-button :icon="Refresh" :loading="loading" @click="fetchList">刷新</el-button>
        <el-button type="primary" :icon="Plus" @click="openCreate">新增供应商</el-button>
      </div>
    </div>

    <!-- adminKey 失效：空态而非白屏 -->
    <div v-if="authExpired" class="panel-empty">
      <el-empty description="管理员验证已失效，请退出后重新验证" />
    </div>
    <div v-else-if="loadError" class="panel-empty">
      <el-empty :description="`加载失败：${loadError}`" />
      <el-button :icon="Refresh" @click="fetchList">重试</el-button>
    </div>

    <div v-else v-loading="loading" class="panel-body">
      <!-- 当前生效配置横幅 -->
      <div v-if="effective" class="effective-banner">
        <div class="effective-main">
          <span class="effective-badge">{{ effectiveLabel }}</span>
          <span class="effective-url">{{ effective.base_url }}</span>
          <span class="effective-model">主模型 <strong>{{ effective.chat_model }}</strong></span>
          <el-button
            v-if="effective.source === 'db'"
            size="small" plain
            @click="deactivateActive"
          >恢复 .env 默认</el-button>
        </div>
        <div v-if="effective.models?.length" class="effective-models">
          <span class="models-label">用户可见模型：</span>
          <el-tag v-for="m in effective.models" :key="m" size="small" class="model-tag">{{ m }}</el-tag>
        </div>
      </div>

      <!-- 供应商列表 -->
      <el-table
        :data="providers"
        border
        size="small"
        row-key="id"
        class="admin-table"
        empty-text="暂无供应商，点击右上角「新增供应商」添加"
      >
        <el-table-column label="启用" width="70" align="center">
          <template #default="{ row }">
            <el-switch v-model="row.enabled" :loading="switchingId === row.id" @change="toggleEnabled(row)" />
          </template>
        </el-table-column>
        <el-table-column prop="name" label="名称" width="130" show-overflow-tooltip />
        <el-table-column label="主模型" min-width="190">
          <template #default="{ row }">
            <div class="model-cell">
              <span class="model-main">{{ row.chat_model }}</span>
              <span v-for="s in subModels(row)" :key="s.label" class="model-sub">{{ s.label }}: {{ s.model }}</span>
            </div>
          </template>
        </el-table-column>
        <el-table-column prop="base_url" label="接口地址" min-width="200" show-overflow-tooltip />
        <el-table-column label="API Key" width="170">
          <template #default="{ row }">
            <div class="key-cell">
              <span class="key-text">{{ revealedKeys.has(row.id) ? row.api_key : maskKey(row.api_key) }}</span>
              <el-button link size="small" @click="toggleReveal(row.id)">
                <el-icon><View v-if="revealedKeys.has(row.id)" /><Hide v-else /></el-icon>
              </el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="备注" min-width="100" show-overflow-tooltip>
          <template #default="{ row }">{{ row.remark || '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="96" align="center">
          <template #default="{ row }">
            <el-tag v-if="row.is_active" type="success" size="small">当前使用</el-tag>
            <span v-else class="status-idle">—</span>
          </template>
        </el-table-column>
        <el-table-column label="操作" width="230" fixed="right">
          <template #default="{ row }">
            <el-button link type="primary" size="small" :loading="testingId === row.id" @click="testRow(row)">测试连接</el-button>
            <el-button v-if="!row.is_active" link type="primary" size="small" :loading="activatingId === row.id" @click="activateRow(row)">设为当前</el-button>
            <el-button link type="primary" size="small" @click="openEdit(row)">编辑</el-button>
            <el-button link type="danger" size="small" @click="deleteRow(row)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <!-- 新增 / 编辑对话框 -->
    <el-dialog v-model="dialogVisible" :title="dialogTitle" width="560px" top="8vh">
      <el-form ref="formRef" :model="form" :rules="formRules" label-width="110px">
        <el-form-item label="名称" prop="name">
          <el-input v-model="form.name" placeholder="如 智谱官方 / 自建中转" maxlength="50" />
        </el-form-item>
        <el-form-item label="接口地址" prop="base_url">
          <el-input v-model="form.base_url" placeholder="https://ai-develop.seeyoncloud.com/v1" />
        </el-form-item>
        <el-form-item label="API Key" prop="api_key">
          <el-input v-model="form.api_key" type="password" show-password placeholder="sk-..." />
        </el-form-item>
        <el-form-item label="主模型" prop="chat_model">
          <div class="model-input-row">
            <!-- 拉取列表前是自由输入；拉取后变为可选列表（仍可手输任意模型名） -->
            <el-select
              v-if="modelOptions.length"
              v-model="form.chat_model"
              filterable allow-create default-first-option
              placeholder="选择或输入模型名"
              class="model-select"
            >
              <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
            </el-select>
            <el-input v-else v-model="form.chat_model" placeholder="模型名，如 glm-4.5-air" />
            <el-button
              :loading="fetchingModels"
              title="用当前接口地址 + API Key 拉取模型列表"
              @click="loadModelList"
            >获取模型列表</el-button>
          </div>
        </el-form-item>

        <div class="advanced-header">高级（可选）</div>
        <el-form-item label="摘要模型">
          <el-input v-model="form.summary_model" placeholder="留空 = 同主模型" />
        </el-form-item>
        <el-form-item label="路由模型">
          <el-input v-model="form.router_model" placeholder="留空 = 同主模型" />
        </el-form-item>
        <el-form-item label="视觉模型">
          <el-input v-model="form.vision_model" placeholder="留空 = 沿用 .env 视觉配置" />
        </el-form-item>
        <el-form-item label="额外模型">
          <el-select
            v-model="form.extra_models"
            multiple filterable allow-create default-first-option
            placeholder="对话页可切换的模型，可手输；点「获取模型列表」后可勾选"
            class="full-width"
          >
            <el-option v-for="m in modelOptions" :key="m" :label="m" :value="m" />
          </el-select>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="form.remark" type="textarea" :rows="2" placeholder="如：公司中转，限额 10w tokens/天" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="dialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="saving" @click="saveProvider">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.llm-provider-section {
  background: var(--cf-card);
  border-radius: var(--cf-radius-lg);
  padding: 24px;
  box-shadow: var(--cf-shadow-sm);
  border: 1px solid var(--cf-border-soft);
}

.section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}

.section-title {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 700;
  color: var(--cf-text-1);
}

.section-title .el-icon {
  font-size: 20px;
  color: var(--cf-bili-blue);
}

.section-actions {
  display: flex;
  gap: 8px;
}

.panel-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 12px 0 20px;
}

/* ── 当前生效横幅 ── */
.effective-banner {
  border: 1px solid var(--cf-border-glow);
  background: var(--cf-active);
  border-radius: var(--cf-radius-md);
  padding: 12px 16px;
  margin-bottom: 16px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.effective-main {
  display: flex;
  align-items: center;
  gap: 14px;
  flex-wrap: wrap;
}

.effective-badge {
  font-size: 13px;
  font-weight: 700;
  color: var(--cf-bili-blue);
  background: rgba(0, 174, 236, 0.12);
  padding: 3px 10px;
  border-radius: 6px;
  white-space: nowrap;
}

.effective-url {
  font-family: 'Inter', system-ui, monospace;
  font-size: 12px;
  color: var(--cf-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 340px;
}

.effective-model {
  font-size: 13px;
  color: var(--cf-text-2);
  white-space: nowrap;
}

.effective-model strong {
  color: var(--cf-text-1);
  font-weight: 700;
}

.effective-models {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}

.models-label {
  font-size: 12px;
  color: var(--cf-text-3);
}

.model-tag {
  font-family: 'Inter', system-ui, monospace;
}

/* ── 供应商表格（暗色跟随 --cf-* 变量，同 SysDashboard.admin-table） ── */
.admin-table {
  border-radius: var(--cf-radius-md);
  overflow: hidden;
  --el-table-border-color: var(--cf-border-soft);
  --el-table-header-bg-color: var(--cf-bg);
  --el-table-header-text-color: var(--cf-text-1);
  --el-table-text-color: var(--cf-text-2);
  --el-table-bg-color: transparent;
  --el-table-tr-bg-color: transparent;
}

:deep(.el-table__header-wrapper th) {
  font-weight: 700;
}

.model-cell {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.model-main {
  font-family: 'Inter', system-ui, monospace;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--cf-text-1);
}

.model-sub {
  font-size: 11.5px;
  color: var(--cf-text-3);
}

.key-cell {
  display: flex;
  align-items: center;
  gap: 4px;
}

.key-text {
  font-family: 'Inter', system-ui, monospace;
  font-size: 12px;
  color: var(--cf-text-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 118px;
}

.status-idle {
  color: var(--cf-text-4);
}

/* ── 新增 / 编辑对话框表单 ── */
.advanced-header {
  font-size: 13px;
  font-weight: 700;
  color: var(--cf-text-2);
  border-top: 1px dashed var(--cf-border);
  padding-top: 14px;
  margin: 4px 0 16px;
}

.model-input-row {
  display: flex;
  gap: 8px;
  width: 100%;
}

.model-input-row .model-select,
.model-input-row .el-input {
  flex: 1;
  min-width: 0;
}

.full-width {
  width: 100%;
}
</style>
