import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const dataUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const compile = source => ts.transpileModule(source, {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
const types = dataUrl(compile(await readFile(new URL('../src/types/index.ts',import.meta.url),'utf8')))
const component = (await readFile(new URL('../src/components/PredictionView.vue',import.meta.url),'utf8')).match(/<script setup lang="ts">([\s\S]*?)<\/script>/)[1]
const deferred = () => { let resolve;const promise=new Promise(r=>{resolve=r});return {promise,resolve} }
let version=0
async function load(pred) {
  const key=`predictionView${++version}`
  const context=globalThis[key]={pred,mount:[],unmount:[],timers:0}
  globalThis.window={setInterval:()=>{context.timers++;return 1}}
  const vue=dataUrl(`export const ref=v=>({value:v});export const computed=get=>({get value(){return get()}});export const watch=()=>{};export const nextTick=()=>Promise.resolve();export const onMounted=fn=>globalThis.${key}.mount.push(fn);export const onUnmounted=fn=>globalThis.${key}.unmount.push(fn);`)
  const prediction=dataUrl(`export const usePrediction=()=>globalThis.${key}.pred;`)
  const admin=dataUrl('export const useAdmin=()=>({isVerified:{value:true}});')
  let code=compile(component)
  code=code.replaceAll("from 'vue'",`from '${vue}'`).replaceAll("from '../composables/usePrediction'",`from '${prediction}'`).replaceAll("from '../composables/useAdmin'",`from '${admin}'`).replaceAll("from '../types'",`from '${types}'`)
  await import(dataUrl(`const defineEmits=()=>()=>{};\n${code}\n//${key}`))
  return context
}

test('unmount during initial prediction list load never installs polling timers',async()=>{
  const records=deferred();let activeCalls=0
  const ctx=await load({loadRecords:()=>records.promise,checkActiveSession:async()=>{activeCalls++}})
  const mounting=ctx.mount[0]()
  ctx.unmount.forEach(fn=>fn())
  records.resolve()
  await mounting
  assert.equal(activeCalls,0)
  assert.equal(ctx.timers,0)
})

test('unmount during prediction active lookup invalidates callback and skips timers',async()=>{
  const active=deferred();let isCurrent
  const ctx=await load({loadRecords:async()=>{},checkActiveSession:current=>{isCurrent=current;return active.promise}})
  const mounting=ctx.mount[0]()
  await new Promise(r=>setImmediate(r))
  assert.equal(isCurrent(),true)
  ctx.unmount.forEach(fn=>fn())
  assert.equal(isCurrent(),false)
  active.resolve()
  await mounting
  assert.equal(ctx.timers,0)
})
