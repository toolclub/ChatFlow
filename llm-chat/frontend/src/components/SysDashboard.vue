<script setup lang="ts">
import { ref, onMounted, onUnmounted, nextTick } from 'vue'
import * as echarts from 'echarts'
import { 
  User, ChatDotRound, Cpu, PieChart, TrendCharts, Refresh, ArrowLeft, Close
} from '@element-plus/icons-vue'
import { get } from '../api'

const props = defineProps<{
  adminKey: string
}>()

const emit = defineEmits<{
  back: []
}>()

const loading = ref(true)
const stats = ref<any>(null)
const users = ref<any[]>([])

// 图表实例
let trendChart: echarts.ECharts | null = null
let modelChart: echarts.ECharts | null = null

const trendChartRef = ref<HTMLElement | null>(null)
const modelChartRef = ref<HTMLElement | null>(null)

async function fetchData() {
  loading.value = true
  try {
    const headers = { 'X-Admin-Key': props.adminKey }
    const [statsData, usersData] = await Promise.all([
      get<any>('/api/admin/stats', {}, headers),
      get<any[]>('/api/admin/users', {}, headers)
    ])
    stats.value = statsData
    users.value = usersData
    
    await nextTick()
    renderCharts()
  } catch (err) {
    console.error('获取管理数据失败:', err)
  } finally {
    loading.value = false
  }
}

const renderCharts = () => {
  if (!stats.value) return
  const isDark = document.body.classList.contains('dark')
  const textColor = isDark ? '#A2A7AE' : '#64748B'
  const splitLineColor = isDark ? '#2B2C30' : '#EBF0F5'

  // 1. 趋势图
  if (trendChartRef.value) {
    if (!trendChart) trendChart = echarts.init(trendChartRef.value)
    trendChart.setOption({
      backgroundColor: 'transparent',
      tooltip: { 
        trigger: 'axis',
        backgroundColor: isDark ? '#1F2023' : '#fff',
        borderColor: isDark ? '#323335' : '#EBF0F5',
        textStyle: { color: isDark ? '#E6E7E9' : '#18191C' }
      },
      legend: { 
        data: ['消息数', 'Token 消耗'], 
        bottom: 0,
        textStyle: { color: textColor }
      },
      grid: { left: '3%', right: '4%', top: '12%', bottom: '15%', containLabel: true },
      xAxis: { 
        type: 'category', 
        data: stats.value.charts.trend.days,
        axisLine: { lineStyle: { color: splitLineColor } },
        axisLabel: { color: textColor }
      },
      yAxis: [
        { 
          type: 'value', 
          name: '消息', 
          position: 'left',
          splitLine: { lineStyle: { color: splitLineColor } },
          axisLabel: { color: textColor }
        },
        { 
          type: 'value', 
          name: 'Tokens', 
          position: 'right',
          splitLine: { show: false },
          axisLabel: { color: textColor }
        }
      ],
      series: [
        {
          name: '消息数',
          type: 'bar',
          data: stats.value.charts.trend.messages,
          itemStyle: { 
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: '#00AEEC' },
              { offset: 1, color: '#0095CC' }
            ]),
            borderRadius: [4, 4, 0, 0]
          },
          barWidth: '35%'
        },
        {
          name: 'Token 消耗',
          type: 'line',
          yAxisIndex: 1,
          data: stats.value.charts.trend.tokens,
          itemStyle: { color: '#FB7299' },
          lineStyle: { width: 3 },
          symbol: 'circle',
          symbolSize: 8,
          smooth: true,
          areaStyle: {
            color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
              { offset: 0, color: 'rgba(251, 114, 153, 0.2)' },
              { offset: 1, color: 'rgba(251, 114, 153, 0)' }
            ])
          }
        }
      ]
    })
  }

  // 2. 模型分布图
  if (modelChartRef.value) {
    if (!modelChart) modelChart = echarts.init(modelChartRef.value)
    modelChart.setOption({
      backgroundColor: 'transparent',
      tooltip: { 
        trigger: 'item',
        backgroundColor: isDark ? '#1F2023' : '#fff',
        borderColor: isDark ? '#323335' : '#EBF0F5',
        textStyle: { color: isDark ? '#E6E7E9' : '#18191C' }
      },
      legend: { 
        orient: 'vertical', 
        left: '5%', 
        top: 'center',
        textStyle: { color: textColor },
        itemGap: 15
      },
      series: [
        {
          name: '模型使用量',
          type: 'pie',
          center: ['65%', '50%'],
          radius: ['50%', '75%'],
          avoidLabelOverlap: false,
          itemStyle: { 
            borderRadius: 8, 
            borderColor: isDark ? '#1F2023' : '#fff', 
            borderWidth: 2 
          },
          label: { show: false },
          emphasis: { 
            label: { 
              show: true, 
              fontSize: 14, 
              fontWeight: 'bold',
              color: isDark ? '#E6E7E9' : '#18191C'
            } 
          },
          data: stats.value.charts.models
        }
      ]
    })
  }
}

