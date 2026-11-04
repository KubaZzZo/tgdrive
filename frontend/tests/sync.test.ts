import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useSyncStore } from '../src/stores/sync.ts'
import { useDriveStore } from '../src/stores/drive.ts'
Object.assign(globalThis, {
  window: { setTimeout, clearTimeout, dispatchEvent() {} },
  document: { removeEventListener() {}, addEventListener() {}, visibilityState: 'visible' },
})
const json = (value: unknown) =>
  new Response(JSON.stringify(value), { headers: { 'content-type': 'application/json' } })
const status = (revision: number) => ({
  enabled: true,
  state: 'idle',
  running: false,
  scanned: 2,
  imported: 1,
  files: 1,
  revision,
  last_success: null,
  error: null,
  interval: 30,
})
beforeEach(() => setActivePinia(createPinia()))
test('channel revisions refresh the active category without navigating away', async () => {
  let revision = 0
  let itemRequests = 0
  let last = ''
  globalThis.fetch = async (url) => {
    if (String(url) === '/api/sync') return json(status(revision))
    itemRequests++
    last = String(url)
    return json({
      items: [],
      path: [],
      stats: { files: 0, folders: 0, bytes: 0 },
      categories: [],
      total: 0,
    })
  }
  const drive = useDriveStore()
  const sync = useSyncStore()
  await drive.selectView('video')
  await sync.refresh()
  assert.equal(itemRequests, 1)
  revision = 1
  await sync.refresh()
  assert.equal(itemRequests, 2)
  assert.equal(drive.view, 'video')
  assert.ok(last.includes('category=video'))
  await sync.refresh()
  assert.equal(itemRequests, 2)
})
test('logout discards an in-flight status response', async () => {
  let resolve!: (value: Response) => void
  globalThis.fetch = async () =>
    new Promise((done) => {
      resolve = done
    })
  const sync = useSyncStore()
  const request = sync.refresh()
  sync.stop()
  resolve(json(status(2)))
  await request
  assert.equal(sync.status, null)
})
