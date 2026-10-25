import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
import * as vue from 'vue'

const url = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const deferred = () => { let resolve, reject; const promise = new Promise((a,b) => { resolve=a; reject=b }); return {promise,resolve,reject} }
let serial = 0
async function mount(api) {
  const key = `authLifecycle${++serial}`
  const context = globalThis[key] = {api, vue, tokens:[]}
  const vueUrl = url(['ref','computed','readonly'].map(name => `export const ${name}=globalThis.${key}.vue.${name};`).join('\n'))
  const apiUrl = url(['fetchMe','logout'].map(name => `export const ${name}=(...args)=>globalThis.${key}.api.${name}(...args);`).join('\n'))
  const httpUrl = url(`export const setAccessToken=token=>globalThis.${key}.tokens.push(token);`)
  let source = ts.transpileModule(await readFile(new URL('../src/composables/useAuth.ts',import.meta.url),'utf8'), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
  source = source.replace("from 'vue'",`from '${vueUrl}'`).replace("from '../api/auth'",`from '${apiUrl}'`).replace("from '../api/http'",`from '${httpUrl}'`)
  const {useAuth} = await import(url(source))
  return {auth:useAuth(),context}
}
const profile = id => ({user:{id,name:id},settings:{theme:'light'}})

test('concurrent auth initialization shares one profile request',async()=>{
  const response=deferred(); let calls=0
  const {auth}=await mount({fetchMe:()=>{calls++;return response.promise}})
  const first=auth.init(),second=auth.init()
  assert.equal(first,second);assert.equal(calls,1)
  response.resolve(profile('a'));await first
  assert.equal(auth.user.value.id,'a');assert.equal(auth.loading.value,false)
})

test('profile response arriving after logout cannot restore signed-in UI',async()=>{
  const response=deferred()
  const {auth,context}=await mount({fetchMe:()=>response.promise,logout:async()=>{}})
  const pending=auth.init()
  await auth.logout()
  response.resolve(profile('old'));await pending
  assert.equal(auth.user.value,null);assert.equal(auth.settings.value,null)
  assert.equal(auth.loading.value,false);assert.deepEqual(context.tokens,[null])
})

test('old init failure cannot erase a successful new login',async()=>{
  const old=deferred();let calls=0
  const {auth,context}=await mount({fetchMe:()=>++calls===1?old.promise:Promise.resolve(profile('new'))})
  const pending=auth.init()
  assert.equal(await auth.handleAuthSuccess('new-token'),true)
  old.reject(new Error('old unauthorized'));await pending
  assert.equal(auth.user.value.id,'new');assert.equal(auth.initialized.value,true)
  assert.deepEqual(context.tokens,['new-token'])
})

test('late logout completion cannot clear a newer login token or profile',async()=>{
  const stopped=deferred()
  const {auth,context}=await mount({fetchMe:async()=>profile('new'),logout:()=>stopped.promise})
  const pending=auth.logout()
  assert.equal(await auth.handleAuthSuccess('new-token'),true)
  stopped.resolve();await pending
  assert.equal(auth.user.value.id,'new');assert.deepEqual(context.tokens,['new-token'])
})

test('superseded login callback reports failure without replacing the newer profile',async()=>{
  const old=deferred();let calls=0
  const {auth}=await mount({fetchMe:()=>++calls===1?old.promise:Promise.resolve(profile('new'))})
  const pending=auth.handleAuthSuccess('old-token')
  assert.equal(await auth.handleAuthSuccess('new-token'),true)
  old.resolve(profile('old'))
  assert.equal(await pending,false);assert.equal(auth.user.value.id,'new')
})
