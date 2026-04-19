/**
 * 预览注册表 + 派发入口
 *
 * 用法：
 *   const r = pickRenderer(file)
 *   if (!r) showUnsupported(getUnsupportedHint(file) ?? '不支持在线预览')
 *   else { const data = await prepare(r.source, file.id); mount(r.component, { data, file }) }
 */
import type { Renderer, PreviewFile } from './types'
import { imageRenderer } from './renderers/image'
import { pdfRenderer } from './renderers/pdf'
import { videoRenderer } from './renderers/video'
import { audioRenderer } from './renderers/audio'
import { spreadsheetRenderer } from './renderers/spreadsheet'
import { docxRenderer } from './renderers/docx'
import { markdownRenderer } from './renderers/markdown'
import { codeRenderer } from './renderers/code'

/** 注册表 —— 顺序无关（后缀互斥），新加渲染器在这 push 一行即可 */
const RENDERERS: Renderer[] = [
  imageRenderer,
  pdfRenderer,
  videoRenderer,
  audioRenderer,
  spreadsheetRenderer,
  docxRenderer,
  markdownRenderer,
  codeRenderer,
]

/** 已知"不可渲染"后缀 → 友好副标题（仅 UX 提示，不参与决策） */
const UNSUPPORTED_HINTS: Record<string, string> = {
  // Office（除 Excel/Word 外）
  doc: 'Office 文档（.doc 老格式）', ppt: 'PowerPoint', pptx: 'PowerPoint',
  odt: 'OpenDocument 文档', odp: 'OpenDocument 演示', ods: 'OpenDocument 表格',
  rtf: 'RTF 文档', pages: 'iWork Pages', key: 'iWork Keynote', numbers: 'iWork Numbers',
  // 归档/打包
  zip: '归档/打包文件', tar: '归档/打包文件', gz: '归档/打包文件',
  tgz: '归档/打包文件', '7z': '归档/打包文件', rar: '归档/打包文件',
  bz2: '归档/打包文件', xz: '归档/打包文件', zst: '归档/打包文件',
  jar: 'Java 包', war: 'Java 包', ear: 'Java 包',
  deb: '安装包', rpm: '安装包', apk: 'Android 安装包', ipa: 'iOS 安装包',
  dmg: '磁盘镜像', iso: '光盘镜像', msi: 'Windows 安装包',
  whl: 'Python wheel 包', pkg: '安装包',
  // 可执行/二进制
  exe: '可执行文件', dll: '动态链接库', sys: '系统驱动',
  so: '动态库', dylib: '动态库', wasm: 'WebAssembly 二进制',
  class: 'Java 字节码', pyc: 'Python 字节码', pyo: 'Python 字节码', dex: 'Android 字节码',
  bin: '二进制', elf: '可执行文件',
  // 设计稿
  psd: 'Photoshop 设计稿', psb: 'Photoshop 大文件', ai: 'Illustrator',
  sketch: 'Sketch 设计稿', fig: 'Figma 设计稿', xd: 'Adobe XD',
  indd: 'InDesign', cdr: 'CorelDRAW',
  // 相机 RAW / 罕见图
  raw: '相机 RAW 图像', cr2: '佳能 RAW', cr3: '佳能 RAW',
  nef: '尼康 RAW', arw: '索尼 RAW', dng: 'DNG RAW',
  orf: '奥林巴斯 RAW', rw2: '松下 RAW',
  heic: 'Apple HEIC 图像', heif: 'HEIF 图像',
  tiff: 'TIFF 图像', tif: 'TIFF 图像',
  // 浏览器不支持的音视频
  mov: 'QuickTime 视频', avi: 'AVI 视频', mkv: 'MKV 视频',
  wmv: 'WMV 视频', flv: 'Flash 视频', wma: 'WMA 音频', aiff: 'AIFF 音频',
  // 字体
  ttf: '字体', otf: '字体', woff: 'Web 字体', woff2: 'Web 字体', eot: '字体',
  // 数据库
  db: '数据库文件', sqlite: 'SQLite 数据库', sqlite3: 'SQLite 数据库',
  mdb: 'Access 数据库', accdb: 'Access 数据库',
  // 加密 / 证书
  gpg: '加密文件', pgp: '加密文件', asc: '加密文件',
  enc: '加密文件', p12: '证书容器', pfx: '证书容器', pem: '证书',
}

function getExt(name: string): string {
  const i = name.lastIndexOf('.')
  return i >= 0 ? name.slice(i + 1).toLowerCase() : ''
}

/** 根据文件名挑选渲染器，没有匹配返回 null */
export function pickRenderer(file: PreviewFile): Renderer | null {
  const ext = getExt(file.name)
  if (!ext) return null
  return RENDERERS.find(r => r.extensions.includes(ext)) ?? null
}

/** 获取"不可渲染"的友好副标题，无注册返回 null */
export function getUnsupportedHint(file: PreviewFile): string | null {
  const ext = getExt(file.name)
  return UNSUPPORTED_HINTS[ext] ?? null
}

/** 给 modal 用的"标签"（命中渲染器 → 渲染器 label，否则 → 后缀大写） */
export function getDisplayLabel(file: PreviewFile, renderer: Renderer | null): string {
  if (renderer) return renderer.label
  const ext = getExt(file.name)
  return ext ? ext.toUpperCase() : '未知'
}
