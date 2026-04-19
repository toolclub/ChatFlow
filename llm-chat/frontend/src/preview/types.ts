/**
 * 预览生态：能力驱动的渲染器接口
 *
 * 设计哲学：
 *   - 能不能预览 = 注册表里有没有匹配的渲染器（不再有"不支持列表"）
 *   - 渲染器是自包含模块：声明接管哪些后缀、需要什么形式的数据、用哪个组件渲染
 *   - 加新格式 = 加一个 renderer 文件，不动 modal、不动其它任何模块
 */
import type { Component } from 'vue'

/** 渲染器需要的"原始数据"形式 */
export type DataSource = 'blobUrl' | 'text' | 'arrayBuffer'

/** 准备好的数据，按 source 类型分发 */
export type PreparedData =
  | { type: 'blobUrl'; url: string; revoke: () => void }
  | { type: 'text'; text: string }
  | { type: 'arrayBuffer'; buffer: ArrayBuffer }

/** 一个渲染器声明 */
export interface Renderer {
  /** 标识（调试/日志用） */
  id: string
  /** 用户可见的标签，会显示在模态 header */
  label: string
  /** 接管的文件后缀（小写，无点） */
  extensions: string[]
  /** 数据准备模式：modal 拉好数据再喂给组件 */
  source: DataSource
  /** 真正的渲染组件，建议用 defineAsyncComponent 动态加载 */
  component: Component
}

/** 模态需要的最小文件信息 */
export interface PreviewFile {
  id: number
  name: string
  size: number
  path?: string
}

/** 视图组件统一 props */
export interface ViewProps {
  data: PreparedData
  file: PreviewFile
}
