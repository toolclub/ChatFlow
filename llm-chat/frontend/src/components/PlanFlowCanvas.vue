<script setup lang="ts">
import { ref, computed, watch, onMounted, nextTick, reactive } from 'vue'
import type { PlanStep } from '../types'

// ── Props / Emits ─────────────────────────────────────────────────────────────
const props = defineProps<{
  plan: PlanStep[]
  loading: boolean
}>()

const emit = defineEmits<{
  reorder: [plan: PlanStep[]]
  editNode: [step: PlanStep, index: number]
  addNode: [afterIndex: number]
  deleteNode: [index: number]
}>()

// ── 节点尺寸常量 ──────────────────────────────────────────────────────────────
const NODE_W = 200
const NODE_H = 64
const NODE_GAP_X = 40   // 水平间距（布局用）
const NODE_GAP_Y = 50   // 垂直间距

// ── 内部节点状态 ──────────────────────────────────────────────────────────────
interface FlowNode {
  id: string
  x: number
  y: number
  planIdx: number  // 对应 props.plan 的索引
}

const nodes = reactive<FlowNode[]>([])
const svgW = ref(600)
const svgH = ref(400)

// ── 拖拽状态 ──────────────────────────────────────────────────────────────────
const draggingId = ref<string | null>(null)

// ── 右键菜单 ──────────────────────────────────────────────────────────────────
const ctxMenu = ref({ visible: false, x: 0, y: 0, canvasX: 0, canvasY: 0 })

// ── 弹出面板（点击节点详情） ──────────────────────────────────────────────────
const detailPanel = ref<{ visible: boolean; node: FlowNode | null }>({
  visible: false,
  node: null,
})

// ── Canvas ref ────────────────────────────────────────────────────────────────
const graphEl = ref<HTMLDivElement>()

// ── 状态配置 ──────────────────────────────────────────────────────────────────
function sBorder(status: PlanStep['status']) {
  return { pending: '#909399', running: '#6B9EFF', done: '#67C23A', failed: '#F56C6C' }[status] ?? '#909399'
}
function sBorderWidth(status: PlanStep['status']) {
  return status === 'running' ? '2px' : '1.5px'
}
function sBg(status: PlanStep['status']) {
  return { pending: '#fff', running: '#f0f5ff', done: '#f6ffed', failed: '#fff1f0' }[status] ?? '#fff'
}
function sEmoji(status: PlanStep['status']) {
  return { pending: '⏳', running: '🚀', done: '✅', failed: '❌' }[status] ?? '⏳'
}
function sLabel(status: PlanStep['status']) {
  return { pending: '待执行', running: '执行中', done: '已完成', failed: '失败' }[status] ?? '待执行'
}
function sTextColor(status: PlanStep['status']) {
  return { pending: '#909399', running: '#6B9EFF', done: '#67C23A', failed: '#F56C6C' }[status] ?? '#909399'
}

// ── 初始布局（垂直居中排列） ──────────────────────────────────────────────────
function autoLayout() {
  const containerW = graphEl.value?.clientWidth || 300
  const x = Math.max(16, (containerW - NODE_W) / 2)

  nodes.length = 0
  props.plan.forEach((step, i) => {
    nodes.push({
      id: step.id,
      x,
      y: i * (NODE_H + NODE_GAP_Y) + 16,
      planIdx: i,
    })
  })
  updateSvgSize()
}

function updateSvgSize() {
  if (nodes.length === 0) { svgW.value = 300; svgH.value = 200; return }
  svgW.value = Math.max(graphEl.value?.clientWidth || 300, Math.max(...nodes.map(n => n.x + NODE_W + 24)))
  svgH.value = Math.max(200, Math.max(...nodes.map(n => n.y + NODE_H + 24)))
}

