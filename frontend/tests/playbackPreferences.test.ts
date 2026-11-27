import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { playbackPreference, resolvePlaybackMode, saveDefaultPlaybackMode, savePlaybackPreference, videoPlaybackKey } from '../src/utils/playbackPreferences.ts'

const values = new Map<string, string>()
const key = 'tgdrive_playback_preferences_v1'
beforeEach(() => {
  values.clear()
  Object.assign(globalThis, { localStorage: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
  } })
})

test('video preferences override the global default and are scoped to the workspace', () => {
  const video = videoPlaybackKey('channel-a', 'video')
  const other = videoPlaybackKey('channel-b', 'video')
  assert.deepEqual(playbackPreference(video), { preferred: null, defaultMode: 'server' })
  saveDefaultPlaybackMode('telegram')
  savePlaybackPreference(video, 'server')
  assert.deepEqual(playbackPreference(video), { preferred: 'server', defaultMode: 'telegram' })
  assert.deepEqual(playbackPreference(other), { preferred: null, defaultMode: 'telegram' })
  savePlaybackPreference(video, null)
  assert.equal(playbackPreference(video).preferred, null)
})

test('direct playback requires both a preference and current eligibility', () => {
  assert.equal(resolvePlaybackMode(null, 'server', true), 'server')
  assert.equal(resolvePlaybackMode(null, 'telegram', true), 'telegram')
  assert.equal(resolvePlaybackMode('server', 'telegram', true), 'server')
  assert.equal(resolvePlaybackMode('telegram', 'server', true), 'telegram')
  assert.equal(resolvePlaybackMode('telegram', 'telegram', false), 'server')
})

test('invalid storage is ignored and preferences remain bounded', () => {
  for (const data of ['invalid', 'null', '{"videos":null}', '{"defaultMode":"bad","videos":{"video":"bad"}}']) {
    values.set(key, data)
    assert.deepEqual(playbackPreference('video'), { preferred: null, defaultMode: 'server' })
  }
  for (let i = 0; i < 305; i++) savePlaybackPreference(String(i), 'telegram')
  assert.equal(Object.keys(JSON.parse(values.get(key)!).videos).length, 300)
  assert.equal(playbackPreference('0').preferred, null)
  assert.equal(playbackPreference('304').preferred, 'telegram')
})

test('blocked browser storage never prevents opening or switching playback', () => {
  Object.assign(globalThis, { localStorage: {
    getItem: () => { throw new Error('blocked') },
    setItem: () => { throw new Error('blocked') },
  } })
  savePlaybackPreference('video', 'telegram')
  saveDefaultPlaybackMode('telegram')
  assert.deepEqual(playbackPreference('video'), { preferred: null, defaultMode: 'server' })
})
