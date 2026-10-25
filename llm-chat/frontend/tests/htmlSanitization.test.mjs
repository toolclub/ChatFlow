import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { pathToFileURL } from 'node:url'
import ts from 'typescript'
import { JSDOM } from 'jsdom'
import { Marked } from 'marked'
import markedKatex from 'marked-katex-extension'
import hljs from 'highlight.js/lib/common'
import * as XLSX from 'xlsx'

// A real HTML/SVG/MathML DOM parser, not a regex or mocked sanitizer.
const dom = new JSDOM('<!doctype html><body></body>', { url: 'https://chat.example/' })
globalThis.window = dom.window
const require = createRequire(import.meta.url)
const compile = source => ts.transpileModule(source, { compilerOptions: {
  target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022,
} }).outputText
const dataUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const helper = compile(await readFile(new URL('../src/utils/sanitizeHtml.ts', import.meta.url), 'utf8'))
  .replace("from 'dompurify'", `from '${pathToFileURL(require.resolve('dompurify')).href}'`)
const { sanitizeHtml } = await import(dataUrl(helper))

async function statements(file, names, expressions = () => false) {
  const source = await readFile(new URL(file, import.meta.url), 'utf8')
  const script = source.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  const tree = ts.createSourceFile(file + '.ts', script, ts.ScriptTarget.Latest, true)
  return tree.statements.filter(node => {
    if (ts.isFunctionDeclaration(node)) return names.has(node.name.text)
    if (ts.isVariableStatement(node)) return node.declarationList.declarations.some(d => names.has(d.name.getText(tree)))
    return expressions(node.getText(tree))
  }).map(node => node.getText(tree)).join('\n')
}

// Execute the component's actual render pipeline, including custom renderers,
// data-URI placeholder restoration, code fences, math and the final sanitizer.
const messageSource = await statements('../src/components/MessageItem.vue', new Set([
  'PREVIEWABLE', '_skipHljs', 'buildCodeHtml', 'markedInstance', '_SKIP_HLJS_THRESHOLD', 'renderContent',
]), code => code.startsWith('markedInstance.use('))
const renderMessage = new Function('Marked', 'markedKatex', 'hljs', 'sanitizeHtml',
  compile(messageSource) + '\nreturn renderContent;')(Marked, markedKatex, hljs, sanitizeHtml)
const markdownSource = await statements('../src/preview/views/MarkdownView.vue', new Set([
  'text', 'TEXT_RENDER_LIMIT', 'truncated', 'safeText', 'marked', 'renderedHtml', 'highlightedSource',
]))
const renderPreview = text => new Function('props', 'computed', 'Marked', 'hljs', 'sanitizeHtml',
  compile(markdownSource) + '\nreturn { html: renderedHtml.value, source: highlightedSource.value };')(
    { data: { type: 'text', text } }, fn => ({ value: fn() }), Marked, hljs, sanitizeHtml)

function parse(html) {
  const target = dom.window.document.createElement('div')
  target.innerHTML = html
  return target
}
function assertSafe(html) {
  const target = parse(html)
  assert.equal(target.querySelector('script,iframe,object,embed,style,foreignObject'), null)
  for (const element of target.querySelectorAll('*')) {
    for (const attr of element.attributes) {
      assert.ok(!/^on/i.test(attr.name), `event handler survived: ${attr.name}`)
      if (['href', 'src', 'xlink:href', 'action', 'formaction'].includes(attr.name)) {
        assert.ok(!/^(?:javascript|vbscript):/i.test(attr.value.replace(/[\u0000-\u0020]/g, '')),
          `unsafe URL survived: ${attr.value}`)
        if (attr.name === 'href' || attr.name === 'xlink:href') assert.ok(!/^data:/i.test(attr.value))
      }
    }
  }
  return target
}

