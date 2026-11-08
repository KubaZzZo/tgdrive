import assert from 'node:assert/strict'
import { test } from 'node:test'
import { attachMedia, fragmentedMp4Type, mediaChunkSize } from '../src/utils/fragmentedMp4.ts'

test('media ranges adapt to bitrate and stay aligned within memory limits', () => {
  assert.equal(mediaChunkSize(10_000_000, 1000), 512 * 1024)
  assert.equal(mediaChunkSize(4_000_000_000, 1000), 4 * 1024 * 1024)
  assert.equal(mediaChunkSize(100_000_000, NaN), 2 * 1024 * 1024)
  assert.equal(mediaChunkSize(100_000_000, 0), 2 * 1024 * 1024)
  assert.equal(mediaChunkSize(200_000_000, 1000) % (64 * 1024), 0)
})

function box(type: string, ...parts: Uint8Array[]): Uint8Array {
  const length = 8 + parts.reduce((sum, part) => sum + part.length, 0)
  const bytes = new Uint8Array(length)
  new DataView(bytes.buffer).setUint32(0, length)
  bytes.set(Buffer.from(type), 4)
  let offset = 8
  for (const part of parts) { bytes.set(part, offset); offset += part.length }
  return bytes
}
function movie(fragmented = true, video = 'avc1', audioType = 2): Uint8Array {
  const videoEntry = box(video, new Uint8Array(78), box('avcC', Uint8Array.from([1, 100, 0, 31])))
  const decoder = [0x40, 0x15, ...new Uint8Array(11), 5, 2, audioType << 3, 0x10]
  const es = [0, 1, 0, 4, decoder.length, ...decoder]
  const audioEntry = box('mp4a', new Uint8Array(28), box('esds', Uint8Array.from([0, 0, 0, 0, 3, es.length, ...es])))
  const track = (entry: Uint8Array) => box('trak', box('mdia', box('minf', box('stbl', box('stsd', Uint8Array.from([0, 0, 0, 0, 0, 0, 0, 1]), entry)))))
  return box('moov', track(videoEntry), track(audioEntry), ...(fragmented ? [box('mvex')] : []))
}

test('fragmented H.264/AAC chooses streaming with codec data from the file', () => {
  assert.equal(fragmentedMp4Type(movie()), 'video/mp4; codecs="avc1.64001f,mp4a.40.2"')
  assert.equal(fragmentedMp4Type(movie(true, 'avc3', 5)), 'video/mp4; codecs="avc3.64001f,mp4a.40.5"')
})

test('ordinary, unsupported, incomplete and invalid MP4 headers stay native', () => {
  assert.equal(fragmentedMp4Type(movie(false)), null)
  assert.equal(fragmentedMp4Type(movie(true, 'hvc1')), null)
  assert.equal(fragmentedMp4Type(movie().slice(0, -1)), null)
  assert.equal(fragmentedMp4Type(new Uint8Array(65536)), null)
  assert.equal(fragmentedMp4Type(Uint8Array.from([255, 255, 255, 255, 109, 111, 111, 118])), null)
})

function words(...values: number[]): Uint8Array<ArrayBuffer> {
  const data = new Uint8Array(values.length * 4)
  const view = new DataView(data.buffer)
  values.forEach((value, index) => view.setUint32(index * 4, value))
  return data
}

const video = box('avc1', new Uint8Array(78), box('avcC', Uint8Array.from([1, 100, 0, 31])))
const header = box('moov', box('trak', box('tkhd', words(0, 0, 0, 1)),
  box('mdia', box('mdhd', words(0, 0, 0, 1000)), box('hdlr', words(0, 0, 0x76696465)),
    box('minf', box('stbl', box('stsd', words(0, 1), video))))), box('mvex'))
const moof = (index: number) => box('moof', box('mfhd', words(0, index)),
  box('traf', box('tfhd', words(0, 1)), box('tfdt', words(0, index * 5000)),
    box('trun', words(4, 1, index % 2 ? 0x10000 : 0x02000000))))

function videoFixture(index: 'none' | 'direct' | 'indirect' = 'none'): Blob {
  const segments = Array.from({ length: 200 }, (_, index) =>
    [moof(index), box('mdat', new Uint8Array(index % 3 ? 20000 : 10000))])
  const indexes = index === 'none' ? [] : [box('sidx', words(0, 1, 1000, 0, 0, segments.length),
    ...segments.map(([fragment, data], entry) => {
      const length = fragment!.length + data!.length
      return words(index === 'indirect' && entry === 0 ? length | 0x80000000 : length,
        5000, entry % 2 ? 0 : 0x80000000)
    }))]
  return new Blob([header, ...indexes, ...segments.flat()] as Uint8Array<ArrayBuffer>[])
}

