import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'
const data=code=>`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const vue=data('export const ref=value=>({value});')
const source=ts.transpileModule(await readFile(new URL('../src/composables/useArtifactPreview.ts',import.meta.url),'utf8'),{compilerOptions:{target:ts.ScriptTarget.ES2022,module:ts.ModuleKind.ES2022}}).outputText.replace("from 'vue'",`from '${vue}'`)
const {useArtifactPreview}=await import(data(source))
const deferred=()=>{let resolve;const promise=new Promise(r=>resolve=r);return{promise,resolve}}

test('late preview of an older same-name artifact cannot replace the current version',async()=>{
 const first=deferred(),second=deferred(),signals=[]
 const preview=useArtifactPreview(()=> 'conv',(id,signal)=>{signals.push(signal);return id===1?first.promise:second.promise},()=>assert.fail('unexpected error'))
 const a=preview.select({id:1,name:'index.html',language:'html'})
 const b=preview.select({id:2,name:'index.html',language:'html'})
 assert.equal(signals[0].aborted,true)
 second.resolve({id:2,name:'index.html',content:'new'});await b
 first.resolve({id:1,name:'index.html',content:'old'});await a
 assert.equal(preview.selectedFile.value.content,'new')
 assert.equal(preview.loading.value,false)
})

test('changing conversation or opening an archive invalidates pending previews',async()=>{
 let conv='a';const gate=deferred()
 const preview=useArtifactPreview(()=>conv,()=>gate.promise,()=>assert.fail('unexpected error'))
 const request=preview.select({id:1,name:'a.html'})
 conv='b';preview.clear()
 await preview.select({id:2,name:'archive.zip',language:'archive'})
 gate.resolve({id:1,content:'old'});await request
 assert.equal(preview.selectedFile.value.id,2);assert.equal(preview.loading.value,false)
})