// ── 边（SVG 贝塞尔曲线） ──────────────────────────────────────────────────────
const edges = computed(() => {
  const result = []
  for (let i = 0; i < nodes.length - 1; i++) {
    const src = nodes[i]
    const tgt = nodes[i + 1]
    if (!src || !tgt) continue

    const sx = src.x + NODE_W / 2
    const sy = src.y + NODE_H
    const tx = tgt.x + NODE_W / 2
    const ty = tgt.y

    // 三次贝塞尔 S 型曲线
    const dy = ty - sy
    const cp1y = sy + dy * 0.5
    const path = `M ${sx} ${sy} C ${sx} ${cp1y}, ${tx} ${cp1y}, ${tx} ${ty}`

    const srcStatus = props.plan[src.planIdx]?.status ?? 'pending'
    const isAnimated = srcStatus === 'running' || props.plan[tgt.planIdx]?.status === 'running'

    result.push({ id: `e${i}`, path, animated: isAnimated, srcStatus, delay: i * 0.2 })
  }
  return result
})

// ── 拖拽节点 ──────────────────────────────────────────────────────────────────
function onNodeMousedown(e: MouseEvent, node: FlowNode) {
  if (props.loading) return
  e.stopPropagation()
  draggingId.value = node.id
  const startX = e.clientX - node.x
  const startY = e.clientY - node.y

  function onMove(ev: MouseEvent) {
    if (draggingId.value !== node.id) return
    node.x = ev.clientX - startX
    node.y = ev.clientY - startY
    updateSvgSize()
  }
  function onUp() {
    draggingId.value = null
    document.removeEventListener('mousemove', onMove)
    document.removeEventListener('mouseup', onUp)

    // 释放时按 Y 排序并 emit
    const sorted = [...nodes].sort((a, b) => a.y - b.y)
    const newPlan = sorted.map(n => props.plan[n.planIdx]).filter(Boolean) as PlanStep[]
    if (newPlan.length === props.plan.length) emit('reorder', newPlan)
  }
  document.addEventListener('mousemove', onMove)
  document.addEventListener('mouseup', onUp)
}

// ── 点击节点 ──────────────────────────────────────────────────────────────────
function onNodeClick(node: FlowNode) {
  if (draggingId.value) return
  detailPanel.value = { visible: true, node }
}

// ── 双击空白处 → 添加节点 ─────────────────────────────────────────────────────
function onCanvasDblclick(e: MouseEvent) {
  if (e.target !== graphEl.value && !(e.target as HTMLElement).classList.contains('edges-svg')) return
  const rect = graphEl.value!.getBoundingClientRect()
  const cx = e.clientX - rect.left
  const cy = e.clientY - rect.top
  emit('addNode', props.plan.length - 1)
  spawnBounce(cx, cy)
}

// ── 右键菜单 ──────────────────────────────────────────────────────────────────
function onContextmenu(e: MouseEvent) {
  e.preventDefault()
  const rect = graphEl.value!.getBoundingClientRect()
  ctxMenu.value = { visible: true, x: e.clientX - rect.left, y: e.clientY - rect.top, canvasX: e.clientX, canvasY: e.clientY }
}
function closeCtxMenu() { ctxMenu.value.visible = false }
function ctxAddNode() {
  emit('addNode', props.plan.length - 1)
  spawnBounce(ctxMenu.value.x, ctxMenu.value.y)
  closeCtxMenu()
}

// ── 弹性反馈动画 ──────────────────────────────────────────────────────────────
function spawnBounce(x: number, y: number) {
  if (!graphEl.value) return
  const el = document.createElement('div')
  el.className = 'bounce-feedback'
  el.style.left = x + 'px'
  el.style.top = y + 'px'
  el.textContent = '✨'
  graphEl.value.appendChild(el)
  setTimeout(() => el.remove(), 700)
}

// ── 适应视图 ──────────────────────────────────────────────────────────────────
function fitView() {
  autoLayout()
}

// ── Watch plan 变化重新布局 ───────────────────────────────────────────────────
watch(
  () => props.plan.length,
  async () => {
    await nextTick()
    autoLayout()
  }
)

watch(
  () => props.plan.map(s => s.status).join(','),
  () => { /* status变化不重排，只重绘edges（computed自动） */ }
)

onMounted(async () => {
  await nextTick()
  autoLayout()
})

// ── Detail panel helpers ──────────────────────────────────────────────────────
const detailStep = computed(() => {
  const node = detailPanel.value.node
  if (!node) return null
  return props.plan[node.planIdx] ?? null
})
const detailIdx = computed(() => detailPanel.value.node?.planIdx ?? -1)
</script>

