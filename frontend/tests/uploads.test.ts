import assert from 'node:assert/strict'
import { beforeEach, test } from 'node:test'
import { createPinia, setActivePinia } from 'pinia'
import { useUploadStore } from '../src/stores/uploads.ts'
import { uploadChunk } from '../src/api/client.ts'
import { useAuthStore } from '../src/stores/auth.ts'
import type { Meta } from '../src/api/types.ts'

const memory = new Map<string, string>()
Object.assign(globalThis, {
  window: {
    setTimeout: (callback: () => void, ms: number) => setTimeout(callback, ms === 120_000 ? ms : 5),
    clearTimeout,
    dispatchEvent() {},
  },
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

test('image host uploads preserve publication intent and cannot resume a private upload', async () => {
  const image = new File(['pngbytes'], 'photo.png', { lastModified: 123 })
  memory.set('tgdrive_resume_v2', JSON.stringify({ '|photo.png|8|123': 'private-upload' }))
  const bodies: Record<string, unknown>[] = []
  globalThis.fetch = async (url, init) => {
    assert.ok(!String(url).includes('private-upload'))
    if (url === '/api/uploads') {
      bodies.push(JSON.parse(String(init?.body)))
      return json({ id: 'public-upload', offset: 8, chunk_size: 8, size: 8 })
    }
    if (init?.method === 'POST') return json({ ok: true }, 202)
    return json({ id: 'public-upload', state: 'done', image_host: 1 })
  }
  const store = useUploadStore()
  assert.equal(store.enqueue([image], null, true), 1)
  await until(() => store.jobs[0]?.state === 'done')
  assert.equal(bodies[0]?.image_host, true)
  assert.equal(JSON.parse(memory.get('tgdrive_resume_v2')!)['|photo.png|8|123'], 'private-upload')
  assert.equal(store.enqueue([new File(['svg'], 'evil.svg')], null, true), 0)
})

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

test('pause and immediate resume reuse the server-confirmed offset', async () => {
  const store = useUploadStore()
  let creates = 0
  let patches = 0
  let resumedOffset = ''
  const deleted: string[] = []
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') {
      creates++
      return json({ id: String(creates), offset: 0, chunk_size: 4, size: 8 })
    }
    if (init?.method === 'DELETE') { deleted.push(String(url)); return json({ ok: true }) }
    if (init?.method === 'PATCH') {
      patches++
      if (patches === 1) {
        return new Promise((_, reject) => init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError'))))
      }
      assert.equal(url, '/api/uploads/1')
      resumedOffset = new Headers(init.headers).get('Upload-Offset') ?? ''
      return json({ offset: 8, received: 8, size: 8 })
    }
    if (init?.method === 'POST') return json({ ok: true }, 202)
    return json({ id: '1', name: 'data.bin', size: 8, parent_id: null,
      received: 4, chunk_size: 4, state: patches === 1 ? 'uploading' : 'done' })
  }
  store.enqueue([file()], null)
  await until(() => patches === 1)
  store.pause(store.jobs[0]!)
  assert.equal(store.jobs[0]!.state, 'paused')
  store.resume(store.jobs[0]!) // Resume before the aborted request has unwound.
  await until(() => store.jobs[0]!.state === 'done')
  assert.equal(creates, 1)
  assert.deepEqual(deleted, [])
  assert.equal(resumedOffset, '4')
  assert.equal(store.totalProgress, 100)
})

test('retry observes an already processing upload instead of creating a duplicate', async () => {
  const original = useUploadStore()
  let submitted = false
  globalThis.fetch = async (_url, init) => {
    if (_url === '/api/uploads') return json({ id: 'existing', offset: 8, size: 8, chunk_size: 4 })
    if (init?.method === 'POST') { submitted = true; return json({ detail: 'denied' }, 403) }
    throw new Error('Unexpected request')
  }
  original.enqueue([file()], null)
  await until(() => submitted && original.jobs[0]?.state === 'error')

  setActivePinia(createPinia())
  const recovered = useUploadStore()
  let gets = 0
  globalThis.fetch = async (url, init) => {
    assert.equal(url, '/api/uploads/existing')
    assert.ok(!init?.method || init.method === 'GET')
    gets++
    return json({ id: 'existing', name: 'data.bin', parent_id: null, size: 8,
      received: 8, chunk_size: 4, progress: 8, state: gets === 1 ? 'processing' : 'done' })
  }
  recovered.enqueue([file()], null)
  await until(() => recovered.jobs[0]!.state === 'done')
  assert.equal(gets, 2)
})

for (const mode of ['retry', 'reselect'] as const) {
  test(`an invalid temp file starts a new upload on ${mode}`, async () => {
    const original = useUploadStore()
    const requests: string[] = []
    const invalid = {
      id: 'old', name: 'data.bin', parent_id: null, size: 8,
      received: 8, chunk_size: 8, state: 'invalid', error: '临时文件不存在，请重新上传',
    }
    let creates = 0
    globalThis.fetch = async (url, init) => {
      requests.push(`${init?.method ?? 'GET'} ${url}`)
      if (url === '/api/uploads') {
        return json({ id: ++creates === 1 ? 'old' : 'new', offset: 0, size: 8, chunk_size: 8 })
      }
      if (init?.method === 'PATCH') {
        assert.equal(new Headers(init.headers).get('Upload-Offset'), '0')
        return json({ offset: 8, received: 8, size: 8 })
      }
      if (url === '/api/uploads/old/complete' && mode === 'reselect') {
        return json({ detail: 'denied' }, 403)
      }
      if (String(url).startsWith('/api/uploads/old')) return json(invalid)
      return json({ id: 'new', state: 'done' })
    }
    original.enqueue([file()], null)
    await until(() => original.jobs[0]?.state === 'error')
    let store = original
    if (mode === 'retry') {
      assert.equal(original.jobs[0]!.uploadId, null)
      assert.deepEqual(JSON.parse(memory.get('tgdrive_resume_v2')!), {})
      store.retry(store.jobs[0]!)
    } else {
      setActivePinia(createPinia())
      store = useUploadStore()
      store.enqueue([file()], null)
    }
    await until(() => store.jobs[0]?.state === 'done')
    assert.deepEqual(requests, [
      'POST /api/uploads', 'PATCH /api/uploads/old', 'POST /api/uploads/old/complete',
      ...(mode === 'reselect' ? ['GET /api/uploads/old'] : []),
      'POST /api/uploads', 'PATCH /api/uploads/new', 'POST /api/uploads/new/complete',
    ])
  })
}

test('duplicate queue entries are skipped and permanent upload errors are not retried', async () => {
  const store = useUploadStore()
  let patches = 0
  let deletes = 0
  globalThis.fetch = async (_url, init) => {
    if (init?.method === 'PATCH') { patches++; return json({ detail: 'too large' }, 413) }
    if (init?.method === 'DELETE') { deletes++; return json({ ok: true }) }
    return json({ id: 'one', offset: 0, chunk_size: 4, size: 8 })
  }
  assert.equal(store.enqueue([file(), file()], null), 1)
  await until(() => store.jobs[0]!.state === 'error')
  assert.equal(patches, 1)
  assert.equal(deletes, 0)
  store.clearFinished()
  assert.equal(store.jobs.length, 1)
})

test('retry after going offline reuses the confirmed server offset', async () => {
  const store = useUploadStore()
  let online = true
  let creates = 0
  let patches = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') {
      creates++
      return json({ id: 'one', offset: 4, chunk_size: 4, size: 8 })
    }
    if (init?.method === 'PATCH') {
      patches++
      assert.equal(url, '/api/uploads/one')
      assert.equal(new Headers(init.headers).get('Upload-Offset'), '4')
      if (!online) throw new TypeError('offline')
      return json({ offset: 8, received: 8, size: 8 })
    }
    if (init?.method === 'POST') return json({ ok: true }, 202)
    return json({ id: 'one', name: 'data.bin', parent_id: null, size: 8,
      received: 4, chunk_size: 4, state: online && patches > 5 ? 'done' : 'uploading' })
  }
  online = false
  store.enqueue([file()], null)
  await until(() => store.jobs[0]!.state === 'error')
  assert.equal(patches, 5)
  assert.equal(store.jobs[0]!.received, 4)
  online = true
  store.retry(store.jobs[0]!)
  await until(() => store.jobs[0]!.state === 'done')
  assert.equal(creates, 1)
})

