/**
 * 数据准备层：根据渲染器声明的 source，把后端字节流转成对应形式。
 * 调用者负责在不再使用时调用返回值的 revoke()（如果存在）。
 */
import { fetchArtifactBlob } from '../api'
import type { DataSource, PreparedData } from './types'

export async function prepare(source: DataSource, fileId: number): Promise<PreparedData> {
  const blob = await fetchArtifactBlob(fileId)
  switch (source) {
    case 'blobUrl': {
      const url = URL.createObjectURL(blob)
      return { type: 'blobUrl', url, revoke: () => URL.revokeObjectURL(url) }
    }
    case 'text':
      return { type: 'text', text: await blob.text() }
    case 'arrayBuffer':
      return { type: 'arrayBuffer', buffer: await blob.arrayBuffer() }
  }
}
