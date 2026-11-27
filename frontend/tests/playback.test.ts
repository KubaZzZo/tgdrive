import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { mediaTime, playbackPosition, savePlayback } from '../src/utils/playback.ts'
const values = new Map<string, string>()
Object.assign(globalThis, {
  localStorage: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  },
})
beforeEach(() => values.clear())
test('playback progress restores valid positions and clears near the end', () => {
  savePlayback('video', 34, 120)
  assert.equal(playbackPosition('video', 120), 34)
  assert.equal(playbackPosition('video', 20), 0)
  savePlayback('video', 119, 120)
  assert.equal(playbackPosition('video', 120), 0)
})
test('progress handles invalid storage and limits history to 100 files', () => {
  values.set('tgdrive_playback_v1', 'bad json')
  assert.equal(playbackPosition('x', 100), 0)
  for (let i = 0; i < 110; i++) savePlayback(String(i), 10, 100)
  assert.equal(Object.keys(JSON.parse(values.get('tgdrive_playback_v1')!)).length, 100)
  savePlayback('invalid', NaN, 100)
  assert.equal(playbackPosition('invalid', 100), 0)
})
test('media time formats long videos and incomplete metadata', () => {
  assert.equal(mediaTime(3661), '1:01:01')
  assert.equal(mediaTime(65), '1:05')
  assert.equal(mediaTime(Infinity), '0:00')
})
test('scoped progress migrates legacy records and keeps channels independent', () => {
  savePlayback('video', 34, 120)
  assert.equal(playbackPosition('["a","video"]', 120, 'video'), 34)
  savePlayback('["a","video"]', 45, 120, 'video')
  assert.equal(playbackPosition('["a","video"]', 120, 'video'), 45)
  assert.equal(playbackPosition('["b","video"]', 120, 'video'), 0)
  assert.equal(playbackPosition('video', 120), 0)
})