test('a lost PATCH response is retried at the same offset and duplicate acknowledgement advances it', async () => {
  const store = useUploadStore()
  const offsets: string[] = []
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: 'one', offset: 0, size: 8, chunk_size: 4 })
    if (init?.method === 'PATCH') {
      offsets.push(new Headers(init.headers).get('Upload-Offset')!)
      if (offsets.length === 1) return new Response('{', { headers: { 'content-type': 'application/json' } })
      if (offsets.length === 2) return json({ offset: 4, received: 4, duplicate: true, size: 8 })
      return json({ offset: 8, received: 8, size: 8 })
    }
    return json({ id: 'one', received: 8, state: 'done' })
  }
  store.enqueue([file()], null)
  await until(() => store.jobs[0]?.state === 'done')
  assert.deepEqual(offsets, ['0', '0', '4'])
})

test('a timed-out chunk backs off to smaller requests and resumes from the confirmed offset', async () => {
  const store = useUploadStore()
  const selected = new File([new Uint8Array(600 * 1024)], 'large.bin')
  const sends: { offset: number; size: number }[] = []
  let confirmed = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: 'one', offset: 0, size: selected.size, chunk_size: 512 * 1024 })
    if (init?.method === 'PATCH') {
      const offset = Number(new Headers(init.headers).get('Upload-Offset'))
      const size = (init.body as Blob).size
      sends.push({ offset, size })
      if (sends.length === 1) return json({ detail: 'timed out' }, 408)
      assert.equal(offset, confirmed)
      confirmed += size
      return json({ offset: confirmed, received: confirmed, size: selected.size })
    }
    return json({ id: 'one', state: 'done', received: confirmed })
  }
  store.enqueue([selected], null)
  await until(() => store.jobs[0]?.state === 'done')
  assert.deepEqual(sends.slice(0, 2), [{ offset: 0, size: 256 * 1024 }, { offset: 0, size: 128 * 1024 }])
  assert.equal(confirmed, selected.size)
})

