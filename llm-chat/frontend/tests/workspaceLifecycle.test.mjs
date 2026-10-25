import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const dataUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const artifactHelpersUrl = dataUrl(compile(await readFile(new URL('../src/composables/messageArtifacts.ts', import.meta.url), 'utf8')))
const typesUrl = dataUrl(compile(await readFile(new URL('../src/types/index.ts', import.meta.url), 'utf8')))
let sequence = 0
async function load(name, api) {
  const key = `workspaceTest${++sequence}`
  const context = globalThis[key] = { api, dispose: [], notices: [] }
  globalThis.window = globalThis
  const vueUrl = dataUrl(`export const reactive = value => value; export const ref = value => ({value}); export const computed = get => ({get value(){return get()}}); export const watch = () => {}; export const onScopeDispose = cb => globalThis.${key}.dispose.push(cb);`)
  const apiUrl = dataUrl(Object.keys(api).map(method => `export const ${method} = (...args) => globalThis.${key}.api.${method}(...args);`).join('\n'))
  const elementUrl = dataUrl(`export const ElMessage = { warning: text => globalThis.${key}.notices.push(text), error: text => globalThis.${key}.notices.push(text) };`)
  const adminUrl = dataUrl(`export const useAdmin = () => ({verifiedAdminKey:{value:'test'}});`)
  let source = compile(await readFile(new URL(`../src/composables/${name}.ts`, import.meta.url), 'utf8'))
  source = source.replaceAll("from 'vue'", `from '${vueUrl}'`).replaceAll("from '../api'", `from '${apiUrl}'`).replaceAll("from '../types'", `from '${typesUrl}'`).replaceAll("from './useAdmin'", `from '${adminUrl}'`)
  source = source.replaceAll("from './messageArtifacts'", `from '${artifactHelpersUrl}'`)
  source = source.replaceAll("from 'element-plus'", `from '${elementUrl}'`)
  const module = await import(dataUrl(source + `\n// ${key}`))
  return { module, context }
}
const deferred = () => { let resolve; const promise = new Promise(r => { resolve = r }); return { promise, resolve } }
const snapshot = (id, status = 'news_searching') => ({id, status, market:'cn_a', symbol:'000001.SZ', historical_bars:{dates:['2026-01-01'], values:[[1,1,1,1]]}})
function streamingApi() {
  const streams = []
  return { streams, api: {
    fetchPredictionSnapshot: async id => snapshot(id),
    fetchPredictionRecords: async () => [],
    streamPrediction: (_id, _key, handlers, signal) => new Promise(resolve => {
      streams.push({ handlers, signal, finish: () => { handlers.onStopped?.(); resolve() } })
      signal.addEventListener('abort', () => queueMicrotask(() => { handlers.onStopped?.(); resolve() }), {once:true})
    }),
  } }
}

test('old callbacks cannot stop or write into a new connection for the same prediction', async () => {
  const { streams, api } = streamingApi()
  const { module } = await load('usePrediction', api)
  const pred = module.usePrediction()
  pred.viewingId.value = 'a'
  const first = pred.streamPredict('a')
  const second = pred.streamPredict('a')
  await first
  assert.equal(pred.streamingId.value, 'a')
  assert.equal(pred.isPredicting.value, true)
  const candle = {day:1,date:'2026-01-01',o:1,h:2,l:1,c:2}
  streams[0].handlers.onForecastDay(candle)
  assert.equal(pred.forecastCandles.value.length, 0)
  streams[1].handlers.onForecastDay(candle)
  assert.equal(pred.forecastCandles.value.length, 1)
  streams[1].finish()
  await second
})

test('late resume of A cannot change B or replace its active stream', async () => {
  const { streams, api } = streamingApi()
  const resume = deferred()
  api.resumePrediction = () => resume.promise
  api.fetchPredictionSnapshot = async id => snapshot(id, id === 'a' ? 'paused' : 'news_searching')
  const { module } = await load('usePrediction', api)
  const pred = module.usePrediction()
  await pred.openDetail('a')
  const pending = pred.resumePredict()
  await pred.openDetail('b')
  resume.resolve({status:'inferring'})
  await pending
  assert.equal(pred.currentSnapshot.value.id, 'b')
  assert.equal(pred.currentSnapshot.value.status, 'news_searching')
  assert.equal(pred.streamingId.value, 'b')
  assert.equal(streams.length, 1)
  streams.at(-1).finish()
})

