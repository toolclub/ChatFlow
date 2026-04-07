/**
 * 代码预览 iframe HTML 构建器
 * 独立放在 .ts 文件，避免 Vue SFC 编译器误解 </script> 标签
 */

// 将用户代码安全嵌入到 <script> 内（防止 </script> 提前截断）
function safeEmbed(code: string): string {
  return JSON.stringify(code).replace(/<\/script>/gi, '<\\/script>')
}

// ── console 可视化公共 CSS
const CONSOLE_STYLE = `
*{box-sizing:border-box;margin:0;padding:0}
body{background:#f8f9fa;padding:14px;font-family:system-ui,sans-serif;font-size:13px}
#out{display:flex;flex-direction:column;gap:3px}
.ln{padding:5px 12px;border-radius:6px;background:#fff;border-left:3px solid #00AEEC;
    font-family:'Fira Code',Consolas,monospace;font-size:12.5px;white-space:pre-wrap;word-break:break-all;line-height:1.5}
.err{border-left-color:#ef4444;color:#ef4444;background:#fff5f5}
.warn{border-left-color:#f59e0b;color:#78350f;background:#fffbeb}
`

// ── console 劫持脚本（不含 <script> 标签，由调用方包裹）
const CONSOLE_SCRIPT = `
const _o=document.getElementById('out')
const _a=(c,...x)=>{const e=document.createElement('div');e.className='ln '+c;
  e.textContent=x.map(v=>v===null?'null':v===undefined?'undefined':typeof v==='object'?JSON.stringify(v,null,2):String(v)).join(' ');
  _o.appendChild(e)}
window.console={log:(...x)=>_a('',   ...x),info:(...x)=>_a('',   ...x),
                warn:(...x)=>_a('warn',...x),error:(...x)=>_a('err',...x),dir:(...x)=>_a('',...x)}
window.onerror=(m,_,l)=>{_a('err','Error: '+m+(l?' (line '+l+')':''));return true}
`

// ────────────────────────────────────────────────────────
// Vue SFC 解析
// ────────────────────────────────────────────────────────
function parseVueSFC(code: string) {
  const templateMatch = code.match(/<template>([\s\S]*?)<\/template>/i)
  const scriptSetupMatch = code.match(/<script\s+setup[^>]*>([\s\S]*?)<\/script>/i)
  const scriptMatch = code.match(/<script(?!\s+setup)[^>]*>([\s\S]*?)<\/script>/i)
  const styleMatch = code.match(/<style[^>]*>([\s\S]*?)<\/style>/i)
  return {
    template: templateMatch?.[1]?.trim() ?? '<div></div>',
    script: (scriptSetupMatch?.[1] ?? scriptMatch?.[1] ?? '').trim(),
    isSetup: !!scriptSetupMatch,
    style: styleMatch?.[1]?.trim() ?? ''
  }
}

function extractTopLevelVars(code: string): string[] {
  const vars = new Set<string>()
  for (const m of code.matchAll(/^\s*(?:const|let|var)\s+([a-zA-Z_$][a-zA-Z0-9_$]*)/gm))
    vars.add(m[1])
  for (const m of code.matchAll(/^\s*function\s+([a-zA-Z_$][a-zA-Z0-9_$]*)/gm))
    vars.add(m[1])
  return [...vars]
}

