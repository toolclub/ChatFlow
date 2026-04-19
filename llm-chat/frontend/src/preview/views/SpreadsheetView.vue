<script setup lang="ts">
import { ref, onMounted } from 'vue'
import type { ViewProps } from '../types'

const props = defineProps<ViewProps>()

interface SheetMeta { name: string; html: string; truncated: boolean; loaded: boolean }
const sheetNames = ref<string[]>([])
const sheets = ref<Record<string, SheetMeta>>({})
const activeSheet = ref('')
const SHEET_ROW_LIMIT = 1000
let _wb: any = null

onMounted(async () => {
  if (props.data.type !== 'arrayBuffer') return
  const XLSX = await import('xlsx')
  _wb = XLSX.read(new Uint8Array(props.data.buffer), { type: 'array' })
  const names = _wb.SheetNames as string[]
  sheetNames.value = names
  if (names.length) {
    activeSheet.value = names[0]
    renderSheet(names[0])
  }
})

async function renderSheet(name: string) {
  if (!_wb || sheets.value[name]?.loaded) return
  const XLSX = await import('xlsx')
  const ws = _wb.Sheets[name]
  if (!ws) return
  let truncated = false
  if (ws['!ref']) {
    const range = XLSX.utils.decode_range(ws['!ref'])
    if (range.e.r - range.s.r + 1 > SHEET_ROW_LIMIT) {
      range.e.r = range.s.r + SHEET_ROW_LIMIT - 1
      ws['!ref'] = XLSX.utils.encode_range(range)
      truncated = true
    }
  }
  const html = XLSX.utils.sheet_to_html(ws, { id: '', editable: false })
  sheets.value = { ...sheets.value, [name]: { name, html, truncated, loaded: true } }
}

function selectSheet(name: string) {
  activeSheet.value = name
  if (!sheets.value[name]?.loaded) renderSheet(name)
}
</script>

<template>
  <div class="ss-wrap">
    <div v-if="sheetNames.length > 1" class="ss-tabs">
      <button
        v-for="name in sheetNames" :key="name"
        class="ss-tab" :class="{ active: name === activeSheet }"
        @click="selectSheet(name)"
      >{{ name }}</button>
    </div>
    <div v-if="sheets[activeSheet]?.truncated" class="ss-tip">
      仅显示前 {{ SHEET_ROW_LIMIT }} 行
    </div>
    <div v-if="sheets[activeSheet]?.loaded" class="ss-scroll" v-html="sheets[activeSheet].html" />
    <div v-else class="ss-loading">解析中...</div>
  </div>
</template>

<style scoped>
.ss-wrap { flex: 1; display: flex; flex-direction: column; overflow: hidden; }
.ss-tabs {
  display: flex; gap: 2px; padding: 6px 10px 0;
  background: #F1F2F3; border-bottom: 1px solid #E3E5E7; overflow-x: auto;
}
.ss-tab {
  padding: 6px 14px; border: none; cursor: pointer; font-size: 12px;
  color: #61666D; background: transparent; border-radius: 6px 6px 0 0;
  white-space: nowrap; transition: background 0.15s;
}
.ss-tab:hover { background: #E3E5E7; }
.ss-tab.active { background: #fff; color: #00AEEC; font-weight: 600; }
.ss-tip {
  padding: 6px 14px; font-size: 12px; color: #FB7299;
  background: #FFF4F8; border-bottom: 1px solid #FFE0EC;
}
.ss-scroll { flex: 1; overflow: auto; background: #fff; }
.ss-loading {
  flex: 1; display: flex; align-items: center; justify-content: center;
  font-size: 13px; color: #9499A0;
}
.ss-scroll :deep(table) {
  border-collapse: collapse; font-size: 12px;
  font-family: 'Consolas', 'Monaco', monospace;
}
.ss-scroll :deep(th),
.ss-scroll :deep(td) {
  border: 1px solid #E3E5E7; padding: 4px 8px; min-width: 60px;
  white-space: nowrap; color: #18191C;
}
.ss-scroll :deep(tr:nth-child(even)) { background: #FAFBFC; }
</style>