test('slow historical chart does not delay restored progress or SSE attachment', async () => {
  const { streams, api } = streamingApi()
  const chart = deferred()
  api.fetchPredictionSnapshot = async id => ({...snapshot(id),historical_bars:null})
  api.fetchStockChart = () => chart.promise
  const { module } = await load('usePrediction', api)
  const pred = module.usePrediction()
  await pred.openDetail('a')
  assert.equal(pred.currentSnapshot.value.id, 'a')
  assert.equal(streams.length, 1)
  chart.resolve({dates:[],values:[]})
  streams.at(-1).finish()
})

test('unmount while screening POST is pending cannot restart polling or analysis', async () => {
  const started = deferred()
  let polls = 0
  const { module, context } = await load('useQuant', {
    runQuantScreen: () => started.promise,
    fetchQuantSnapshot: async () => { polls++; return {status:'DONE',rows:[]} },
  })
  const quant = module.useQuant('cn_a')
  const pending = quant.runScreen()
  context.dispose.forEach(fn => fn())
  started.resolve({snapshot_id:'a'})
  await pending
  assert.equal(polls, 0)
})


test('chat transport failure preserves already generated content', async () => {
  const { module, context } = await load('useChat', {
    sendMessage: async (...args) => { args[6]('已生成的重要内容'); throw new Error('重连失败') },
  })
  const chat = module.useChat()
  chat.currentConvId.value = 'a'
  await chat.send({text:'test',images:[],agentMode:false})
  assert.equal(chat.messages.value.at(-1).content, '已生成的重要内容')
  assert.deepEqual(context.notices, ['重连失败'])
  assert.equal(chat.loading.value, false)
})

test('chat stays busy until server stop handshake and rejects another send', async () => {
  const stopped = deferred()
  let requests = 0
  const { module } = await load('useChat', {
    sendMessage: (...args) => new Promise(resolve => {
      requests++
      args[16].addEventListener('abort', () => { args[15](); resolve() }, {once:true})
    }),
    stopStreamWithToken: () => stopped.promise,
  })
  const chat = module.useChat()
  chat.currentConvId.value = 'a'
  const first = chat.send({text:'test',images:[],agentMode:false})
  const stopping = chat.stopConversation()
  await first
  assert.equal(chat.loading.value, true)
  await chat.send({text:'duplicate',images:[],agentMode:false})
  assert.equal(requests, 1)
  stopped.resolve({can_continue:true,reason:'stopped'})
  await stopping
  assert.equal(chat.loading.value, false)
  assert.equal(chat.canContinue.value, true)
})

function chatApi() {
  const streams = []
  const api = {
    sendMessage: (...args) => new Promise(resolve => streams.push({ args, resolve })),
    fetchConversations: async () => [{ id:'a', title:'A', updated_at:0 }],
  }
  return { api, streams }
}
const file = (id, message_id, content = 'version') => ({id,message_id,name:'index.html',path:'index.html',content,language:'html'})
function completeChat(stream, id, artifacts = []) {
  stream.args[26]?.({assistant_message_id:id,user_message_id:`user-${id}`})
  artifacts.forEach(artifact => stream.args[21](artifact))
  stream.args[14]()
  stream.resolve()
}

