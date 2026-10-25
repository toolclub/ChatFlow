import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import * as vue from 'vue'

let sequence=0
async function mountScroll() {
  const key=`scrollHarness${++sequence}`
  const frames=new Map()
  const observers=[]
  let frameId=0
  const state=globalThis[key]={vue,props:vue.reactive({messages:[],loading:false,currentConvId:'a'}),cleanup:[]}
  const runtime=Object.keys(vue).filter(name=>['ref','watch','computed','nextTick'].includes(name)).map(name=>`export const ${name}=globalThis.${key}.vue.${name};`).join('\n')+`export const onUnmounted=cb=>globalThis.${key}.cleanup.push(cb);`
  const url=code=>`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
  globalThis.localStorage={getItem:()=>null}
  globalThis.requestAnimationFrame=callback=>{frames.set(++frameId,callback);return frameId}
  globalThis.cancelAnimationFrame=id=>frames.delete(id)
  globalThis.ResizeObserver=class {
    constructor(callback){this.callback=callback;this.disconnected=false;observers.push(this)}
    observe(){}
    disconnect(){this.disconnected=true}
  }
  const component=await readFile(new URL('../src/components/ChatView.vue',import.meta.url),'utf8')
  const script=component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
  let code=ts.transpileModule(script,{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
  code=code.replace("from 'vue'",`from '${url(runtime)}'`)
  code=`const defineProps=()=>globalThis.${key}.props; const defineEmits=()=>()=>{};\n`+code+'\nexport {messagesContainer,messagesInner,onMessagesScroll,pauseFollowing};'
  const module=await import(url(code))
  const viewport={scrollTop:400,scrollHeight:500,clientHeight:100}
  module.messagesContainer.value=viewport
  module.messagesInner.value={}
  const flush=async()=>{await vue.nextTick();const callbacks=[...frames.values()];frames.clear();callbacks.forEach(callback=>callback())}
  await flush()
  return {module,viewport,frames,observers,state,flush,dispose:()=>state.cleanup.forEach(fn=>fn())}
}

test('chat follows step/artifact height changes once per frame and preserves manual reading',async()=>{
  const h=await mountScroll()
  try {
    h.viewport.scrollHeight=800
    h.observers.at(-1).callback()
    h.observers.at(-1).callback()
    assert.equal(h.frames.size,1)
    await h.flush()
    assert.equal(h.viewport.scrollTop,800)
    h.viewport.scrollTop=100
    h.module.onMessagesScroll()
    h.viewport.scrollHeight=1000
    h.observers.at(-1).callback()
    await h.flush()
    assert.equal(h.viewport.scrollTop,100)
    h.state.props.loading=true
    await h.flush()
    h.observers.at(-1).callback()
    await h.flush()
    assert.equal(h.viewport.scrollTop,100,'generation start must not override reading position')
  } finally {h.dispose()}
})

test('expanding history pauses following and unmount releases observer and scheduled frame',async()=>{
  const h=await mountScroll()
  h.module.pauseFollowing({target:{closest:()=>({})}})
  const before=h.viewport.scrollTop
  h.viewport.scrollHeight+=500
  h.observers.at(-1).callback()
  await h.flush()
  assert.equal(h.viewport.scrollTop,before)
  h.observers.at(-1).callback()
  h.dispose()
  assert.equal(h.frames.size,0)
  assert.equal(h.observers.at(-1).disconnected,true)
})
