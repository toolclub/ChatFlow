import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import ts from 'typescript'

const source = await readFile(new URL('../src/api/chatStream.ts', import.meta.url), 'utf8')
const compiled = ts.transpileModule(source, { compilerOptions: { target: ts.ScriptTarget.ES2022, module: ts.ModuleKind.ES2022 } }).outputText
const { consumeChatStream, StreamHttpError } = await import(`data:text/javascript;base64,${Buffer.from(compiled).toString('base64')}`)
const encoder = new TextEncoder()
const event = (data) => `data: ${JSON.stringify(data)}\n\n`
function response(...parts) {
  return new Response(new ReadableStream({
    start(controller) { parts.forEach(part => controller.enqueue(encoder.encode(part))); controller.close() },
  }))
}

test('reconnects with durable round cursor, deduplicates replay, never repeats initial request', async () => {
  let starts = 0
  const events = []
  const result = await consumeChatStream({
    initial: async () => { starts++; return response(event({ id: 10, message_id: 'round-1', content: 'a' })) },
    reconnect: async (cursor, id) => {
      assert.equal(cursor, 10); assert.equal(id, 'round-1')
      return response(event({ id: 10, content: 'a' }), event({ id: 11, content: 'b' }), event({ id: 12, done: true }))
    },
    onEvent: data => events.push(data), retryDelayMs: 0,
  })
  assert.equal(result, 'done'); assert.equal(starts, 1)
  assert.deepEqual(events.map(e => e.id), [10, 11, 12])
})

test('handles CRLF, split bytes, SSE id and a partial event without applying it twice', async () => {
  const events = []
  await consumeChatStream({
    initial: async () => response('id: 3\r\ndata: {"message_id":"r","content":', '"好"}\r\n\r\ndata: {"id":4,"content":"partial'),
    reconnect: async cursor => {
      assert.equal(cursor, 3)
      return response(event({ id: 4, content: 'complete' }), event({ id: 5, done: true }))
    }, onEvent: data => events.push(data), retryDelayMs: 0,
  })
  assert.deepEqual(events.filter(e => e.content).map(e => e.content), ['好', 'complete'])
})

test('error followed by done stays interrupted so Continue remains available', async () => {
  const result = await consumeChatStream({
    initial: async () => response(event({ error: 'provider timeout', can_continue: true }), event({ done: true })),
    reconnect: () => assert.fail('must not reconnect terminal response'), onEvent() {},
  })
  assert.equal(result, 'interrupted')
})

test('HTTP authorization errors surface without retry or false success', async () => {
  await assert.rejects(consumeChatStream({
    initial: async () => new Response(JSON.stringify({ detail: 'Forbidden' }), { status: 403 }),
    reconnect: () => assert.fail('403 must not retry'), onEvent() {},
  }), error => error instanceof StreamHttpError && error.status === 403)
})

test('explicit abort cancels the body reader and does not reconnect', async () => {
  const controller = new AbortController()
  let cancelled = false
  const result = await consumeChatStream({
    initial: async () => new Response(new ReadableStream({
      start(stream) { stream.enqueue(encoder.encode(event({ id: 1, message_id: 'r', content: 'x' }))) },
      cancel() { cancelled = true },
    })),
    reconnect: () => assert.fail('abort must not reconnect'),
    onEvent() { controller.abort() }, signal: controller.signal,
  })
  assert.equal(result, 'stopped'); assert.equal(cancelled, true)
})

test('silent socket is cancelled and recovered; retry exhaustion is visible', async () => {
  let cancelled = false
  await assert.rejects(consumeChatStream({
    initial: async () => new Response(new ReadableStream({ cancel() { cancelled = true } })),
    messageId: 'r', idleTimeoutMs: 5, retryDelayMs: 0, maxRetries: 1,
    reconnect: async () => response(), onEvent() {},
  }), /连接恢复失败/)
  assert.equal(cancelled, true)
})

test('unacknowledged send failure is never automatically submitted again', async () => {
  await assert.rejects(consumeChatStream({
    initial: async () => { throw new TypeError('Failed to fetch') },
    reconnect: () => assert.fail('no acknowledged round'), onEvent() {},
  }), /Failed to fetch/)
})

test('safe resume GET retries its first network failure using the known round', async () => {
  let retries = 0
  const result = await consumeChatStream({
    initial: async () => { throw new TypeError('temporary network error') },
    initialRequestSafe: true, messageId: 'known-round', cursor: 9, retryDelayMs: 0,
    reconnect: async (cursor, id) => {
      retries++; assert.equal(cursor, 9); assert.equal(id, 'known-round')
      return response(event({ id: 10, done: true }))
    }, onEvent() {},
  })
  assert.equal(result, 'done'); assert.equal(retries, 1)
})
