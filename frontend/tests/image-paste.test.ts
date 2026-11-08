import assert from 'node:assert/strict'
import { test } from 'node:test'
import { pastedImageFiles } from '../src/utils/imagePaste.ts'

test('clipboard images are ignored while another modal workflow is open', () => {
  const image = new File(['png'], 'photo.png', { type: 'image/png' })
  const event = { target: null, defaultPrevented: false, clipboardData: { files: [image] } }
  assert.deepEqual(pastedImageFiles(event, true), [])
  assert.deepEqual(pastedImageFiles(event, false), [image])
})

test('image paste respects text editors and events consumed by another handler', () => {
  const image = new File(['png'], 'photo.png', { type: 'image/png' })
  const event = { target: null, defaultPrevented: true, clipboardData: { files: [image] } }
  assert.deepEqual(pastedImageFiles(event, false), [])
  const editor = { closest: () => ({}) } as unknown as EventTarget
  assert.deepEqual(pastedImageFiles({ ...event, target: editor, defaultPrevented: false }, false), [])
  assert.deepEqual(pastedImageFiles({ target: null, defaultPrevented: false, clipboardData: null }, false), [])
})