async function seekToKeyframe(file: Blob, version?: string): Promise<number[]> {
  const reads: number[] = []
  class ObservedFile extends Blob {
    override slice(start = 0, end?: number): Blob {
      reads.push(start)
      return super.slice(start, end)
    }
  }
  const media = Object.assign(new EventTarget(), {
    src: '', load() {}, error: null, duration: 1000, currentTime: 0, paused: true,
    buffered: { length: 1, start: () => 0, end: () => 10 },
  })
  const original = Object.getOwnPropertyDescriptor(globalThis, 'MediaSource')
  const create = URL.createObjectURL, revoke = URL.revokeObjectURL
  const controller = new AbortController()
  const { promise, resolve, reject } = Promise.withResolvers<Uint8Array>()
  class FakeBuffer extends EventTarget {
    buffered = media.buffered
    reset = false
    abort() { this.reset = true }
    remove() { queueMicrotask(() => this.dispatchEvent(new Event('updateend'))) }
    appendBuffer(bytes: Uint8Array) {
      if (this.reset) resolve(bytes)
      queueMicrotask(() => this.dispatchEvent(new Event('updateend')))
    }
  }
  class FakeSource {
    static isTypeSupported() { return true }
    readyState = 'open'
    addSourceBuffer() { return new FakeBuffer() }
  }
  Object.defineProperty(globalThis, 'MediaSource', { value: FakeSource, configurable: true })
  URL.createObjectURL = () => 'blob:test-seek'
  URL.revokeObjectURL = () => {}
  const timeout = setTimeout(() => reject(new Error('No fragment appended for the seek')), 2000)
  let session
  try {
    session = await attachMedia(media as unknown as HTMLMediaElement, '/video', controller.signal,
      () => reject(new Error('Media pump failed')), { file: new ObservedFile([file]), bufferSeconds: () => 5, version })
    media.currentTime = 655
    media.dispatchEvent(new Event('seeking'))
    const firstAppend = await promise
    assert.deepEqual(firstAppend.subarray(0, moof(130).length), moof(130))
    assert.equal(media.currentTime, 655)
    return reads
  } finally {
    clearTimeout(timeout)
    controller.abort()
    session?.dispose()
    URL.createObjectURL = create
    URL.revokeObjectURL = revoke
    if (original) Object.defineProperty(globalThis, 'MediaSource', original)
    else Reflect.deleteProperty(globalThis, 'MediaSource')
  }
}

test('unbuffered seek appends a nearby keyframe and reuses probes when reopening the same version', async () => {
  const file = videoFixture()
  const first = await seekToKeyframe(file, 'v1')
  assert.ok(first.length > 2, 'first seek needs at least one probe')
  const reopened = await seekToKeyframe(file, 'v1')
  assert.deepEqual(reopened, [0, first.at(-1)])
  const changed = await seekToKeyframe(file, 'v2')
  assert.ok(changed.length > 2, 'updated file must probe again')
})

test('direct segment indexes avoid probes and indirect indexes fall back to probing', async () => {
  assert.equal((await seekToKeyframe(videoFixture('direct'))).length, 2)
  assert.ok((await seekToKeyframe(videoFixture('indirect'))).length > 2)
})

for (const changed of [false, true]) test(`range playback ${changed ? 'rejects a changed file' : 'recovers from buffer quota without redownloading'}`, async () => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, 'MediaSource')
  const originalFetch = globalThis.fetch, create = URL.createObjectURL, revoke = URL.revokeObjectURL
  const controller = new AbortController()
  const file = videoFixture()
  const requests: Record<string, string>[] = []
  const { promise, resolve, reject } = Promise.withResolvers<void>()
  let calls = 0, removals = 0, errors = 0
  let rejectedBytes: Uint8Array | undefined
  const media = Object.assign(new EventTarget(), {
    src: '', load() {}, error: null, duration: 1000, currentTime: 20, paused: false,
    buffered: { length: 0 },
  })
  class FakeBuffer extends EventTarget {
    buffered = { length: 1, start: () => 0 }
    appendBuffer(bytes: Uint8Array) {
      calls++
      if (calls === 2) {
        rejectedBytes = bytes
        throw new DOMException('Full', 'QuotaExceededError')
      }
      if (calls === 3) assert.equal(bytes, rejectedBytes, 'retry must use the same unconsumed bytes')
      queueMicrotask(() => this.dispatchEvent(new Event('updateend')))
    }
    remove(start: number, end: number) {
      assert.equal(start, 0); assert.equal(end, 10)
      removals++
      queueMicrotask(() => this.dispatchEvent(new Event('updateend')))
    }
  }
  class FakeSource {
    static isTypeSupported() { return true }
    readyState = 'open'
    addSourceBuffer() { return new FakeBuffer() }
    endOfStream() { resolve() }
  }
  Object.defineProperty(globalThis, 'MediaSource', { value: FakeSource, configurable: true })
  URL.createObjectURL = () => 'blob:test-recovery'
  URL.revokeObjectURL = () => {}
  globalThis.fetch = async (_url, init) => {
    const headers = init!.headers as Record<string, string>
    requests.push(headers)
    if (changed && requests.length > 1) return new Response('changed', { status: 200 })
    const match = /bytes=(\d+)-(\d+)/.exec(headers.Range!)!
    const start = Number(match[1]), end = Math.min(file.size - 1, Number(match[2]))
    return new Response(file.slice(start, end + 1), { status: 206,
      headers: { 'Content-Range': `bytes ${start}-${end}/${file.size}`, ETag: '"v1"' } })
  }
  const timeout = setTimeout(() => reject(new Error('Playback did not finish')), 3000)
  let session
  try {
    session = await attachMedia(media as unknown as HTMLMediaElement, '/video', controller.signal, () => {
      errors++
      if (changed) resolve()
      else reject(new Error('Quota recovery failed'))
    })
    await promise
    assert.ok(requests.length > 1)
    assert.ok(requests.slice(1).every(headers => headers['If-Range'] === '"v1"'))
    if (changed) {
      assert.equal(errors, 1)
      assert.equal(calls, 1, 'changed content must never reach the buffer')
    } else {
      assert.equal(removals, 1)
      assert.equal(errors, 0)
      assert.equal(requests.length, 1 + Math.ceil((file.size - 65536) / (512 * 1024)))
    }
  } finally {
    clearTimeout(timeout)
    controller.abort(); session?.dispose()
    globalThis.fetch = originalFetch
    URL.createObjectURL = create; URL.revokeObjectURL = revoke
    if (saved) Object.defineProperty(globalThis, 'MediaSource', saved)
    else Reflect.deleteProperty(globalThis, 'MediaSource')
  }
})
