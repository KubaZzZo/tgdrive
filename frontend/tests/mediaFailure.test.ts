import assert from 'node:assert/strict'
import { test } from 'node:test'
import { mediaFailure } from '../src/api/client.ts'

test('reports the storage limit when the media request returns 503', async () => {
  const methods: string[] = []
  globalThis.fetch = async (_url, options) => {
    methods.push(options?.method ?? 'GET')
    assert.equal(new Headers(options?.headers).get('range'), 'bytes=0-0')
    return Response.json({ detail: 'Bot API 下载上限为 20 MB。' }, { status: 503 })
  }
  const error = await mediaFailure('/video', '无法解码', new AbortController().signal)
  assert.equal(error, 'Bot API 下载上限为 20 MB。')
  assert.deepEqual(methods, ['GET'])
})

test('keeps the browser error when the media source serves the requested byte', async () => {
  globalThis.fetch = async (_url, options) => {
    assert.equal(new Headers(options?.headers).get('range'), 'bytes=0-0')
    return new Response('x', { status: 206 })
  }
  assert.equal(await mediaFailure('/video', '无法解码', new AbortController().signal), '无法解码')
})