test('markdown preview strips executable raw HTML, dangerous URLs and SVG namespace payloads', () => {
  const attacks = [
    '<script>alert(1)</script><img src=x onerror="alert(1)">',
    '[run](javascript:alert%281%29) <a href="jav&#x61;script:alert(1)">bad</a>',
    '<a href="data:text/html;base64,PHNjcmlwdD4=">unsafe data link</a>',
    '<svg><a xlink:href="javascript:alert(1)"><text>bad</text></a><g onload="alert(1)"></g></svg>',
    '<math><mtext><table><mglyph><style><!--</style><img title="--><img src=x onerror=alert(1)>">',
    '<style>body{display:none}</style><iframe srcdoc="<script>alert(1)</script>"></iframe>',
  ]
  for (const attack of attacks) {
    const result = renderPreview(attack)
    assertSafe(result.html)
    assertSafe(result.source)
  }
})

test('message rendering sanitizes dangerous markdown links and data image attribute injection after substitution', () => {
  const attacks = [
    '[run](javascript:alert%281%29)',
    '![x" onerror="alert(1)](data:image/png;base64,AAAA)',
    '![x](data:image/png;base64,AAAA" onerror="alert%281%29)',
    '![x](https://example.org/image.png "\" onerror=alert(1)")',
    '```unknown><img src=x onerror=alert(1)>\nhello\n```',
    '<!doctype html><html><script>alert(1)</script></html>',
  ]
  for (const attack of attacks) {
    assertSafe(renderMessage(attack))
    assertSafe(renderMessage(attack, true))
  }
})

test('normal markdown, table, code controls, SVG icons and KaTeX survive the final boundary', () => {
  const root = assertSafe(renderMessage('# Heading\n\n**bold** [safe](https://example.org)\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\n```html\n<div>Hello</div>\n```\n\n$\\sqrt{x}+\\frac{1}{2}$'))
  assert.equal(root.querySelector('h1').textContent, 'Heading')
  assert.equal(root.querySelector('strong').textContent, 'bold')
  assert.equal(root.querySelector('a').getAttribute('href'), 'https://example.org')
  assert.ok(root.querySelector('table td'))
  assert.ok(root.querySelector('.cb-copy[data-code]'))
  assert.ok(root.querySelector('.cb-preview[data-code][data-lang="html"]'))
  assert.ok(root.querySelector('.cb-copy svg path'))
  assert.ok(root.querySelector('.katex math'))
  assert.ok(root.querySelector('.katex svg path'))
  assert.match(root.querySelector('pre code').textContent, /<div>Hello<\/div>/)
  const preview = assertSafe(renderPreview('# Title\n\n```js\nconst x = 1\n```').html)
  assert.equal(preview.querySelector('h1').textContent, 'Title')
  assert.match(preview.querySelector('pre code').textContent, /const x = 1/)
  const image = assertSafe(renderMessage('![preview](https://example.org/image.png)')).querySelector('img')
  assert.equal(image.getAttribute('src'), 'https://example.org/image.png')
  assert.equal(image.getAttribute('alt'), 'preview')
  const dataImage = assertSafe(renderMessage('![inline](data:image/svg+xml;base64,PHN2Zy8+)')).querySelector('img')
  assert.equal(dataImage.getAttribute('src'), 'data:image/svg+xml;base64,PHN2Zy8+')
})

test('spreadsheet actual render pipeline strips malicious hyperlinks but preserves cells and merges', async () => {
  const source = await statements('../src/preview/views/SpreadsheetView.vue', new Set(['renderSheet']))
  const ws = XLSX.utils.aoa_to_sheet([['linked', 'plain'], [1, 2]])
  ws.A1.l = { Target: 'javascript:alert(1)' }
  ws['!merges'] = [{ s: { r: 1, c: 0 }, e: { r: 1, c: 1 } }]
  const sheets = { value: {} }
  const run = new Function('_wb', 'sheets', 'SHEET_ROW_LIMIT', 'xlsxModule', 'sanitizeHtml',
    compile(source).replace("await import('xlsx')", 'xlsxModule') + '\nreturn renderSheet;')(
      { Sheets: { Sheet1: ws } }, sheets, 1000, XLSX, sanitizeHtml)
  assert.match(XLSX.utils.sheet_to_html(ws), /javascript:/)
  await run('Sheet1')
  const root = assertSafe(sheets.value.Sheet1.html)
  assert.match(root.textContent, /linked/)
  assert.match(root.textContent, /plain/)
  assert.ok(root.querySelector('td[colspan="2"]'))
})
