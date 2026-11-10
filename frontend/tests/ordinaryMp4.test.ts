import assert from 'node:assert/strict'
import test from 'node:test'
import { hasSafeSampleTables } from '../src/utils/ordinaryMp4'

function box(type: string, body: Buffer): Buffer {
  const header = Buffer.alloc(8)
  header.writeUInt32BE(header.length + body.length)
  header.write(type, 4)
  return Buffer.concat([header, body])
}

function track(type: string, count: number, sampleSize: number, tableBytes = 0): Buffer {
  const body = Buffer.alloc(12 + tableBytes)
  body.writeUInt32BE(sampleSize, 4)
  body.writeUInt32BE(count, 8)
  return box('trak', box('mdia', box('minf', box('stbl', box(type, body)))))
}

test('remux rejects expanded or incomplete sample tables before MP4Box parses them', () => {
  assert.equal(hasSafeSampleTables(box('moov', track('stsz', 24, 1))), true)
  assert.equal(hasSafeSampleTables(box('moov', track('stsz', 0xffffffff, 1))), false)
  assert.equal(hasSafeSampleTables(box('moov', track('stsz', 24, 0, 4))), false)
  assert.equal(hasSafeSampleTables(box('moov', track('stz2', 3, 4, 2))), true)
  const tracks = Buffer.concat([track('stsz', 180_000, 1), track('stsz', 150_000, 1)])
  assert.equal(hasSafeSampleTables(box('moov', tracks)), false)
  const hidden = box('udta', track('stsz', 0xffffffff, 1))
  assert.equal(hasSafeSampleTables(box('moov', Buffer.concat([track('stsz', 24, 1), hidden]))), false)
})