test('retry recreates a server upload only after the old ID expires', async () => {
  const store = useUploadStore()
  let creates = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: String(++creates), offset: 0, size: 8, chunk_size: 8 })
    if (url === '/api/uploads/1') return json({ detail: 'expired' }, 404)
    if (init?.method === 'PATCH') return json({ offset: 8, received: 8, size: 8 })
    return json({ id: '2', state: 'done', received: 8 })
  }
  store.enqueue([file()], null)
  await until(() => store.jobs[0]?.state === 'error')
  store.retry(store.jobs[0]!)
  await until(() => store.jobs[0]?.state === 'done')
  assert.equal(creates, 2)
})

test('persistent failure submitting a save releases its reservation for retry', async () => {
  const store = useUploadStore()
  let submissions = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: 'one', offset: 8, size: 8, chunk_size: 8 })
    if (init?.method === 'POST') {
      submissions++
      return json({ detail: 'unavailable' }, 503)
    }
    throw new Error('Unexpected request')
  }
  store.enqueue([file()], null)
  await until(() => store.jobs[0]?.state === 'error')
  assert.equal(submissions, 5)
  assert.equal(store.jobs[0]?.uploadId, 'one')
  assert.equal(store.jobs[0]?.received, 8)
})

test('reselection cannot reuse an upload for a same-metadata file with different contents', async () => {
  let creates = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: String(++creates), offset: 0, size: 8, chunk_size: 4 })
    if (init?.method === 'PATCH') return json({ detail: 'blocked' }, 413)
    throw new Error('Different files must use separate upload IDs')
  }
  const original = useUploadStore()
  original.enqueue([file()], null)
  await until(() => original.jobs[0]?.state === 'error')
  setActivePinia(createPinia())
  const replacement = useUploadStore()
  replacement.enqueue([new File(['ABCDEFGH'], 'data.bin', { lastModified: 123 })], null)
  await until(() => replacement.jobs[0]?.state === 'error')
  assert.equal(creates, 2)
})

