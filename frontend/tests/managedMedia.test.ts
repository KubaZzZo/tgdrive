import assert from 'node:assert/strict'
import { test } from 'node:test'
import { attachMedia } from '../src/utils/fragmentedMp4.ts'

function box(type: string, ...parts: Uint8Array[]): Uint8Array {
  const bytes = new Uint8Array(8 + parts.reduce((n, p) => n + p.length, 0))
  new DataView(bytes.buffer).setUint32(0, bytes.length)
  bytes.set(Buffer.from(type), 4)
  let offset = 8
  for (const part of parts) { bytes.set(part, offset); offset += part.length }
  return bytes
}
const header = box('moov', box('trak', box('mdia', box('minf', box('stbl',
  box('stsd', Uint8Array.from([0,0,0,0,0,0,0,1]),
    box('avc1', new Uint8Array(78), box('avcC', Uint8Array.from([1,100,0,31])))),
)))), box('mvex'))

for (const desktopApi of [false, true]) test(`ManagedMediaSource plays on iPhone (desktop API present: ${desktopApi})`, async () => {
  const saved = Object.getOwnPropertyDescriptor(globalThis, 'MediaSource')
  const managedSaved = Object.getOwnPropertyDescriptor(globalThis, 'ManagedMediaSource')
  const objectURL = URL.createObjectURL, revoke = URL.revokeObjectURL
  const controller = new AbortController()
  let instance: FakeManaged | undefined
  let appended = 0, loads = 0, reads = 0
  const media = {
    disableRemotePlayback: false, paused: true, currentTime: 0,
    buffered: { length: 0 }, error: null,
    set src(_value: string) {
      if (instance) assert.equal(this.disableRemotePlayback, true)
    },
    load() { loads++; if (instance) instance.readyState = 'open' },
  } as unknown as HTMLMediaElement
  class FakeManaged extends EventTarget {
    static isTypeSupported() { return true }
    readyState = 'closed'
    streaming = false
    constructor() { super(); instance = this }
    addSourceBuffer() {
      const buffer = new EventTarget() as EventTarget & { appendBuffer: () => void }
      buffer.appendBuffer = () => {
        appended++
        this.streaming = false
        this.dispatchEvent(new Event('endstreaming'))
        queueMicrotask(() => buffer.dispatchEvent(new Event('updateend')))
      }
      return buffer
    }
    endOfStream() { this.readyState = 'ended' }
  }
  class DesktopSource { constructor() { throw new Error('Must prefer ManagedMediaSource') } }
  Object.defineProperty(globalThis, 'MediaSource', { value: desktopApi ? DesktopSource : undefined, configurable: true })
  Object.defineProperty(globalThis, 'ManagedMediaSource', { value: FakeManaged, configurable: true })
  URL.createObjectURL = () => 'blob:test-managed'
  URL.revokeObjectURL = () => {}
  let session
  class VideoFile extends Blob {
    override slice(start?: number, end?: number): Blob { reads++; return super.slice(start, end) }
  }
  try {
    const file = new VideoFile([header, new Uint8Array(100000)])
    session = await attachMedia(media, '/video', controller.signal, () => {}, { file })
    assert.equal(session.streaming, true, 'iPhone must not fall back to native fragmented MP4')
    assert.equal(loads, 1)
    await new Promise(r => setTimeout(r, 250))
    assert.equal(appended, 0, 'ManagedMediaSource owns the streaming window')
    instance!.streaming = true
    instance!.dispatchEvent(new Event('startstreaming'))
    await new Promise(r => setTimeout(r, 250))
    assert.equal(appended, 1)
    assert.equal(reads, 1, 'endstreaming must suspend subsequent range reads')
    instance!.streaming = true
    instance!.dispatchEvent(new Event('startstreaming'))
    await new Promise(r => setTimeout(r, 250))
    assert.equal(appended, 2)
    assert.equal(reads, 2)
  } finally {
    controller.abort(); session?.dispose()
    assert.equal(media.disableRemotePlayback, false)
    URL.createObjectURL = objectURL; URL.revokeObjectURL = revoke
    if (saved) Object.defineProperty(globalThis, 'MediaSource', saved)
    else Reflect.deleteProperty(globalThis, 'MediaSource')
    if (managedSaved) Object.defineProperty(globalThis, 'ManagedMediaSource', managedSaved)
    else Reflect.deleteProperty(globalThis, 'ManagedMediaSource')
  }
})