function handleResize() {
  trendChart?.resize()
  modelChart?.resize()
}

onMounted(() => {
  fetchData()
  window.addEventListener('resize', handleResize)
})

onUnmounted(() => {
  window.removeEventListener('resize', handleResize)
})

function formatTime(ts: number) {
  if (!ts) return '-'
  const d = new Date(ts * 1000)
  return d.toLocaleString()
}
</script>

<template>
  <div class="admin-dashboard">
    <!-- 头部导航 -->
    <div class="admin-header">
      <div class="header-left">
        <el-button :icon="ArrowLeft" circle @click="emit('back')" />
        <h2 class="header-title">系统管理面板</h2>
      </div>
      <div class="header-actions">
        <el-button :icon="Refresh" @click="fetchData" :loading="loading">刷新数据</el-button>
        <el-button type="danger" plain :icon="Close" @click="emit('back')">退出</el-button>
      </div>
    </div>

    <div v-if="loading && !stats" class="loading-state">
      <el-skeleton :rows="10" animated />
    </div>

    <div v-else class="admin-content">
      <!-- 数据概览卡片 -->
      <div class="stats-grid">
        <div class="stat-card blue">
          <div class="stat-icon"><el-icon><User /></el-icon></div>
          <div class="stat-info">
            <div class="stat-label">总用户 / 今日新增</div>
            <div class="stat-value">{{ stats.summary.total_users }} <span class="sub">/ {{ stats.summary.new_users_today }}</span></div>
          </div>
        </div>
        <div class="stat-card pink">
          <div class="stat-icon"><el-icon><ChatDotRound /></el-icon></div>
          <div class="stat-info">
            <div class="stat-label">总对话 / 总消息</div>
            <div class="stat-value">{{ stats.summary.total_conversations }} <span class="sub">/ {{ stats.summary.total_messages }}</span></div>
          </div>
        </div>
        <div class="stat-card purple">
          <div class="stat-icon"><el-icon><Cpu /></el-icon></div>
          <div class="stat-info">
            <div class="stat-label">总 Token 消耗</div>
            <div class="stat-value">{{ (stats.summary.total_prompt_tokens + stats.summary.total_completion_tokens).toLocaleString() }}</div>
          </div>
        </div>
        <div class="stat-card green">
          <div class="stat-icon"><el-icon><TrendCharts /></el-icon></div>
          <div class="stat-info">
            <div class="stat-label">推理 Token (Reasoning)</div>
            <div class="stat-value">{{ stats.summary.total_reasoning_tokens.toLocaleString() }}</div>
          </div>
        </div>
      </div>

      <!-- 图表区域 -->
      <div class="charts-row">
        <div class="chart-container main-chart">
          <div class="chart-header">
            <el-icon><TrendCharts /></el-icon>
            <span>使用趋势 (最近 7 天)</span>
          </div>
          <div ref="trendChartRef" class="chart-body"></div>
        </div>
        <div class="chart-container side-chart">
          <div class="chart-header">
            <el-icon><PieChart /></el-icon>
            <span>模型分布</span>
          </div>
          <div ref="modelChartRef" class="chart-body"></div>
        </div>
      </div>

      <!-- 最近访问用户 -->
      <div class="users-section">
        <div class="section-header">
          <el-icon><User /></el-icon>
          <span>最近活跃用户 (Top 50)</span>
        </div>
        <el-table :data="users" border stripe style="width: 100%" size="small" class="admin-table">
          <el-table-column prop="name" label="名称" width="120" />
          <el-table-column prop="email" label="邮箱" min-width="180" />
          <el-table-column label="最近登录" width="180">
            <template #default="scope">{{ formatTime(scope.row.last_login_at) }}</template>
          </el-table-column>
          <el-table-column label="注册时间" width="180">
            <template #default="scope">{{ formatTime(scope.row.created_at) }}</template>
          </el-table-column>
          <el-table-column label="状态" width="100">
            <template #default="scope">
              <el-tag :type="scope.row.is_active ? 'success' : 'danger'" size="small">
                {{ scope.row.is_active ? '活跃' : '禁用' }}
              </el-tag>
            </template>
          </el-table-column>
        </el-table>
      </div>
    </div>
  </div>
