import assert from 'node:assert/strict'
import { test } from 'node:test'
import { acquireCoverSlot, holdCoverQueue } from '../src/utils/coverQueue.ts'

test('cover requests are bounded across cards and cancelled waiters are removed', async () => {
  const first = await acquireCoverSlot(new AbortController().signal)
  const second = await acquireCoverSlot(new AbortController().signal)
  const cancelled = new AbortController()
  const aborted = assert.rejects(acquireCoverSlot(cancelled.signal), { name: 'AbortError' })
  let started = false
  const next = acquireCoverSlot(new AbortController().signal).then(release => { started = true; return release })
  await Promise.resolve()
  assert.equal(started, false)
  cancelled.abort(); await aborted
  first(); first() // Release must be idempotent.
  const third = await next
  assert.equal(started, true)
  third(); second()
  const after = await acquireCoverSlot(new AbortController().signal)
  after()
})

test('foreground playback holds new cover requests until it closes', async () => {
  const release = await acquireCoverSlot(new AbortController().signal)
  const resume = holdCoverQueue()
  let started = false
  const next = acquireCoverSlot(new AbortController().signal).then(slot => { started = true; return slot })
  await Promise.resolve()
  release()
  assert.equal(started, false)
  resume(); resume()
  ;(await next)()
  assert.ok(started)
})