test('artifacts stay on the original message after completion, another turn and reload', async () => {
  const { api, streams } = chatApi()
  const { module } = await load('useChat', api)
  const chat = module.useChat()
  chat.currentConvId.value = 'a'
  let pending = chat.send({text:'create',images:[],agentMode:false})
  completeChat(streams[0], 'round-1', [file(1,'round-1','first'), file(1,'round-1','updated')])
  await pending
  const first = chat.messages.value[1]
  const localKey = first.ui_id
  assert.equal(first.message_id,'round-1')
  assert.equal(first.artifacts.length,1)
  assert.equal(first.artifacts[0].content,'updated')
  pending = chat.send({text:'next',images:[],agentMode:false})
  completeChat(streams[1], 'round-2', [file(2,'round-2','second')])
  await pending
  assert.equal(first.ui_id,localKey)
  assert.equal(first.artifacts[0].id,1)
  assert.equal(chat.messages.value[3].artifacts[0].id,2)

  const history = structuredClone(chat.messages.value)
  const refreshed = await load('useChat', {fetchFullState: async () => ({messages:history, artifacts:[], has_streaming:false})})
  globalThis.location = {hash:''}
  const reloaded = refreshed.module.useChat()
  await reloaded.selectConversation('a')
  assert.equal(reloaded.messages.value[1].artifacts[0].message_id,'round-1')
  assert.equal(reloaded.messages.value[3].artifacts[0].message_id,'round-2')
  assert.equal(reloaded.cognitive.value.artifacts.length,2)
})

test('cancelled stream cannot append artifacts or clear loading for a newer turn', async () => {
  const { api, streams } = chatApi()
  const { module } = await load('useChat', api)
  const chat = module.useChat()
  chat.currentConvId.value = 'a'
  const first = chat.send({text:'first',images:[],agentMode:false})
  streams[0].args[26]({assistant_message_id:'first'})
  chat.cancelStream()
  const second = chat.send({text:'second',images:[],agentMode:false})
  streams[1].args[26]({assistant_message_id:'second'})
  streams[0].args[6]('late old text')
  streams[0].args[21](file(1,'first'))
  streams[0].args[14]()
  streams[0].resolve()
  await first
  assert.equal(chat.loading.value,true)
  assert.equal(chat.messages.value[1].content,'')
  assert.equal(chat.messages.value[1].artifacts,undefined)
  completeChat(streams[1],'second',[file(2,'second')])
  await second
  assert.equal(chat.messages.value[3].artifacts[0].id,2)
})

test('late full-state response cannot replace a turn already being generated', async () => {
  const { api, streams } = chatApi()
  const snapshot = deferred()
  api.fetchFullState = () => snapshot.promise
  const { module } = await load('useChat', api)
  globalThis.location = {hash:''}
  const chat = module.useChat()
  const selection = chat.selectConversation('a')
  const pending = chat.send({text:'new',images:[],agentMode:false})
  snapshot.resolve({messages:[],has_streaming:false})
  await selection
  assert.equal(chat.messages.value.length,2)
  completeChat(streams[0],'current',[file(4,'current')])
  await pending
  assert.equal(chat.messages.value[1].artifacts[0].id,4)
})

test('regenerate and revision append real turns and preserve historical artifacts', async () => {
  const { api, streams } = chatApi()
  const { module } = await load('useChat', api)
  globalThis.localStorage = {getItem:()=>null}
  const chat = module.useChat()
  chat.currentConvId.value = 'a'
  const send = chat.send({text:'original',images:[],agentMode:false, files:[{id:5,name:'input.csv',size:1}]})
  completeChat(streams[0],'original',[file(6,'original')])
  await send
  const regenerate = chat.regenerate()
  assert.equal(chat.messages.value.length,4)
  assert.deepEqual(streams[1].args[24],[5])
  completeChat(streams[1],'retry')
  await regenerate
  const revision = chat.editMessage(0,'revised')
  assert.equal(chat.messages.value.length,6)
  assert.equal(chat.messages.value[0].content,'original')
  assert.equal(chat.messages.value[4].content,'revised')
  assert.equal(chat.messages.value[1].artifacts[0].id,6)
  completeChat(streams[2],'revision')
  await revision
})

test('delete failure preserves loaded history and reports the server error', async () => {
  const { module, context } = await load('useChat', {
    fetchFullState:async()=>({messages:[{role:'assistant',message_id:'r',content:'keep'}],has_streaming:false}),
    deleteConversation:async()=>{throw new Error('delete denied')},
  })
  globalThis.location = {hash:''}
  const chat = module.useChat()
  await chat.selectConversation('a')
  await chat.removeConversation('a')
  assert.equal(chat.messages.value[0].content,'keep')
  assert.deepEqual(context.notices,['delete denied'])
})