function stripImports(code: string, ...pkgs: string[]): string {
  let result = code
  for (const pkg of pkgs) {
    const escaped = pkg.replace(/\//g, '\\/')
    result = result.replace(
      new RegExp(`^\\s*import\\s+.*from\\s+['"]${escaped}['"]\\s*;?\\s*$`, 'gm'), ''
    )
  }
  return result
}

// ────────────────────────────────────────────────────────
// 各语言 iframe HTML 构建器
// ────────────────────────────────────────────────────────

export function buildHtml(code: string): string {
  if (/<html[\s>]/i.test(code)) return code
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>*{box-sizing:border-box}body{font-family:system-ui,sans-serif;margin:0;padding:16px}</style>
</head><body>${code}</body></html>`
}

export function buildCss(code: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>*{box-sizing:border-box}body{font-family:system-ui,sans-serif;padding:24px;line-height:1.6;color:#333}
h1{font-size:1.8em;margin-bottom:8px}h2{font-size:1.4em;margin:16px 0 6px}
p{margin:8px 0}ul{margin:8px 0;padding-left:24px}li{margin:4px 0}
input{padding:6px 10px;border:1px solid #ccc;border-radius:4px;margin:4px}
</style>
<style id="u"></style></head>
<body>
<h1>Heading 1</h1><h2>Heading 2</h2>
<p>Paragraph with <a href="#">a link</a> and <strong>bold</strong> text.</p>
<ul>
  <li class="item">List item one</li>
  <li class="item active">Item (active)</li>
  <li class="item">List item three</li>
</ul>
<div style="margin:12px 0;display:flex;gap:8px;flex-wrap:wrap">
  <button class="btn btn-primary">Primary</button>
  <button class="btn btn-secondary">Secondary</button>
  <button class="btn" disabled>Disabled</button>
</div>
<input type="text" placeholder="Text input" class="input"/>
<div class="card"><p class="card-title">Card Title</p><p class="card-body">Card content goes here.</p></div>
<script>document.getElementById('u').textContent=${safeEmbed(code)}</` + `script>
</body></html>`
}

export function buildJs(code: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>${CONSOLE_STYLE}</style></head><body>
<div id="out"></div>
<script>
${CONSOLE_SCRIPT}
try{eval(${safeEmbed(code)})}catch(e){_a('err','Error: '+e.message)}
</` + `script></body></html>`
}

export function buildTs(code: string): string {
  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<script src="https://cdn.jsdelivr.net/npm/@babel/standalone/babel.min.js"></` + `script>
<style>${CONSOLE_STYLE}</style></head><body>
<div id="out"></div>
<script>
${CONSOLE_SCRIPT}
try{
  const _r=Babel.transform(${safeEmbed(code)},{presets:['typescript'],filename:'f.ts'})
  eval(_r.code)
}catch(e){_a('err','Error: '+e.message)}
</` + `script></body></html>`
}

export function buildVue(code: string): string {
  const { template, script, isSetup, style } = parseVueSFC(code)
  const cleanScript = stripImports(script, 'vue')
  const vars = isSetup ? extractTopLevelVars(cleanScript) : []
  const returnStmt = vars.length
    ? 'return{' + vars.map(v => `${v}:typeof ${v}!=="undefined"?${v}:void 0`).join(',') + '}'
    : 'return{}'

  const ERR_DIV = (msg: string) =>
    `'<div style="color:#ef4444;font-family:monospace;padding:12px;font-size:13px">⚠ ${msg}: '+e.message+'</div>'`

  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/vue@3/dist/vue.global.prod.js"></` + `script>
<style>*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:16px}</style>
<style id="us"></style></head><body>
<div id="app"><div style="color:#9ca3af;padding:8px;font-family:system-ui;font-size:13px">正在初始化 Vue…</div></div>
<script>
const{createApp,ref,reactive,computed,watch,watchEffect,onMounted,onUnmounted,
      onBeforeMount,onBeforeUnmount,nextTick,toRef,toRefs,inject,provide,defineComponent,h,readonly}=Vue
document.getElementById('us').textContent=${safeEmbed(style)}
const _cn=['ref','reactive','computed','watch','watchEffect','onMounted','onUnmounted',
           'onBeforeMount','onBeforeUnmount','nextTick','toRef','toRefs','inject','provide','defineComponent','h','readonly']
const _cv=[ref,reactive,computed,watch,watchEffect,onMounted,onUnmounted,
           onBeforeMount,onBeforeUnmount,nextTick,toRef,toRefs,inject,provide,defineComponent,h,readonly]
const _sc=${safeEmbed(cleanScript)}
const _tp=${safeEmbed(template)}
const _rs=${JSON.stringify(returnStmt)}
let _sf=()=>({})
try{_sf=new Function(..._cn,_sc+'\\n'+_rs)}catch(e){
  document.getElementById('app').innerHTML=${ERR_DIV('Compile error')}
}
let _sr={}
try{_sr=_sf(..._cv)||{}}catch(e){
  document.getElementById('app').innerHTML=${ERR_DIV('Runtime error')}
}
try{createApp({template:_tp,setup(){return _sr}}).mount('#app')}catch(e){
  document.getElementById('app').innerHTML=${ERR_DIV('Mount error')}
}
</` + `script></body></html>`
}

export function buildReact(code: string): string {
  const cleanCode = stripImports(code, 'react', 'react-dom', 'react-dom/client')
  const hasRender = /ReactDOM\s*\.\s*(createRoot|render)\s*\(/.test(cleanCode)
  const withRender = cleanCode + (hasRender ? '' : `
;(function(){
  const _C=typeof App!=='undefined'?App
        :typeof Component!=='undefined'?Component
        :typeof Demo!=='undefined'?Demo:null
  if(_C)ReactDOM.createRoot(document.getElementById('root')).render(React.createElement(_C))
  else document.getElementById('root').innerHTML='<p style="color:#9ca3af;font-family:sans-serif;padding:8px">请定义 App、Component 或 Demo 组件</p>'
})()`)

  return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<script src="https://cdn.jsdelivr.net/npm/react@18/umd/react.production.min.js"></` + `script>
<script src="https://cdn.jsdelivr.net/npm/react-dom@18/umd/react-dom.production.min.js"></` + `script>
<script src="https://cdn.jsdelivr.net/npm/@babel/standalone/babel.min.js"></` + `script>
<style>*{box-sizing:border-box}body{font-family:system-ui,-apple-system,sans-serif;margin:0;padding:16px}</style>
</head><body>
<div id="root"><div style="color:#9ca3af;padding:8px;font-family:system-ui;font-size:13px">正在初始化 React…</div></div>
<script>
const{useState,useEffect,useRef,useCallback,useMemo,useContext,useReducer,
      createContext,memo,forwardRef,Fragment,useId,useTransition}=React
window.onerror=(m,_,l)=>{
  document.getElementById('root').innerHTML='<div style="color:#ef4444;font-family:monospace;padding:12px;font-size:13px">⚠ Error: '+m+(l?' (line '+l+')':'')+'</div>'
  return true
}
try{
  const _r=Babel.transform(${safeEmbed(withRender)},{
    presets:['react',['typescript',{allExtensions:true,isTSX:true}]],
    filename:'app.tsx'
  })
  eval(_r.code)
}catch(e){
  document.getElementById('root').innerHTML='<div style="color:#ef4444;font-family:monospace;padding:12px;font-size:13px">⚠ Transform error: '+e.message+'</div>'
}
</` + `script></body></html>`
}

// ── 统一入口
export function buildIframeSrcdoc(code: string, lang: string): string {
  switch (lang) {
    case 'html':
    case 'svg':        return buildHtml(code)
    case 'css':        return buildCss(code)
    case 'javascript':
    case 'js':         return buildJs(code)
    case 'typescript':
    case 'ts':         return buildTs(code)
    case 'vue':        return buildVue(code)
    case 'jsx':
    case 'tsx':
    case 'react':      return buildReact(code)
    default:
      return `<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{font-family:monospace;padding:16px;font-size:13px;white-space:pre-wrap;word-break:break-all}</style>
</head><body>${code.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')}</body></html>`
  }
}
