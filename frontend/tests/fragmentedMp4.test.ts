import assert from 'node:assert/strict'
import { test } from 'node:test'
import { bufferedSeek, fragmentedMp4Type } from '../src/utils/fragmentedMp4.ts'

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

test('fragmented video seeks stay within buffered ranges, including gaps', () => {
  const buffered = { length: 2, start: (i: number) => [0, 30][i]!, end: (i: number) => [10, 40][i]! }
  assert.equal(bufferedSeek(buffered, 5), 5)
  assert.equal(bufferedSeek(buffered, 35), 35)
  assert.equal(bufferedSeek(buffered, 120), 39.9)
  assert.equal(bufferedSeek(buffered, 20), 30)
  assert.equal(bufferedSeek({ ...buffered, length: 0 }, 20), null)
})
