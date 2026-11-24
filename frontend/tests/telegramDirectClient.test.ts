import assert from 'node:assert/strict'
import { test } from 'node:test'
import { tl } from '@mtcute/web'
import type { BaseTelegramClient } from '@mtcute/web'
import type { DriveItem } from '../src/api/types.ts'
import { telegramDirectMedia } from '../src/utils/telegramDirectClient.ts'

const blockSize = 256 * 1024
const item: DriveItem = {
  id: 'video', parent_id: null, kind: 'file', category: 'video', name: 'test.mp4',
  size: blockSize * 5 + 9, mime: 'video/mp4', sha256: null, tg_chat_id: '-1001234567890',
  created_at: '', updated_at: '', telegram_video_url: 'https://t.me/c/1234567890/41?single&t=0',
}

function fixture(replaced = false) {
  let messageReads = 0
  const reads: number[] = []
  const peer = { _: 'inputPeerChannel', channelId: 1234567890, accessHash: 'fixture' }
  const client = {
    storage: { peers: { getById: async () => peer } },
    async call(method: { _: string; offset: number; limit: number; id: { id: number }[] }, options?: { abortSignal?: AbortSignal }) {
      if (method._ === 'channels.getMessages') {
        assert.deepEqual(method.id, [{ _: 'inputMessageID', id: 41 }])
        messageReads++
        return {
          _: 'messages.channelMessages', users: [], chats: [],
          messages: [{
            _: 'message', media: { _: 'messageMediaDocument', document: {
              _: 'document', id: replaced ? messageReads : 201, accessHash: 'fixture',
              size: item.size, mimeType: item.mime, dcId: 2, fileReference: Uint8Array.of(messageReads),
            } },
          }],
        }
      }
      assert.equal(method._, 'upload.getFile')
      options?.abortSignal?.throwIfAborted()
      reads.push(method.offset)
      assert.equal(method.limit, blockSize)
      if (messageReads === 1) throw new tl.RpcError(400, 'FILE_REFERENCE_EXPIRED')
      return { _: 'upload.file', bytes: new Uint8Array(Math.min(item.size - method.offset, blockSize)).fill(7) }
    },
  } as unknown as BaseTelegramClient
  return { client, reads }
}

test('Telegram direct reads align ranges, refresh stale file references and reuse the block', async () => {
  const { client, reads } = fixture()
  const signal = new AbortController().signal
  const source = await telegramDirectMedia(client, item, signal)
  assert.deepEqual(await source.read(blockSize + 7, blockSize + 10, signal), new Uint8Array([7, 7, 7]))
  assert.equal((await source.read(blockSize + 19, blockSize + 21, signal)).length, 2)
  assert.deepEqual(reads, [blockSize, blockSize])
  assert.equal((await source.read(item.size - 2, item.size, signal)).length, 2)
  await assert.rejects(source.read(0, 16, AbortSignal.abort()), { name: 'AbortError' })
  assert.deepEqual(reads, [blockSize, blockSize, blockSize * 5])
})

test('Telegram direct reads reject a replaced video instead of mixing bytes', async () => {
  const { client, reads } = fixture(true)
  const signal = new AbortController().signal
  const source = await telegramDirectMedia(client, item, signal)
  await assert.rejects(source.read(0, 2, signal), /视频已更换/)
  assert.deepEqual(reads, [0])
})