<template>
  <div class="flow-graph" @click="closeCtxMenu">

    <!-- ── 工具栏 ── -->
    <div class="toolbar">
      <div class="toolbar-left">
        <span class="toolbar-title">执行计划</span>
        <span class="node-count">{{ plan.length }} 个节点</span>
      </div>
      <div class="toolbar-actions">
        <button class="toolbar-btn" title="适应视图" @click.stop="fitView">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round">
            <path d="M3 9V5a2 2 0 012-2h4M3 15v4a2 2 0 002 2h4M21 9V5a2 2 0 00-2-2h-4M21 15v4a2 2 0 01-2 2h-4"/>
          </svg>
          <span>适应视图</span>
        </button>
      </div>
    </div>

    <!-- ── 画布 ── -->
    <div
      class="graph-container"
      ref="graphEl"
      @dblclick="onCanvasDblclick"
      @contextmenu="onContextmenu"
    >
      <!-- SVG 边层 -->
      <svg
        class="edges-svg"
        :width="svgW"
        :height="svgH"
        style="position:absolute;top:0;left:0;pointer-events:none;overflow:visible;"
      >
        <defs>
          <marker id="arrowhead" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto">
            <polygon points="0 0, 10 3.5, 0 7" fill="#6B9EFF" />
          </marker>
        </defs>
        <path
          v-for="(edge, vi) in edges"
          :key="edge.id"
          :d="edge.path"
          :class="['edge-path', { animated: edge.animated }]"
          :style="{ animationDelay: vi * 0.2 + 's' }"
          marker-end="url(#arrowhead)"
        />
      </svg>

      <!-- 节点层 -->
      <div
        v-for="node in nodes"
        :key="node.id"
        class="workflow-node"
        :class="[plan[node.planIdx]?.status ?? 'pending', { dragging: draggingId === node.id }]"
        :style="{
          left: node.x + 'px',
          top: node.y + 'px',
          width: NODE_W + 'px',
          height: NODE_H + 'px',
          background: sBg(plan[node.planIdx]?.status ?? 'pending'),
          borderColor: sBorder(plan[node.planIdx]?.status ?? 'pending'),
          borderWidth: sBorderWidth(plan[node.planIdx]?.status ?? 'pending'),
        }"
        @mousedown="onNodeMousedown($event, node)"
        @click.stop="onNodeClick(node)"
      >
        <!-- 左侧色条 -->
        <div class="node-accent" :style="{ background: sBorder(plan[node.planIdx]?.status ?? 'pending') }"></div>

        <!-- 节点内容 -->
        <div class="node-content">
          <div class="node-main">
            <div class="node-header-row">
              <span class="node-num" :style="{ color: sTextColor(plan[node.planIdx]?.status ?? 'pending') }">
                {{ node.planIdx + 1 }}
              </span>
              <span class="node-label" :title="plan[node.planIdx]?.title">
                {{ plan[node.planIdx]?.title ?? '' }}
              </span>
              <span class="node-emoji">{{ sEmoji(plan[node.planIdx]?.status ?? 'pending') }}</span>
            </div>
            <div v-if="plan[node.planIdx]?.description" class="node-desc" :title="plan[node.planIdx]?.description">
              {{ plan[node.planIdx]?.description }}
            </div>
          </div>
        </div>

        <!-- running 时的脉冲波纹 -->
        <div v-if="plan[node.planIdx]?.status === 'running'" class="running-pulse"></div>
      </div>

      <!-- 图例 -->
      <div class="graph-legend">
        <div class="legend-item"><span class="legend-dot pending"></span><span>等待</span></div>
        <div class="legend-item"><span class="legend-dot running"></span><span>运行中</span></div>
        <div class="legend-item"><span class="legend-dot done"></span><span>成功</span></div>
        <div class="legend-item"><span class="legend-dot failed"></span><span>失败</span></div>
      </div>

      <!-- 空提示 -->
      <div v-if="plan.length === 0" class="empty-hint">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 5v14M5 12h14" stroke-linecap="round"/></svg>
        <span>双击空白处或右键点击添加节点</span>
      </div>

      <!-- 右键菜单 -->
      <div
        v-if="ctxMenu.visible"
        class="context-menu"
        :style="{ left: ctxMenu.x + 'px', top: ctxMenu.y + 'px' }"
        @click.stop
      >
        <div class="menu-item" @click="ctxAddNode">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
          <span>添加节点</span>
        </div>
        <div class="menu-item" @click="closeCtxMenu">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
          <span>取消</span>
        </div>
      </div>
    </div>

    <!-- ── 节点详情侧板 ── -->
    <Transition name="detail-slide">
      <div v-if="detailPanel.visible && detailStep" class="detail-panel" @click.stop>
        <!-- 关闭 -->
        <button class="detail-close" @click="detailPanel.visible = false">
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M18 6L6 18M6 6l12 12"/></svg>
        </button>

        <!-- 状态徽章 -->
        <div class="detail-status-row">
          <span class="detail-emoji">{{ sEmoji(detailStep.status) }}</span>
          <span class="detail-badge" :style="{ color: sTextColor(detailStep.status), borderColor: sBorder(detailStep.status) }">
            {{ sLabel(detailStep.status) }}
          </span>
        </div>

        <div class="detail-title">{{ detailStep.title }}</div>

        <div v-if="detailStep.description" class="detail-field">
          <div class="detail-label">描述</div>
          <div class="detail-val">{{ detailStep.description }}</div>
        </div>

        <div v-if="detailStep.result" class="detail-field">
          <div class="detail-label">执行结果</div>
          <div class="detail-val detail-result">{{ detailStep.result }}</div>
        </div>

        <!-- 操作按钮 -->
        <div class="detail-actions" v-if="!loading">
          <button class="detail-btn" @click="emit('editNode', detailStep!, detailIdx); detailPanel.visible = false">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M11 4H4a2 2 0 00-2 2v14a2 2 0 002 2h14a2 2 0 002-2v-7"/><path d="M18.5 2.5a2.121 2.121 0 013 3L12 15l-4 1 1-4 9.5-9.5z"/></svg>
            编辑
          </button>
          <button class="detail-btn" @click="emit('addNode', detailIdx); detailPanel.visible = false">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>
            插入后
          </button>
          <button class="detail-btn detail-btn--danger" :disabled="plan.length <= 1" @click="emit('deleteNode', detailIdx); detailPanel.visible = false">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M3 6h18M8 6V4h8v2M19 6l-1 14a2 2 0 01-2 2H8a2 2 0 01-2-2L5 6"/></svg>
            删除
          </button>
        </div>
      </div>
    </Transition>
  </div>
