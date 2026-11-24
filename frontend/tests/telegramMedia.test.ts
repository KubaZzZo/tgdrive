import assert from 'node:assert/strict'
import { test } from 'node:test'
import { telegramMediaRange } from '../src/utils/telegramMedia.ts'

test('Telegram playback handles Range probes, seeks and suffix requests', () => {
  assert.deepEqual(telegramMediaRange(null, 1024), { start: 0, end: 1024 })
  assert.deepEqual(telegramMediaRange('bytes=0-1', 1024), { start: 0, end: 2 })
  assert.deepEqual(telegramMediaRange('bytes=500-', 1024), { start: 500, end: 1024 })
  assert.deepEqual(telegramMediaRange('bytes=1000-2048', 1024), { start: 1000, end: 1024 })
  assert.deepEqual(telegramMediaRange('bytes=-50', 1024), { start: 974, end: 1024 })
})

test('Telegram playback rejects ranges the media source cannot serve', () => {
  for (const range of ['bytes=0-1,10-20', 'bytes=1024-', 'bytes=-0', 'bytes=9-3', 'bytes=-']) {
    assert.equal(telegramMediaRange(range, 1024), null, range)
  }
})
