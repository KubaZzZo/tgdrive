import { BaseTelegramClient, MemoryStorage, networkMiddlewares, tl } from '@mtcute/web'
import { getMessages, iterDialogs } from '@mtcute/web/methods.js'
import type { DriveItem } from '@/api/types'
import type { TelegramDirectSession } from './telegramDirectSession'
import { untilAborted } from './telegramMedia'
import type { TelegramMediaSource } from './telegramMedia'

export { checkPassword, sendCode, signIn, signInQr } from '@mtcute/web/methods.js'

const blockSize = 256 * 1024

export function createTelegramDirectClient(
  apiId: number,
  apiHash: string,
  { onQrAuthorized }: { onQrAuthorized?: () => void } = {},
): BaseTelegramClient {
  return new BaseTelegramClient({
    apiId,
    apiHash,
    storage: new MemoryStorage(),
    // QR login needs the server's updateLoginToken notification.
    disableUpdates: !onQrAuthorized,
    updates: false,
    network: onQrAuthorized ? {
      middlewares: [
        async (context, next) => {
          const result = await next(context)
          // signInQr fetches the user profile after Telegram has authorized the session.
          if (result && typeof result === 'object' && '_' in result && result._ === 'auth.loginTokenSuccess') {
            onQrAuthorized()
          }
          return result
        },
        ...networkMiddlewares.basic(),
      ],
    } : undefined,
    logLevel: 0,
    initConnectionOptions: { deviceModel: 'TG Drive Browser', appVersion: 'TG Drive' },
  })
}

export function telegramPasswordRequired(error: unknown): boolean {
  return tl.RpcError.is(error, 'SESSION_PASSWORD_NEEDED')
}

export function telegramSessionExpired(error: unknown): boolean {
  return tl.RpcError.is(error) && error.code === 401
}

export async function logOutTelegramDirectClient(client: BaseTelegramClient, signal: AbortSignal): Promise<void> {
  await untilAborted(signal, () => client.call({ _: 'auth.logOut' }, { abortSignal: signal, maxRetryCount: 0 }))
}

export async function revokeTelegramDirectAuthorization(saved: TelegramDirectSession, signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  const client = createTelegramDirectClient(saved.apiId, '')
  try {
    await untilAborted(signal, () => client.importSession(saved.session))
    await logOutTelegramDirectClient(client, signal)
  } catch (cause) {
    if (!telegramSessionExpired(cause)) throw cause
  } finally {
    void client.destroy().catch(() => {})
  }
}

export function telegramDirectError(error: unknown): string {
  if (tl.RpcError.is(error, 'API_ID_INVALID')) return '请检查 Telegram API ID 和 API Hash。'
  if (tl.RpcError.is(error, 'PHONE_NUMBER_INVALID')) return '请输入带国家或地区区号的 Telegram 手机号。'
  if (tl.RpcError.is(error, 'PHONE_CODE_INVALID')) return '验证码不正确，请重新输入。'
  if (tl.RpcError.is(error, 'PHONE_CODE_EXPIRED')) return '验证码已过期，请重新获取。'
  if (tl.RpcError.is(error, 'AUTH_TOKEN_EXPIRED')) return '二维码已过期，请重新扫码授权。'
  if (tl.RpcError.is(error, 'PASSWORD_HASH_INVALID')) return '两步验证密码不正确。'
  if (tl.RpcError.is(error, 'CHANNEL_PRIVATE')) return '此 Telegram 账号无权查看原频道。'
  if (tl.RpcError.is(error) && error.code === 420) return 'Telegram 操作过于频繁，请稍后重试。'
  if (telegramSessionExpired(error)) return 'Telegram 授权已过期，请重新授权。'
  if (error instanceof DirectVideoError) return error.message
  return 'Telegram 直连失败，请检查设备到 Telegram 的网络后重试。'
}

class DirectVideoError extends Error {}

async function findChannel(client: BaseTelegramClient, channelId: number, signal: AbortSignal): Promise<tl.RawInputPeerChannel> {
  const cached = await client.storage.peers.getById(channelId)
  if (cached?._ === 'inputPeerChannel') return cached
  for await (const dialog of iterDialogs(client, { archived: 'keep', pinned: 'keep' })) {
    signal.throwIfAborted()
    if (dialog.peer.id !== channelId) continue
    const peer = dialog.peer.inputPeer
    if (peer._ === 'inputPeerChannel') return peer
  }
  throw new DirectVideoError('找不到存储频道，请使用已加入该频道的 Telegram 账号授权。')
}