test('unmount aborts active chat and blocks late callbacks', async () => {
  const { api, streams } = chatApi()
  const { module, context } = await load('useChat', api)
  const chat = module.useChat()
  chat.currentConvId.value='a'
  const pending=chat.send({text:'test',images:[],agentMode:false})
  context.dispose.forEach(dispose=>dispose())
  assert.equal(streams[0].args[16].aborted,true)
  completeChat(streams[0],'late',[file(1,'late')])
  await pending
  assert.equal(chat.messages.value[1].artifacts,undefined)
})

test('resuming an active conversation clears the initial loading overlay immediately', async () => {
  const resumes=[]
  const {module}=await load('useChat',{
    fetchFullState:async()=>({messages:[{role:'assistant',message_id:'active',content:'',stream_completed:false}],has_streaming:true}),
    resumeStream:(...args)=>new Promise(resolve=>resumes.push({args,resolve})),
  })
  globalThis.location={hash:''}
  const chat=module.useChat()
  await chat.selectConversation('a')
  assert.equal(chat.initialLoading.value,false)
  assert.equal(chat.loading.value,true)
  assert.equal(resumes.length,1)
  chat.cancelStream()
  resumes[0].resolve()
})

test('double send while conversation creation is pending creates exactly one turn', async () => {
  const creation=deferred()
  const {api,streams}=chatApi()
  let creates=0
  api.createConversation=()=>{creates++;return creation.promise}
  const {module}=await load('useChat',api)
  globalThis.location={hash:''}
  const chat=module.useChat()
  const first=chat.send({text:'one',images:[],agentMode:false})
  await chat.send({text:'two',images:[],agentMode:false})
  creation.resolve({id:'new'})
  await new Promise(resolve=>setImmediate(resolve))
  assert.equal(creates,1)
  assert.equal(streams.length,1)
  completeChat(streams[0],'only')
  await first
})

test('prediction hydrates checkpoint once and appends only incremental thinking', async () => {
  const { streams, api } = streamingApi()
  const calls = []
  const originalStream = api.streamPrediction
  api.streamPrediction = (...args) => { calls.push(args[4]); return originalStream(...args) }
  const news = {dimension:'macro', query:'q', results:[{title:'a',url:'https://example.test',snippet:'summary',body_available:true}]}
  api.fetchPredictionSnapshot = async id => ({...snapshot(id), event_cursor:777, stream_text:'already generated', news_digest:[news],
    forecast:{dates:['2026-10-01'],point_forecast:[10],p10:[],p50:[],p90:[]}})
  const { module } = await load('usePrediction', api)
  const pred = module.usePrediction()
  await pred.openDetail('a')
  assert.deepEqual(calls, [777])
  assert.equal(pred.thinking.value, 'already generated')
  assert.equal(pred.newsDigest.value.length, 1)
  assert.deepEqual(pred.forecast.value.point_forecast, [10])
  streams[0].handlers.onThinking(' plus new')
  streams[0].handlers.onEventId(778)
  // Pending buffered tokens must be flushed before replacing the transport.
  const reconnect = pred.reconnect()
  assert.equal(pred.thinking.value, 'already generated plus new')
  assert.deepEqual(calls, [777,778])
  streams[1].handlers.onThinking(' after reconnect')
  streams[1].handlers.onPaused()
  await reconnect
  assert.equal(pred.thinking.value, 'already generated plus new after reconnect')
  assert.equal(pred.newsDigest.value.length, 1)
})

test('zero-event snapshot retains saved results and paused thinking without replay', async () => {
  const { streams, api } = streamingApi()
  api.fetchPredictionSnapshot = async id => ({...snapshot(id), event_cursor:0,
    thinking_segments:[{content:'saved'}], news_digest:[{dimension:'macro',results:[]}]})
  const { module } = await load('usePrediction', api)
  const pred = module.usePrediction()
  await pred.openDetail('a')
  assert.equal(pred.newsDigest.value.length, 1)
  streams[0].handlers.onThinking(' new')
  streams[0].handlers.onPaused()
  assert.equal(pred.thinking.value, 'saved new')
})

