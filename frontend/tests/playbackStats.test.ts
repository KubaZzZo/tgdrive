import assert from 'node:assert/strict'
import { test } from 'node:test'
import { bufferedSeconds, TransferRate } from '../src/utils/playbackStats.ts'

test('transfer speed averages the latest three seconds and settles to zero when idle', () => {
  const rate = new TransferRate()
  assert.equal(rate.sample(0, 0), null)
  assert.equal(rate.sample(3000, 1000), 3000)
  assert.equal(rate.sample(3000, 2000), 1500)
  assert.equal(rate.sample(3000, 3000), 1000)
  assert.equal(rate.sample(3000, 4000), 0)
  assert.equal(rate.sample(9000, 5000), 2000)
})

test('missing counters, resets and invalid timestamps start a fresh baseline', () => {
  const rate = new TransferRate()
  rate.sample(100, 1000)
  assert.equal(rate.sample(null, 2000), null)
  assert.equal(rate.sample(200, 3000), null)
  assert.equal(rate.sample(300, 4000), 100)
  assert.equal(rate.sample(0, 5000), null)
  assert.equal(rate.sample(10, 4000), null)
  assert.equal(rate.sample(NaN, 6000), null)
  assert.equal(rate.sample(10, Infinity), null)
  rate.sample(100, 7000)
  assert.equal(rate.sample(300, 13000), null)
  assert.equal(rate.sample(300, 14000), 0)
})

test('buffer duration only includes the continuous range at the playhead', () => {
  const buffered = { length: 2, start: (i: number) => [0, 60][i]!, end: (i: number) => [20, 90][i]! }
  assert.equal(bufferedSeconds({ currentTime: 12.5, buffered }), 7.5)
  assert.equal(bufferedSeconds({ currentTime: 30, buffered }), 0)
  assert.equal(bufferedSeconds({ currentTime: 65, buffered }), 25)
  assert.equal(bufferedSeconds({ currentTime: 90, buffered }), 0)
})
