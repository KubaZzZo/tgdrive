import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useUploadStore } from '../src/stores/uploads.ts'
import { uploadChunk } from '../src/api/client.ts'
import { useAuthStore } from '../src/stores/auth.ts'
import type { Meta } from '../src/api/types.ts'

const memory = new Map<string, string>()
Object.assign(globalThis, {
  window: { setTimeout, clearTimeout, dispatchEvent() {} },
  localStorage: {
    getItem: (key: string) => memory.get(key) ?? null,
    setItem: (key: string, value: string) => memory.set(key, value),
  },
})
const json = (data: unknown, status = 200) => new Response(JSON.stringify(data), {
  status, headers: { 'content-type': 'application/json' },
})
const file = () => new File(['abcdefgh'], 'data.bin', { lastModified: 123 })
async function until(predicate: () => boolean) {
  const deadline = Date.now() + 3000
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error('State transition timed out')
    await new Promise((r) => setTimeout(r, 5))
  }
}
beforeEach(() => { memory.clear(); setActivePinia(createPinia()) })

test('switching channels isolates resume IDs and preserves the original channel resume map', async () => {
  memory.set('tgdrive_resume_v2', JSON.stringify({ '|data.bin|8|123': 'original-upload' }))
  const auth = useAuthStore()
  auth.meta = { workspace_is_default: false, storage: { chat_id: '-100222' } } as Meta
  let creates = 0
  let originalLookups = 0
  globalThis.fetch = async (url, init) => {
    if (String(url).includes('original-upload')) originalLookups++
    if (url === '/api/uploads') { creates++; return json({ id: 'new-channel-upload', offset: 8, chunk_size: 4, size: 8 }) }
    if (init?.method === 'POST') return json({ ok: true }, 202)
    return json({ id: 'new-channel-upload', state: 'done', progress: 8 })
  }
  const store = useUploadStore()
  store.enqueue([file()], null)
  await until(() => store.jobs[0]?.state === 'done')
  assert.equal(creates, 1)
  assert.equal(originalLookups, 0)
  assert.equal(JSON.parse(memory.get('tgdrive_resume_v2')!)['|data.bin|8|123'], 'original-upload')
  assert.deepEqual(JSON.parse(memory.get('tgdrive_resume_v2:-100222')!), {})
})

test('409 without offset is an error, not a reset to zero', async () => {
  globalThis.fetch = async () => json({ detail: 'processing' }, 409)
  const result = await uploadChunk('one', 4, new Blob(['efgh']))
  assert.equal(result.kind, 'error')
})

test('pause/resume keeps upload id and asks server for the committed offset', async () => {
  const store = useUploadStore()
  let creates = 0
  let patches = 0
  let resumedOffset = ''
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') {
      creates++
      return json({ id: 'one', offset: 0, chunk_size: 4, size: 8 })
    }
    if (init?.method === 'PATCH') {
      patches++
      if (patches === 1) {
        return new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
      }
      resumedOffset = new Headers(init.headers).get('Upload-Offset') ?? ''
      return json({ offset: 8, received: 8, size: 8 })
    }
    if (init?.method === 'POST') return json({ ok: true }, 202)
    return json({ id: 'one', name: 'data.bin', parent_id: null, size: 8, received: 4,
      chunk_size: 4, progress: 8, state: patches > 1 ? 'done' : 'uploading' })
  }
  store.enqueue([file()], null)
  await until(() => patches === 1)
  store.pause(store.jobs[0]!)
  assert.equal(store.jobs[0]!.state, 'paused')
  store.resume(store.jobs[0]!) // Resume before the aborted request has unwound.
  await until(() => store.jobs[0]!.state === 'done')
  assert.equal(creates, 1)
  assert.equal(resumedOffset, '4')
  assert.equal(store.totalProgress, 100)
})

test('retry observes an already processing upload instead of creating a duplicate', async () => {
  const store = useUploadStore()
  memory.set('tgdrive_resume_v2', JSON.stringify({ '|data.bin|8|123': 'existing' }))
  let gets = 0
  globalThis.fetch = async (_url, init) => {
    assert.ok(!init?.method || init.method === 'GET')
    gets++
    return json({ id: 'existing', name: 'data.bin', parent_id: null, size: 8,
      received: 8, chunk_size: 4, progress: 8, state: gets === 1 ? 'processing' : 'done' })
  }
  store.enqueue([file()], null)
  await until(() => store.jobs[0]!.state === 'done')
  assert.equal(gets, 2)
})

test('duplicate queue entries are skipped and permanent upload errors are not retried', async () => {
  const store = useUploadStore()
  let patches = 0
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'PATCH') { patches++; return json({ detail: 'too large' }, 413) }
    return json({ id: 'one', offset: 0, chunk_size: 4, size: 8 })
  }
  assert.equal(store.enqueue([file(), file()], null), 1)
  await until(() => store.jobs[0]!.state === 'error')
  assert.equal(patches, 1)
  store.clearFinished()
  assert.equal(store.jobs.length, 1)
})


test('queue keeps two active tasks and batch pause/resume preserves waiting tasks', async () => {
  const store = useUploadStore()
  let creates = 0
  const records = new Map<string, { name: string; done: boolean }>()
  let finish = false
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') {
      creates++
      const id = String(creates)
      records.set(id, { name: JSON.parse(String(init?.body)).name, done: false })
      return json({ id, offset: 0, size: 8, chunk_size: 8 })
    }
    const id = String(url).split('/')[3]!
    if (init?.method === 'PATCH') {
      if (!finish) return new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
      return json({ offset: 8, received: 8, size: 8 })
    }
    if (init?.method === 'POST') { records.get(id)!.done = true; return json({ ok: true }, 202) }
    return json({ id, name: records.get(id)!.name, parent_id: null, size: 8,
      received: 0, chunk_size: 8, progress: 8, state: records.get(id)!.done ? 'done' : 'uploading' })
  }
  store.enqueue([file(), new File(['abcdefgh'], 'other.bin'), new File(['abcdefgh'], 'third.bin')], null)
  await until(() => store.jobs.filter((j) => j.controller).length === 2)
  assert.equal(creates, 2)
  assert.equal(store.jobs[2]!.state, 'queued')
  store.pauseAll()
  assert.ok(store.jobs.every((j) => j.state === 'paused'))
  finish = true
  store.resumeAll()
  await until(() => store.jobs.every((j) => j.state === 'done'))
  assert.equal(creates, 3)
})

test('cancel during creation cleans up the late upload and prevents a new run', async () => {
  const store = useUploadStore()
  let resolveCreate!: (value: Response) => void
  let deletes = 0
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'DELETE') { deletes++; return json({ ok: true }) }
    return new Promise((resolve) => { resolveCreate = resolve })
  }
  store.enqueue([file()], null)
  const job = store.jobs[0]!
  const cancel = store.cancel(job)
  assert.equal(job.state, 'canceling')
  store.retry(job)
  assert.equal(job.state, 'canceling')
  resolveCreate(json({ id: 'late', offset: 0, size: 8, chunk_size: 4 }))
  await cancel
  assert.equal(deletes, 1)
  assert.equal(job.state, 'canceled')
  assert.equal(job.uploadId, null)
  assert.deepEqual(JSON.parse(memory.get('tgdrive_resume_v2')!), {})
})
