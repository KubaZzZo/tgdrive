import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useDriveStore } from '../src/stores/drive.ts'
import type { DriveItem } from '../src/api/types.ts'

Object.assign(globalThis, { window: { setTimeout, clearTimeout, dispatchEvent() {} } })
const item = (id: string): DriveItem => ({
  id,
  name: id + '.jpg',
  parent_id: null,
  kind: 'file',
  category: 'image',
  size: 1,
  mime: 'image/jpeg',
  sha256: null,
  tg_chat_id: null,
  created_at: '',
  updated_at: '',
})
const payload = (items: DriveItem[], total = items.length) =>
  new Response(
    JSON.stringify({
      items,
      total,
      path: [],
      categories: [{ id: 'image', label: '图片', count: total, bytes: total }],
      stats: { files: total, folders: 0, bytes: total },
      limit: 60,
      offset: 0,
    }),
    { headers: { 'content-type': 'application/json' } },
  )
beforeEach(() => setActivePinia(createPinia()))

test('category view queries all folders and reloads preserve the category', async () => {
  const store = useDriveStore()
  const urls: URL[] = []
  globalThis.fetch = async (url) => {
    urls.push(new URL(String(url), 'http://test'))
    return payload([item('photo')])
  }
  await store.openFolder('nested')
  await store.selectView('image')
  await store.load()
  assert.equal(store.folderId, null)
  assert.equal(store.view, 'image')
  assert.equal(urls.at(-1)!.searchParams.get('scope'), 'all')
  assert.equal(urls.at(-1)!.searchParams.get('category'), 'image')
})

test('late response cannot overwrite a newer category selection', async () => {
  const store = useDriveStore()
  let completeFirst!: (response: Response) => void
  let firstSignal: AbortSignal | undefined
  let calls = 0
  globalThis.fetch = async (_url, init) => {
    if (++calls === 1) {
      firstSignal = init?.signal as AbortSignal
      return new Promise((resolve) => {
        completeFirst = resolve
      })
    }
    return payload([item('new')])
  }
  const first = store.selectView('image')
  await store.selectView('video')
  completeFirst(payload([item('old')]))
  await first
  assert.equal(firstSignal?.aborted, true)
  assert.equal(store.view, 'video')
  assert.equal(store.items[0]!.id, 'new')
})

test('pagination, literal search and sort are sent to the server', async () => {
  const store = useDriveStore()
  let last!: URL
  globalThis.fetch = async (url) => {
    last = new URL(String(url), 'http://test')
    return payload([item('x')], 130)
  }
  await store.load()
  await store.setPage(2)
  assert.equal(last.searchParams.get('offset'), '60')
  store.query = '照片 %_'
  store.sortOption = 'size'
  await store.search()
  assert.equal(store.page, 1)
  assert.equal(last.searchParams.get('q'), '照片 %_')
  assert.equal(last.searchParams.get('sort'), 'size')
  assert.equal(last.searchParams.get('scope'), 'all')
  await store.openFolder('nested')
  store.query = 'photo'
  await store.search()
  assert.equal(last.searchParams.get('scope'), 'folder')
  assert.equal(last.searchParams.get('parent_id'), 'nested')
})