export async function telegramDirectMedia(
  client: BaseTelegramClient,
  item: DriveItem,
  signal: AbortSignal,
): Promise<TelegramMediaSource> {
  const link = /^https:\/\/t\.me\/c\/([1-9]\d*)\/([1-9]\d*)\?single&t=0$/.exec(item.telegram_video_url ?? '')
  const channelId = Number(item.tg_chat_id)
  const messageId = Number(link?.[2])
  if (!link || !Number.isSafeInteger(channelId) || channelId !== -1_000_000_000_000 - Number(link[1]) ||
    !Number.isSafeInteger(messageId) || messageId > 0x7fffffff || !Number.isSafeInteger(item.size) || item.size <= 0) {
    throw new DirectVideoError('此文件缺少完整的 Telegram 视频记录。')
  }
  const channel = await untilAborted(signal, () => findChannel(client, channelId, signal))
  async function getDocument(readSignal: AbortSignal): Promise<tl.RawDocument> {
    const [message] = await untilAborted(readSignal, () => getMessages(client, channel, messageId))
    const raw = message?.raw
    const document = raw?._ === 'message' && raw.media?._ === 'messageMediaDocument' ? raw.media.document : null
    if (document?._ !== 'document' || document.size !== item.size) {
      throw new DirectVideoError('频道里的视频已移除或更换，请刷新列表后重试。')
    }
    return document
  }

  let document = await getDocument(signal)
  const documentId = document.id.toString()
  let dataCenter = document.dcId
  const validMime = /^video\/[a-z0-9.+-]+$/
  const storedMime = item.mime?.split(';', 1)[0].trim().toLowerCase() ?? ''
  const mime = [document.mimeType.toLowerCase(), storedMime].find((value) => validMime.test(value)) ?? 'video/mp4'
  const blocks = new Map<number, Uint8Array>()

  async function fetchBlock(offset: number, requestSignal: AbortSignal): Promise<Uint8Array> {
    async function read(dcId: number): Promise<Uint8Array> {
      const response = await client.call({
        _: 'upload.getFile',
        location: {
          _: 'inputDocumentFileLocation',
          id: document.id,
          accessHash: document.accessHash,
          fileReference: document.fileReference,
          thumbSize: '',
        },
        offset,
        limit: blockSize,
      }, { dcId, kind: 'download', abortSignal: requestSignal, timeout: 20_000, maxRetryCount: 1 })
      if (response._ !== 'upload.file') throw new DirectVideoError('Telegram 未返回视频分块。')
      return response.bytes
    }

    try {
      return await read(dataCenter)
    } catch (error) {
      if (tl.RpcError.is(error, 'FILE_MIGRATE_%d')) {
        dataCenter = error.newDc
        return read(dataCenter)
      }
      if (!tl.RpcError.is(error, 'FILE_REFERENCE_EXPIRED')) throw error
      const refreshed = await getDocument(requestSignal)
      requestSignal.throwIfAborted()
      if (refreshed.id.toString() !== documentId) throw new DirectVideoError('频道里的视频已更换。')
      document = refreshed
      dataCenter = refreshed.dcId
      return read(dataCenter)
    }
  }

  return {
    size: item.size,
    mime,
    async read(offset, end, requestSignal) {
      requestSignal.throwIfAborted()
      const start = Math.floor(offset / blockSize) * blockSize
      let block = blocks.get(start)
      if (!block) {
        const deadline = AbortSignal.any([requestSignal, AbortSignal.timeout(23_000)])
        block = await untilAborted(deadline, () => fetchBlock(start, deadline))
        requestSignal.throwIfAborted()
        if (block.length !== Math.min(blockSize, item.size - start)) {
          throw new DirectVideoError('Telegram 返回的视频分块不完整，请重试。')
        }
        blocks.set(start, block)
        if (blocks.size > 4) blocks.delete(blocks.keys().next().value!)
      }
      // Transfer a copy to the Service Worker while keeping the cached block usable.
      return new Uint8Array(block.subarray(offset - start, end - start))
    },
  }
}