</template>

<style scoped>
/* ── 整体容器 ── */
.flow-graph {
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--cf-card, #fff);
  position: relative;
  overflow: hidden;
}

/* ── 工具栏 ── */
.toolbar {
  height: 42px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 0 14px;
  border-bottom: 1px solid #eceef4;
  background: #fff;
  flex-shrink: 0;
}
.toolbar-left { display: flex; align-items: center; gap: 8px; }
.toolbar-title { font-size: 13px; font-weight: 600; color: #111827; }
.node-count {
  font-size: 11px; font-weight: 600;
  color: #00AEEC; background: #E3F6FD;
  padding: 1px 7px; border-radius: 10px;
}
.toolbar-actions { display: flex; gap: 6px; }
.toolbar-btn {
  display: flex; align-items: center; gap: 5px;
  padding: 4px 10px;
  background: #f8f9fc; border: 1px solid #e4e6ef;
  border-radius: 7px; color: #6b7280; font-size: 12px;
  cursor: pointer; transition: all .18s ease; font-family: inherit;
}
.toolbar-btn:hover { background: #E3F6FD; border-color: #B8E6F9; color: #00AEEC; transform: translateY(-1px); }

/* ── 画布 ── */
.graph-container {
  flex: 1;
  position: relative;
  overflow: auto;
  /* 点状网格背景 — 与 demo 一致 */
  background: linear-gradient(135deg, #fafbfc, #f5f7fa);
  background-image: radial-gradient(circle, #E4E7ED 1px, transparent 1px);
  background-size: 20px 20px;
  min-height: 200px;
}

/* ── SVG 边 ── */
.edges-svg { pointer-events: none; }
.edge-path {
  fill: none;
  stroke: #6b9eff;
  stroke-width: 2;
  stroke-dasharray: 5, 5;
}
.edge-path.animated {
  animation: flow-dash 1s linear infinite;
}
@keyframes flow-dash {
  to { stroke-dashoffset: -10; }
}

/* ── 工作流节点 ── */
.workflow-node {
  position: absolute;
  border-radius: 12px;
  border-style: solid;
  background: #fff;
  box-shadow: 0 4px 12px rgba(0,0,0,0.06);
  cursor: pointer;
  user-select: none;
  transition: box-shadow .15s ease, transform .15s ease;
  z-index: 10;
  display: flex;
  align-items: stretch;
  overflow: hidden;
}
.workflow-node:hover {
  box-shadow: 0 8px 24px rgba(0,0,0,0.12);
  transform: translateY(-2px);
}
.workflow-node.dragging {
  box-shadow: 0 12px 32px rgba(0,0,0,0.18);
  transform: scale(1.04);
  z-index: 100;
}
.workflow-node.running {
  animation: nodePulse 1.5s ease-in-out infinite;
}
@keyframes nodePulse {
  0%, 100% { box-shadow: 0 4px 12px rgba(107,158,255,0.2); }
  50%       { box-shadow: 0 4px 20px rgba(107,158,255,0.5); }
}

/* 左侧色条 */
.node-accent {
  width: 4px;
  flex-shrink: 0;
  border-radius: 12px 0 0 12px;
}

/* 节点内容 */
.node-content {
  flex: 1;
  display: flex;
  align-items: center;
  padding: 0 10px;
  min-width: 0;
}
.node-main { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 3px; }
.node-header-row {
  display: flex; align-items: center; gap: 6px;
}
.node-num {
  font-size: 10px; font-weight: 700;
  width: 18px; height: 18px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  background: rgba(0,0,0,0.04);
  flex-shrink: 0;
}
.node-label {
  font-size: 12.5px; font-weight: 600; color: #111827;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  flex: 1; line-height: 1.3;
}
.node-emoji { font-size: 14px; flex-shrink: 0; line-height: 1; }
.node-desc {
  font-size: 10px; color: #9ca3af;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  padding-left: 24px;
}

/* running 波纹 */
.running-pulse {
  position: absolute; inset: -2px; border-radius: 14px;
  border: 2px solid rgba(107,158,255,0.4);
  animation: ripple 1.5s ease-in-out infinite;
  pointer-events: none;
}
@keyframes ripple {
  0%   { opacity: 1; transform: scale(1); }
  100% { opacity: 0; transform: scale(1.08); }
}

/* ── 图例 ── */
.graph-legend {
  position: absolute; bottom: 10px; left: 12px;
  display: flex; gap: 10px;
  background: rgba(255,255,255,0.88);
  backdrop-filter: blur(6px);
  padding: 5px 10px; border-radius: 8px;
  border: 1px solid #eceef4;
  box-shadow: 0 2px 8px rgba(0,0,0,0.06);
  pointer-events: none;
}
.legend-item {
  display: flex; align-items: center; gap: 4px;
  font-size: 10.5px; color: #6b7280;
}
.legend-dot {
  width: 7px; height: 7px; border-radius: 50%;
}
.legend-dot.pending { background: #909399; }
.legend-dot.running { background: #6B9EFF; }
.legend-dot.done    { background: #67C23A; }
.legend-dot.failed  { background: #F56C6C; }

/* ── 空提示 ── */
.empty-hint {
  position: absolute; top: 50%; left: 50%; transform: translate(-50%,-50%);
  display: flex; align-items: center; gap: 6px;
  font-size: 12px; color: #c4b5fd; pointer-events: none;
}

/* ── 右键菜单 ── */
.context-menu {
  position: absolute; z-index: 500;
  background: #fff; border-radius: 10px;
  box-shadow: 0 8px 32px rgba(0,0,0,0.14), 0 2px 8px rgba(0,0,0,0.06);
  border: 1px solid #eceef4;
  padding: 5px 0;
  min-width: 130px;
  animation: fadeIn .15s ease;
}
@keyframes fadeIn { from { opacity:0; transform: scale(.96) translateY(-4px); } to { opacity:1; transform: none; } }
.menu-item {
  display: flex; align-items: center; gap: 8px;
  padding: 8px 14px; cursor: pointer;
  font-size: 13px; color: #374151;
  transition: background .1s;
}
.menu-item:hover { background: #F0FAFD; color: #00AEEC; }

/* ── 弹性反馈 ── */
:deep(.bounce-feedback) {
  position: absolute; z-index: 999;
  font-size: 20px; pointer-events: none;
  animation: bounce-out .7s cubic-bezier(0.34, 1.56, 0.64, 1) forwards;
  transform: translate(-50%, -50%);
}
@keyframes bounce-out {
  0%   { opacity: 1; transform: translate(-50%,-50%) scale(.5); }
  60%  { opacity: 1; transform: translate(-50%,-80%) scale(1.2); }
  100% { opacity: 0; transform: translate(-50%,-120%) scale(.8); }
}

/* ── 节点详情侧板 ── */
.detail-panel {
  position: absolute; top: 42px; right: 0; bottom: 0;
  width: 220px; z-index: 200;
  background: rgba(255,255,255,0.96);
  backdrop-filter: blur(12px);
  border-left: 1px solid #eceef4;
  box-shadow: -4px 0 20px rgba(0,0,0,0.08);
  padding: 14px 14px 16px;
  display: flex; flex-direction: column; gap: 12px;
  overflow-y: auto;
}
.detail-close {
  position: absolute; top: 10px; right: 10px;
  width: 24px; height: 24px; border-radius: 6px;
  background: #f4f5f9; border: none; cursor: pointer;
  display: flex; align-items: center; justify-content: center;
  color: #9ca3af; transition: all .15s;
}
.detail-close:hover { background: #fee2e2; color: #ef4444; }
.detail-status-row { display: flex; align-items: center; gap: 7px; }
.detail-emoji { font-size: 20px; }
.detail-badge {
  font-size: 11px; font-weight: 600;
  padding: 2px 9px; border-radius: 99px; border: 1.5px solid;
}
.detail-title {
  font-size: 13.5px; font-weight: 700; color: #111827; line-height: 1.4;
}
.detail-field { display: flex; flex-direction: column; gap: 4px; }
.detail-label {
  font-size: 10px; font-weight: 600; text-transform: uppercase;
  letter-spacing: 0.7px; color: #9ca3af;
}
.detail-val {
  font-size: 12px; color: #374151; line-height: 1.6;
  background: #f8f9fc; border: 1px solid #eceef4;
  border-radius: 7px; padding: 7px 10px;
}
.detail-result {
  font-family: 'Fira Code', Consolas, monospace; font-size: 11px;
  max-height: 150px; overflow-y: auto; white-space: pre-wrap; word-break: break-all;
}
.detail-actions {
  display: flex; flex-wrap: wrap; gap: 6px; margin-top: auto; padding-top: 8px;
  border-top: 1px solid #f0f0f5;
}
.detail-btn {
  display: flex; align-items: center; gap: 5px;
  padding: 5px 10px; border-radius: 7px;
  background: #f8f9fc; border: 1px solid #e4e6ef;
  color: #374151; font-size: 11.5px; font-family: inherit;
  cursor: pointer; transition: all .15s;
}
.detail-btn:hover { background: #E3F6FD; border-color: #B8E6F9; color: #00AEEC; }
.detail-btn--danger:hover { background: #fff1f0; border-color: #fca5a5; color: #ef4444; }
.detail-btn:disabled { opacity: 0.4; cursor: not-allowed; }

/* 侧板过渡 */
.detail-slide-enter-active, .detail-slide-leave-active {
  transition: all .22s cubic-bezier(0.4, 0, 0.2, 1);
}
.detail-slide-enter-from, .detail-slide-leave-to {
  transform: translateX(100%);
  opacity: 0;
}
</style>
