import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const source=await readFile(new URL('../src/composables/messageArtifacts.ts',import.meta.url),'utf8')
const code=ts.transpileModule(source,{compilerOptions:{module:ts.ModuleKind.ES2022,target:ts.ScriptTarget.ES2022}}).outputText
const {attachMessageArtifact,upsertArtifact,artifactKey}=await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`)
const file=(id,message_id,content='original')=>({id,message_id,name:'same.html',path:'same.html',content,language:'html'})

test('same filename in different messages keeps distinct artifact IDs',()=>{
  const a={role:'assistant',message_id:'a'},b={role:'assistant',message_id:'b'}
  attachMessageArtifact([a,b],a,file(1,'a'))
  attachMessageArtifact([a,b],b,file(2,'b'))
  attachMessageArtifact([a,b],b,file(1,'a','old event update'))
  assert.equal(a.artifacts[0].content,'old event update')
  assert.equal(b.artifacts[0].id,2)
  assert.notEqual(artifactKey(a.artifacts[0]),artifactKey(b.artifacts[0]))
})

test('unknown explicit owner never gets attached to another persisted message',()=>{
  const message={role:'assistant',message_id:'current'}
  assert.equal(attachMessageArtifact([message],message,file(1,'unknown')),false)
  assert.equal(message.artifacts,undefined)
})

test('metadata replay preserves loaded content and upgrades legacy identity',()=>{
  const artifacts=[file(undefined,'a')]
  upsertArtifact(artifacts,{id:5,message_id:'a',name:'same.html',path:'same.html',language:'html',size:100})
  assert.equal(artifacts.length,1)
  assert.equal(artifacts[0].id,5)
  assert.equal(artifacts[0].content,'original')
})