</template>

<style scoped>
.admin-dashboard {
  flex: 1;
  height: 100%;
  display: flex;
  flex-direction: column;
  background: var(--cf-bg);
  overflow-y: auto;
  padding: 24px;
  min-width: 0;
}

.admin-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 24px;
  background: var(--cf-card);
  padding: 16px 24px;
  border-radius: var(--cf-radius-lg);
  box-shadow: var(--cf-shadow-sm);
  border: 1px solid var(--cf-border-soft);
}

.header-left {
  display: flex;
  align-items: center;
  gap: 16px;
}

.header-title {
  font-size: 20px;
  font-weight: 800;
  color: var(--cf-text-1);
}

.stats-grid {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 20px;
  margin-bottom: 24px;
}

@media (max-width: 1200px) {
  .stats-grid {
    grid-template-columns: repeat(2, 1fr);
  }
}

.stat-card {
  background: var(--cf-card);
  border-radius: var(--cf-radius-lg);
  padding: 24px;
  display: flex;
  align-items: center;
  gap: 20px;
  box-shadow: var(--cf-shadow-sm);
  border: 1px solid var(--cf-border-soft);
  transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
}

.stat-card:hover {
  transform: translateY(-4px);
  box-shadow: var(--cf-shadow-md);
  border-color: var(--cf-bili-blue);
}

.stat-icon {
  width: 56px;
  height: 56px;
  border-radius: 16px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 28px;
}

.stat-card.blue .stat-icon { background: rgba(0, 174, 236, 0.1); color: var(--cf-bili-blue); }
.stat-card.pink .stat-icon { background: rgba(251, 114, 153, 0.1); color: var(--cf-bili-pink); }
.stat-card.purple .stat-icon { background: rgba(168, 85, 247, 0.1); color: #a855f7; }
.stat-card.green .stat-icon { background: rgba(34, 197, 94, 0.1); color: var(--cf-green); }

.stat-info {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-label {
  font-size: 13px;
  color: var(--cf-text-2);
  font-weight: 500;
}

.stat-value {
  font-size: 26px;
  font-weight: 850;
  color: var(--cf-text-1);
  font-family: 'Inter', system-ui, sans-serif;
}

.stat-value .sub {
  font-size: 14px;
  color: var(--cf-text-3);
  font-weight: normal;
  margin-left: 4px;
}

.charts-row {
  display: grid;
  grid-template-columns: 2fr 1fr;
  gap: 24px;
  margin-bottom: 24px;
}

@media (max-width: 1024px) {
  .charts-row {
    grid-template-columns: 1fr;
  }
}

.chart-container {
  background: var(--cf-card);
  border-radius: var(--cf-radius-lg);
  padding: 24px;
  box-shadow: var(--cf-shadow-sm);
  border: 1px solid var(--cf-border-soft);
}

.chart-header {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 700;
  color: var(--cf-text-1);
  margin-bottom: 24px;
}

.chart-header .el-icon {
  font-size: 20px;
  color: var(--cf-bili-blue);
}

.chart-body {
  height: 360px;
  width: 100%;
}

.users-section {
  background: var(--cf-card);
  border-radius: var(--cf-radius-lg);
  padding: 24px;
  box-shadow: var(--cf-shadow-sm);
  border: 1px solid var(--cf-border-soft);
  margin-bottom: 24px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 10px;
  font-size: 16px;
  font-weight: 700;
  color: var(--cf-text-1);
  margin-bottom: 24px;
}

.section-header .el-icon {
  font-size: 20px;
  color: var(--cf-bili-pink);
}

.admin-table {
  border-radius: var(--cf-radius-md);
  overflow: hidden;
  --el-table-border-color: var(--cf-border-soft);
  --el-table-header-bg-color: var(--cf-bg);
  --el-table-header-text-color: var(--cf-text-1);
  --el-table-text-color: var(--cf-text-2);
}

:deep(.el-table) {
  background-color: transparent;
}

:deep(.el-table tr) {
  background-color: transparent;
}

:deep(.el-table__header-wrapper th) {
  font-weight: 700;
}

.loading-state {
  padding: 40px;
  background: var(--cf-card);
  border-radius: var(--cf-radius-lg);
}
</style>
