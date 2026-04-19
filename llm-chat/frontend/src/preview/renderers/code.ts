import { defineAsyncComponent } from 'vue'
import type { Renderer } from '../types'

/**
 * Code 渲染器：所有"已知文本类"后缀。
 * 关键决定：只接管已知后缀，不做"unknown 兜底"——
 * 未识别后缀走 modal 的 unsupported 分支（颜文字），可预期、可调试。
 * 真二进制内容混进来（如 .txt 里塞了二进制）由 CodeView 内部嗅探兜底。
 */
export const codeRenderer: Renderer = {
  id: 'code',
  label: '代码',
  extensions: [
    // Web
    'html', 'htm', 'css', 'scss', 'sass', 'less',
    // JS/TS
    'js', 'mjs', 'cjs', 'jsx', 'ts', 'tsx', 'vue', 'svelte',
    // Backend
    'py', 'rb', 'go', 'rs', 'java', 'kt', 'kts',
    'c', 'cpp', 'cc', 'cxx', 'h', 'hpp', 'hxx',
    'cs', 'fs', 'fsx', 'vb',
    'sh', 'bash', 'zsh', 'fish', 'ps1', 'bat', 'cmd',
    'lua', 'php', 'swift', 'scala', 'dart', 'r', 'pl', 'pm',
    'ex', 'exs', 'erl', 'clj', 'hs', 'elm', 'ml',
    // Data / config
    'json', 'jsonc', 'yaml', 'yml', 'toml', 'xml', 'sql', 'graphql', 'gql',
    'ini', 'conf', 'cfg', 'env', 'properties', 'gradle',
    // Plain
    'txt', 'log', 'text',
    // Build
    'dockerfile', 'makefile', 'cmake',
  ],
  source: 'text',
  component: defineAsyncComponent(() => import('../views/CodeView.vue')),
}
