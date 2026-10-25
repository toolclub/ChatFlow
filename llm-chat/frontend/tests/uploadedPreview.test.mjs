import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import * as vue from 'vue'
const url=code=>`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
let sequence=0
async function mountPreview() {
  const key=`previewHarness${++sequence}`
  const requests=[]
  const state=globalThis[key]={vue,props:vue.reactive({modelValue:true,file:{id:1,name:'first.png',size:1}}),cleanup:[],prepare:(_source,id,signal)=>new Promise(resolve=>requests.push({id,signal,resolve}))}
  const runtime=['ref','watch','computed','shallowRef'].map(name=>`export const ${name}=globalThis.${key}.vue.${name};`).join('\n')+`export const onBeforeUnmount=cb=>globalThis.${key}.cleanup.push(cb);`
  const registry=url(`export const pickRenderer=()=>({source:'blobUrl'});export const getUnsupportedHint=()=>'';export const getDisplayLabel=()=>'';`)
  const loader=url(`export const prepare=(...args)=>globalThis.${key}.prepare(...args);`)
  const component=await readFile(new URL('../src/components/UploadedFilePreview.vue',import.meta.url),'utf8')
  let code=ts.transpileModule(component.match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1],{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
  code=code.replace("from 'vue'",`from '${url(runtime)}'`).replace("from '../preview'",`from '${registry}'`).replace("from '../preview/prepare'",`from '${loader}'`)
  code=`const defineProps=()=>globalThis.${key}.props;const defineEmits=()=>()=>{};\n`+code+'\nexport {prepared,loading,error};'
  const module=await import(url(code))
  return {module,state,requests,dispose:()=>state.cleanup.forEach(fn=>fn())}
}
const settle=async()=>{await Promise.resolve();await vue.nextTick()}

test('uploaded preview ignores and revokes late old blobs when selection changes',async()=>{
  const h=await mountPreview()
  let revoked=0
  try {
    h.state.props.file={id:2,name:'second.png',size:2}
    await vue.nextTick()
    assert.equal(h.requests[0].signal.aborted,true)
    h.requests[1].resolve({type:'blobUrl',url:'second',revoke:()=>revoked++})
    await settle()
    h.requests[0].resolve({type:'blobUrl',url:'first',revoke:()=>revoked++})
    await settle()
    assert.equal(h.module.prepared.value.url,'second')
    assert.equal(revoked,1)
    assert.equal(h.module.loading.value,false)
  } finally {h.dispose()}
  assert.equal(revoked,2)
})

test('closing uploaded preview invalidates a still-pending preparation',async()=>{
  const h=await mountPreview()
  let revoked=0
  h.state.props.modelValue=false
  await vue.nextTick()
  assert.equal(h.requests[0].signal.aborted,true)
  h.requests[0].resolve({type:'blobUrl',url:'late',revoke:()=>revoked++})
  await settle()
  assert.equal(h.module.prepared.value,null)
  assert.equal(h.module.loading.value,false)
  assert.equal(revoked,1)
  h.dispose()
})
