import { ref } from 'vue'
import type { FileArtifact } from '../types'

/** Artifact ID and request generation, never a filename, identify the preview. */
export function useArtifactPreview(
  conversationId: () => string | null,
  load: (id: number, signal?: AbortSignal) => Promise<FileArtifact | null>,
  notify: (message: string) => void,
) {
  const selectedFile = ref<FileArtifact | null>(null)
  const loading = ref(false)
  let generation = 0
  let controller: AbortController | null = null

  function clear() {
    generation++
    controller?.abort()
    controller = null
    selectedFile.value = null
    loading.value = false
  }

  async function select(file: FileArtifact) {
    clear()
    selectedFile.value = file
    if (file.language === 'archive') return
    if (file.content && (file.language !== 'pptx' || file.slides_html?.length)) return
    if (!file.id) return
    const request = generation
    const owner = conversationId()
    const abort = controller = new AbortController()
    const isCurrent = () => generation === request && conversationId() === owner
    loading.value = true
    try {
      const full = await load(file.id, abort.signal)
      if (isCurrent() && full) selectedFile.value = full
    } catch (error) {
      if (isCurrent() && !abort.signal.aborted) notify(error instanceof Error ? error.message : '文件预览加载失败')
    } finally {
      if (isCurrent()) {
        loading.value = false
        controller = null
      }
    }
  }

  return { selectedFile, loading, select, clear }
}