test('sends past two saving jobs while limiting reserved saves', async () => {
  const store = useUploadStore()
  const received = new Set<string>()
  const saving = new Set<string>()
  let allowFinish = false
  let creates = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: String(++creates), offset: 0, size: 8, chunk_size: 8 })
    const id = String(url).split('/')[3]!
    if (init?.method === 'PATCH') { received.add(id); return json({ offset: 8, received: 8, size: 8 }) }
    if (init?.method === 'POST') { saving.add(id); return json({ ok: true }, 202) }
    return json({ id, state: allowFinish ? 'done' : 'processing', stage: 'sending', progress: 2 })
  }
  store.enqueue(Array.from({ length: 5 }, (_, i) => new File(['abcdefgh'], `file${i}.bin`)), null)
  await until(() => saving.size === 4 && store.jobs[2]?.stage === 'sending')
  assert.equal(received.size, 4)
  assert.equal(store.jobs[2]?.progress, 25)
  assert.equal(store.jobs[4]?.state, 'queued')
  allowFinish = true
  await until(() => store.jobs.every((job) => job.state === 'done'))
  assert.equal(creates, 5)
})

test('an oversized file reserves the byte budget by itself', async () => {
  const store = useUploadStore()
  const huge = new File(['x'], 'huge.bin')
  Object.defineProperty(huge, 'size', { value: 5 * 1024 ** 3 })
  let finishReading!: (value: ArrayBuffer) => void
  huge.slice = () => ({ arrayBuffer: () => new Promise((resolve) => { finishReading = resolve }) }) as Blob
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: 'small', offset: 8, size: 8, chunk_size: 8 })
    if (init?.method === 'POST') return json({ id: 'small', state: 'done', received: 8 })
    throw new Error('Unexpected request')
  }
  store.enqueue([huge, file()], null)
  assert.equal(store.jobs[0]?.state, 'uploading')
  assert.equal(store.jobs[1]?.state, 'queued')
  const cancellation = store.cancel(store.jobs[0]!)
  finishReading(new ArrayBuffer(0))
  await cancellation
  await until(() => store.jobs[1]?.state === 'done')
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
    if (init?.method === 'DELETE') { records.delete(id); return json({ ok: true }) }
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

test('disk quota failure retains confirmed bytes until a successful cancel', async () => {
  const store = useUploadStore()
  let patches = 0
  let deletes = 0
  globalThis.fetch = async (url, init) => {
    if (url === '/api/uploads') return json({ id: 'partial', offset: 4, size: 8, chunk_size: 4 })
    assert.equal(url, '/api/uploads/partial')
    if (init?.method === 'PATCH') {
      patches++
      return json({ detail: 'temp storage full' }, 507)
    }
    assert.equal(init?.method, 'DELETE')
    if (++deletes === 1) return json({ detail: 'cleanup unavailable' }, 503)
    return json({ ok: true })
  }
  store.enqueue([file()], null)
  await until(() => store.jobs[0]?.state === 'error')
  const job = store.jobs[0]!
  assert.equal(patches, 1)
  assert.equal(job.error, 'temp storage full')
  assert.equal(job.received, 4)

  await store.cancel(job)
  assert.equal(job.state, 'error')
  assert.equal(job.error, 'cleanup unavailable')
  assert.equal(job.message, job.error)
  assert.equal(job.uploadId, 'partial')
  assert.equal(job.received, 4)
  assert.deepEqual(Object.values(JSON.parse(memory.get('tgdrive_resume_v2')!)), ['partial'])

  await store.cancel(job)
  assert.equal(job.state, 'canceled')
  assert.equal(job.error, '')
  assert.equal(job.uploadId, null)
  assert.equal(job.received, 0)
  assert.deepEqual(JSON.parse(memory.get('tgdrive_resume_v2')!), {})
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
  await until(() => Boolean(resolveCreate))
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
