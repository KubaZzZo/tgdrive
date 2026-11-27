import assert from 'node:assert/strict'
import { test } from 'node:test'
import { tl } from '@mtcute/web'
import type { BaseTelegramClient } from '@mtcute/web'
import type { DriveItem } from '../src/api/types.ts'
import { createTelegramDirectClient, signInQr, telegramDirectMedia } from '../src/utils/telegramDirectClient.ts'
import { untilAborted } from '../src/utils/telegramMedia.ts'

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

test('Telegram QR login records authorization before the SDK reads the profile in the new DC', { timeout: 5000 }, async () => {
  let authorized = false
  const client = createTelegramDirectClient(123, 'a'.repeat(32), { onQrAuthorized: () => { authorized = true } })
  const abort = new AbortController()
  try {
    assert.equal(client.params.disableUpdates, false)
    assert.equal(client.updates, undefined)
    const observer = client.params.network?.middlewares?.[0]
    assert.ok(observer)
    const profile = Promise.withResolvers<void>()
    const token = Uint8Array.of(1, 2, 3)
    const methods: string[] = []
    let dc = 2
    client.changePrimaryDc = async (nextDc) => { dc = nextDc }
    client.call = ((request: tl.RpcMethod) => observer({ request, manager: client.mt.network }, async () => {
      methods.push(request._)
      switch (request._) {
        case 'auth.exportLoginToken':
          return methods.length === 1
            ? { _: 'auth.loginToken', expires: Math.floor(Date.now() / 1000) + 30, token }
            : { _: 'auth.loginTokenMigrateTo', dcId: 4, token }
        case 'auth.importLoginToken':
          assert.equal(dc, 4)
          assert.deepEqual(request.token, token)
          return { _: 'auth.loginTokenSuccess' }
        case 'users.getUsers':
          assert.equal(authorized, true)
          profile.resolve()
          return untilAborted(abort.signal, () => new Promise(() => {}))
        default:
          assert.fail(`Unexpected Telegram request ${request._}`)
      }
    })) as typeof client.call
    const signingIn = signInQr(client, {
      abortSignal: abort.signal,
      onUrlUpdated: () => queueMicrotask(() => client.onServerUpdate.emit({
        _: 'updateShort', update: { _: 'updateLoginToken' }, date: Math.floor(Date.now() / 1000),
      })),
    })
    await Promise.race([profile.promise, signingIn])
    assert.deepEqual(methods, ['auth.exportLoginToken', 'auth.exportLoginToken', 'auth.importLoginToken', 'users.getUsers'])
    abort.abort()
    await assert.rejects(signingIn, { name: 'AbortError' })
  } finally {
    abort.abort()
    await client.destroy()
  }
})

test('Telegram direct reads align ranges, refresh stale file references and reuse the block', async () => {
  const { client, reads } = fixture()
  const signal = new AbortController().signal
  let received = 0
  const source = await telegramDirectMedia(client, item, signal, bytes => { received += bytes })
  assert.deepEqual(await source.read(blockSize + 7, blockSize + 10, signal), new Uint8Array([7, 7, 7]))
  assert.equal((await source.read(blockSize + 19, blockSize + 21, signal)).length, 2)
  assert.deepEqual(reads, [blockSize, blockSize])
  assert.equal(received, blockSize)
  assert.equal((await source.read(item.size - 2, item.size, signal)).length, 2)
  await assert.rejects(source.read(0, 16, AbortSignal.abort()), { name: 'AbortError' })
  assert.deepEqual(reads, [blockSize, blockSize, blockSize * 5])
  assert.equal(received, blockSize + 9)
})

test('Telegram direct reads reject a replaced video instead of mixing bytes', async () => {
  const { client, reads } = fixture(true)
  const signal = new AbortController().signal
  const source = await telegramDirectMedia(client, item, signal)
  await assert.rejects(source.read(0, 2, signal), /视频已更换/)
  assert.deepEqual(reads, [0])
})
