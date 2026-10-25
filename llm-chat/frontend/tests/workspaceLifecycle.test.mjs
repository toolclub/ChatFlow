import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const dataUrl = code => `data:text/javascript;base64,${Buffer.from(code).toString('base64')}`
const compile = source => ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
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
