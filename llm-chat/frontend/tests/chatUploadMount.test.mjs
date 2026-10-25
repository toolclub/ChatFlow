import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as vue from 'vue'

const url = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const deferred = () => { let resolve; const promise = new Promise(r => { resolve=r }); return {promise,resolve} }
let serial = 0

async function mountChat() {
  const key = `chatUploadMount${++serial}`
  const state = globalThis[key] = {vue, calls:[], notices:[]}
  const runtime = url(Object.keys(vue).filter(k=>/^[a-zA-Z_$][\w$]*$/.test(k)).map(k=>
    `export const ${k}=${['vModelText','vShow'].includes(k)?'{}':`globalThis.${key}.vue.${k}`};`).join('\n'))
  const stub = url(`import {h} from '${runtime}';export default {setup:(_,ctx)=>()=>h('stub',null,ctx.slots.default?.())};`)
  const icons = url(`export const Check=()=>null,Loading=()=>null,Picture=()=>null,Promotion=()=>null;`)
  const element = url(`export const ElMessage={error:t=>globalThis.${key}.notices.push(t),warning:t=>globalThis.${key}.notices.push(t)};`)
  const api = url(`export const uploadFile=async(id,file,signal)=>{globalThis.${key}.calls.push({id,file,signal});return {id:101,name:file.name,size:file.size}};`)
  async function component(name, replacements={}) {
    let source = await readFile(new URL(`../src/components/${name}.vue`,import.meta.url),'utf8')
    // Test-only access to the real input's async operation and draft state.
    if(name==='InputBox') source=source.replace('defineExpose({ openCapsule })','defineExpose({ openCapsule, addAttachmentFile, pendingFiles, input })')
    const descriptor=parse(source,{filename:`${name}.vue`}).descriptor
    let code=ts.transpileModule(compileScript(descriptor,{id:key+name,inlineTemplate:true}).content,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
    const modules={'vue':runtime,'../api':api,'element-plus':element,'@element-plus/icons-vue':icons,
      './UploadedFilePreview.vue':stub,'./MessageItem.vue':stub,'./ClarificationCard.vue':stub,...replacements}
    code=code.replace(/from (["'])([^"']+)\1/g,(all,_quote,path)=>modules[path]?`from '${modules[path]}'`:all)
    return url(code)
  }
  const inputUrl=await component('InputBox')
  const InputBox=(await import(inputUrl)).default
  const ChatView=(await import(await component('ChatView',{'./InputBox.vue':inputUrl}))).default
  const node=(type,text='')=>({type,text,parent:null,children:[],props:{},style:{}})
  const renderer=vue.createRenderer({
    createElement:type=>node(type),createText:text=>node('text',text),createComment:text=>node('comment',text),
    setText:(n,text)=>{n.text=text},setElementText:(n,text)=>{n.text=text;n.children=[]},
    parentNode:n=>n.parent,nextSibling:n=>n.parent?.children[n.parent.children.indexOf(n)+1]||null,
    patchProp:(n,key,_old,value)=>{n.props[key]=value},
    insert(n,parent,anchor=null){if(n.parent){const i=n.parent.children.indexOf(n);if(i>=0)n.parent.children.splice(i,1)}n.parent=parent;const i=anchor?parent.children.indexOf(anchor):-1;if(i<0)parent.children.push(n);else parent.children.splice(i,0,n)},
    insertStaticContent(text,parent,anchor){const n=node('static',text);n.parent=parent;const i=anchor?parent.children.indexOf(anchor):-1;if(i<0)parent.children.push(n);else parent.children.splice(i,0,n);return [n,n]},
    remove(n){if(n.parent){const i=n.parent.children.indexOf(n);if(i>=0)n.parent.children.splice(i,1);n.parent=null}},
  })
  globalThis.localStorage={getItem:()=>null}
  globalThis.window={requestIdleCallback:()=>{}}
  globalThis.requestAnimationFrame=()=>1
  globalThis.cancelAnimationFrame=()=>{}
  globalThis.ResizeObserver=class {observe(){}disconnect(){}}
  const props=vue.reactive({messages:[],loading:false,creating:false,currentConvId:null,
    agentStatus:{state:'idle',model:''},cognitive:{plan:[],traceLog:[],historyEvents:[],artifacts:[]}})
  const created=deferred()
  let pending=null,creations=0
  const app=renderer.createApp({setup:()=>()=>vue.h(ChatView,{...props,onEnsureConv:async complete=>{
    if(!pending){creations++;props.creating=true;pending=created.promise.then(async id=>{props.currentConvId=id;props.creating=false;await vue.nextTick();return id})}
    complete(await pending)
  }})})
  app.config.warnHandler=()=>{}
  const host=node('root')
  app.mount(host)
  function find(vnode){
    if(!vnode)return null
    if(vnode.type===InputBox)return vnode.component
    if(vnode.component){const found=find(vnode.component.subTree);if(found)return found}
    if(Array.isArray(vnode.children))for(const child of vnode.children){const found=find(child);if(found)return found}
    return null
  }
  return {app,props,state,created,input:()=>find(app._instance.subTree),creations:()=>creations}
}

test('first attachments survive ChatView creation overlay and null-to-created conversation transition',async()=>{
  const h=await mountChat()
  try {
    const input=h.input()
    input.exposed.input.value='keep my draft'
    const first=input.exposed.addAttachmentFile({name:'first.txt',size:10})
    const second=input.exposed.addAttachmentFile({name:'second.txt',size:20})
    await vue.nextTick()
    assert.equal(h.props.creating,true)
    assert.equal(h.input(),input,'creating must not unmount the awaiting uploader')
    assert.equal(h.creations(),1)
    h.created.resolve('created')
    await Promise.all([first,second]);await vue.nextTick()
    assert.equal(h.input(),input,'assigning the first conversation ID preserves the same draft')
    assert.equal(input.exposed.input.value,'keep my draft')
    assert.deepEqual(h.state.calls.map(call=>[call.id,call.file.name]),[['created','first.txt'],['created','second.txt']])
    assert.equal(input.exposed.pendingFiles.value.length,2)
    assert.ok(input.exposed.pendingFiles.value.every(file=>!file.uploading && file.id===101))
    assert.deepEqual(h.state.notices,[])
  } finally {h.app.unmount()}
})