test('late prediction creation cannot reopen detail after user returns to records', async () => {
  const { streams, api } = streamingApi()
  const create = deferred()
  api.startPrediction = () => create.promise
  const { module } = await load('usePrediction', api)
  const pred = module.usePrediction()
  const pending = pred.startPredict({symbol:'TEST',market:'us_stock'})
  pred.backToList()
  create.resolve({prediction_id:'late'})
  await pending
  assert.equal(pred.viewingId.value, null)
  assert.equal(pred.isPredicting.value, false)
  assert.equal(streams.length, 0)
})

test('partial batch deletion removes only confirmed IDs and preserves failed history',async()=>{
  const {module,context}=await load('useChat',{
    fetchFullState:async id=>({messages:[{role:'assistant',message_id:id,content:`keep ${id}`}],has_streaming:false}),
    batchDeleteConversations:async()=>({ok:true,deleted:1,deleted_ids:['a'],errors:[{conv_id:'b',error:'still stopping'}]}),
    fetchConversations:async()=>[{id:'b',title:'B',updated_at:0}],
  })
  globalThis.location={hash:''}
  const chat=module.useChat()
  await chat.selectConversation('a')
  await chat.selectConversation('b')
  await chat.batchRemoveConversations(['a','b'])
  assert.equal(chat.currentConvId.value,'b')
  assert.equal(chat.messages.value[0].content,'keep b')
  assert.deepEqual(context.notices,['still stopping'])
})

test('prediction polling deduplicates in-flight loads and rejects older-market results', async () => {
  const requests = []
  const { module } = await load('usePrediction', {
    fetchPredictionRecords: market => { const request=deferred(); requests.push({market,...request}); return request.promise },
  })
  const pred = module.usePrediction()
  const first = pred.loadRecords('cn_a')
  const duplicate = pred.loadRecords('cn_a')
  assert.equal(first, duplicate)
  assert.equal(requests.length, 1)
  const newer = pred.loadRecords('us_stock')
  requests[1].resolve([{id:'new'}])
  await newer
  requests[0].resolve([{id:'old'}])
  await first
  assert.deepEqual(pred.records.value, [{id:'new'}])
})

test('unmounted prediction view cannot reopen active session after pending lookup', async () => {
  const active=deferred()
  let disposed=false, loads=0
  const { module } = await load('usePrediction', {
    fetchActivePrediction: () => active.promise,
    fetchPredictionSnapshot: async id => {loads++; return snapshot(id)},
  })
  const pred=module.usePrediction()
  const pending=pred.checkActiveSession(() => !disposed)
  disposed=true
  active.resolve({active:true,prediction_id:'a'})
  await pending
  assert.equal(pred.viewingId.value,null)
  assert.equal(loads,0)
})

test('new conversation returns its committed ID for ownership-sensitive callers',async()=>{
  const {module}=await load('useChat',{
    createConversation:async()=>({id:'new'}),fetchConversations:async()=>[],
  })
  globalThis.location={hash:''}
  const chat=module.useChat()
  assert.equal(await chat.newConversation(),'new')
  assert.equal(chat.currentConvId.value,'new')
})

test('failed creation returns null and preserves the previously selected conversation',async()=>{
  const {module,context}=await load('useChat',{
    createConversation:async()=>{throw new Error('create failed')},
  })
  globalThis.location={hash:''}
  const chat=module.useChat();chat.currentConvId.value='previous'
  assert.equal(await chat.newConversation(),null)
  assert.equal(chat.currentConvId.value,'previous')
  assert.deepEqual(context.notices,['create failed'])
})

test('creation superseded by another selection returns null and never steals the selection',async()=>{
  const created=deferred()
  const {module}=await load('useChat',{
    createConversation:()=>created.promise,
    fetchFullState:async()=>({messages:[],has_streaming:false}),
  })
  globalThis.location={hash:''}
  const chat=module.useChat()
  const pending=chat.newConversation()
  assert.equal(await chat.newConversation(),null)
  await chat.selectConversation('chosen')
  created.resolve({id:'late'})
  assert.equal(await pending,null)
  assert.equal(chat.currentConvId.value,'chosen')
})
