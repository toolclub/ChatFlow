import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = ts.transpileModule(await readFile(new URL('../src/api/http.ts', import.meta.url),'utf8'), {compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText
let serial=0
async function setup() {
  const storage = new Map([['cf_access_token','expired'],['cf_client_id','client']])
  globalThis.localStorage = {getItem:k=>storage.get(k)??null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)}
  return {storage, ...await import(`data:text/javascript;base64,${Buffer.from(source+`\n// ${++serial}`).toString('base64')}`)}
}
const json=(data,status=200)=>new Response(JSON.stringify(data),{status,headers:{'Content-Type':'application/json'}})
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return {promise,resolve}}

test('concurrent expired requests share refresh; late 401 reuses the new token',async()=>{
  const {fetchWithAuth,storage}=await setup()
  const refreshed=deferred(), late=deferred()
  let refreshes=0
  globalThis.fetch=async(url,options)=>{
    if(url.includes('/token/refresh')){refreshes++;return refreshed.promise}
    const token=options.headers.get('Authorization')
    if(token==='Bearer fresh')return json({ok:true})
    if(url==='/late')return late.promise
    return json({},401)
  }
  const a=fetchWithAuth('/a'),b=fetchWithAuth('/b'),c=fetchWithAuth('/late')
  await new Promise(r=>setImmediate(r))
  refreshed.resolve(json({access_token:'fresh'}))
  assert.equal((await a).status,200);assert.equal((await b).status,200)
  late.resolve(json({},401));assert.equal((await c).status,200)
  assert.equal(refreshes,1);assert.equal(storage.get('cf_access_token'),'fresh')
})

test('multipart retry preserves FormData and lets browser set the boundary',async()=>{
  const {fetchWithAuth}=await setup()
  const body=new FormData();body.set('name','file')
  globalThis.fetch=async(url,options)=>{
    if(url.includes('/token/refresh'))return json({access_token:'fresh'})
    assert.equal(options.body,body);assert.equal(options.headers.has('Content-Type'),false)
    return json({},options.headers.get('Authorization')==='Bearer fresh'?200:401)
  }
  assert.equal((await fetchWithAuth('/upload',{method:'POST',body,headers:new Headers({'X-Extra':'yes'})})).status,200)
})

test('aborting a request waiting for shared refresh returns promptly without cancelling other requests',async()=>{
  const {fetchWithAuth}=await setup();const gate=deferred();const controller=new AbortController()
  globalThis.fetch=async(url,options)=>url.includes('/token/refresh')?gate.promise:json({},options.headers.get('Authorization')==='Bearer fresh'?200:401)
  const a=fetchWithAuth('/a',{signal:controller.signal}),b=fetchWithAuth('/b')
  await new Promise(r=>setImmediate(r));controller.abort()
  await assert.rejects(a,{name:'AbortError'})
  gate.resolve(json({access_token:'fresh'}));assert.equal((await b).status,200)
})

test('a late refresh cannot restore a token after logout',async()=>{
  const {fetchWithAuth,setAccessToken,storage}=await setup();const gate=deferred()
  globalThis.fetch=async url=>url.includes('/token/refresh')?gate.promise:json({},401)
  const request=fetchWithAuth('/a');await new Promise(r=>setImmediate(r))
  setAccessToken(null);gate.resolve(json({access_token:'fresh'}))
  assert.equal((await request).status,401);assert.equal(storage.has('cf_access_token'),false)
})

test('temporary refresh failure retains credentials and HTTP failure is never empty success',async()=>{
  const {fetchWithAuth,readJson,storage}=await setup()
  globalThis.fetch=async url=>json({},url.includes('/token/refresh')?503:401)
  await assert.rejects(readJson(await fetchWithAuth('/a')),/401/)
  assert.equal(storage.get('cf_access_token'),'expired')
  await assert.rejects(readJson(json({detail:'删除失败'},500)),/删除失败/)
})

test('late unauthorized POST from another identity is never resubmitted as the new user',async()=>{
 const {fetchWithAuth,setAccessToken}=await setup();const gate=deferred();const calls=[]
 globalThis.fetch=async(url,options)=>{calls.push([url,options.headers?.get('Authorization')]);return gate.promise}
 const request=fetchWithAuth('/api/conversations',{method:'POST',body:'{}'})
 setAccessToken('other-account');gate.resolve(json({},401))
 assert.equal((await request).status,401)
 assert.equal(calls.length,1)
 assert.equal(calls[0][1],'Bearer expired')
})
