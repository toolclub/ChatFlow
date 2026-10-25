import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import * as vue from 'vue'
const url=code=>`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
let serial=0
async function mount(upload){
 const key=`uploadTest${++serial}`
 const state=globalThis[key]={props:vue.reactive({currentConvId:'a',loading:false}),cleanup:[],notices:[],vue,upload}
 const runtime=['ref','watch','computed'].map(k=>`export const ${k}=globalThis.${key}.vue.${k};`).join('\n')+`export const onMounted=()=>{};export const onUnmounted=cb=>globalThis.${key}.cleanup.push(cb);`
 const api=url(`export const uploadFile=(...args)=>globalThis.${key}.upload(...args);`)
 const element=url(`export const ElMessage={error:t=>globalThis.${key}.notices.push(t),warning:t=>globalThis.${key}.notices.push(t)};`)
 const component=await readFile(new URL('../src/components/InputBox.vue',import.meta.url),'utf8')
 let code=ts.transpileModule(component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1],{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
 code=code.replace("from 'vue'",`from '${url(runtime)}'`).replace("from '../api'",`from '${api}'`).replace("from 'element-plus'",`from '${element}'`)
 code=`const defineProps=()=>globalThis.${key}.props;const defineEmits=()=>(...args)=>globalThis.${key}.emit?.(...args);const defineExpose=()=>{};\n`+code+'\nexport {pendingFiles,addAttachmentFile,removePendingFile};'
 return {state,...await import(url(code))}
}
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve}}

test('upload from previous conversation is aborted and cannot attach to the new draft',async()=>{
 const gate=deferred();let signal
 const h=await mount((_id,_file,s)=>{signal=s;return gate.promise})
 const pending=h.addAttachmentFile({name:'test.txt',size:10})
 assert.equal(h.pendingFiles.value.length,1)
 h.state.props.currentConvId='b'
 assert.equal(signal.aborted,true);assert.equal(h.pendingFiles.value.length,0)
 gate.resolve({id:1,name:'test.txt',size:10});await pending
 assert.equal(h.pendingFiles.value.length,0)
})

test('removing an in-flight attachment aborts the request and ignores its late result',async()=>{
 const gate=deferred();let signal
 const h=await mount((_id,_file,s)=>{signal=s;return gate.promise})
 const pending=h.addAttachmentFile({name:'test.txt',size:10})
 h.removePendingFile(h.pendingFiles.value[0]._localId)
 assert.equal(signal.aborted,true)
 gate.resolve({id:1,name:'test.txt',size:10});await pending
 assert.equal(h.pendingFiles.value.length,0)
 h.state.cleanup.forEach(fn=>fn())
})

test('an existing conversation selected during creation is not the upload destination',async()=>{
 let requests=0,complete
 const h=await mount(async()=>{requests++;return {id:1}})
 h.state.props.currentConvId=null
 // Expose the emitted creator callback without substituting current props.
 h.state.emit=(_event,cb)=>{complete=cb}
 const pending=h.addAttachmentFile({name:'test.txt',size:10})
 h.state.props.currentConvId='existing'
 complete('created-for-upload')
 await pending
 assert.equal(requests,0)
})
