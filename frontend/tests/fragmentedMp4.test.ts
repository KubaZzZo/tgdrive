import assert from 'node:assert/strict'
import { test } from 'node:test'
import { attachMedia, fragmentedMp4Type } from '../src/utils/fragmentedMp4.ts'

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

test('an unbuffered seek starts appending at a nearby video keyframe', async () => {
  const video = box('avc1', new Uint8Array(78), box('avcC', Uint8Array.from([1, 100, 0, 31])))
  const header = box('moov', box('trak', box('tkhd', words(0, 0, 0, 1)),
    box('mdia', box('mdhd', words(0, 0, 0, 1000)), box('hdlr', words(0, 0, 0x76696465)),
      box('minf', box('stbl', box('stsd', words(0, 1), video))))), box('mvex'))
  const moof = (index: number) => box('moof', box('mfhd', words(0, index)),
    box('traf', box('tfhd', words(0, 1)), box('tfdt', words(0, index * 5000)),
      box('trun', words(4, 1, index % 2 ? 0x10000 : 0x02000000))))
  const parts = Array.from({ length: 200 }, (_, index) =>
    [moof(index), box('mdat', new Uint8Array(index % 3 ? 20000 : 10000))]).flat()
  const file = new Blob([header, ...parts] as Uint8Array<ArrayBuffer>[])
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
      () => reject(new Error('Media pump failed')), { file, bufferSeconds: () => 5 })
    media.currentTime = 655
    media.dispatchEvent(new Event('seeking'))
    const firstAppend = await promise
    assert.deepEqual(firstAppend.subarray(0, moof(130).length), moof(130))
    assert.equal(media.currentTime, 655)
  } finally {
    clearTimeout(timeout)
    controller.abort()
    session?.dispose()
    URL.createObjectURL = create
    URL.revokeObjectURL = revoke
    if (original) Object.defineProperty(globalThis, 'MediaSource', original)
    else Reflect.deleteProperty(globalThis, 'MediaSource')
  }
})
