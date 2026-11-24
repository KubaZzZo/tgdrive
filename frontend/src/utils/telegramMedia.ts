const workerPath = '/telegram-media-sw.js'
const mediaPath = '/telegram-media/'

export interface TelegramMediaSource {
  size: number
  mime: string
  read(offset: number, end: number, signal: AbortSignal): Promise<Uint8Array<ArrayBuffer>>
}

export async function untilAborted<T>(signal: AbortSignal, action: () => Promise<T>): Promise<T> {
  signal.throwIfAborted()
  const listeners = new AbortController()
  const interrupted = new Promise<never>((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(signal.reason), { once: true, signal: listeners.signal })
  })
  try {
    return await Promise.race([action(), interrupted])
  } finally {
    listeners.abort()
  }
}

// end is exclusive, including for requests with an inclusive HTTP end byte.
export function telegramMediaRange(range: string | null, size: number): { start: number; end: number } | null {
  if (range === null) return { start: 0, end: size }
  const match = /^bytes=(\d*)-(\d*)$/i.exec(range.trim())
  if (!match || (!match[1] && !match[2])) return null
  const first = match[1] ? Number(match[1]) : null
  const last = match[2] ? Number(match[2]) : null
  if (first === null) return last ? { start: Math.max(0, size - last), end: size } : null
  if (first >= size || (last !== null && last < first)) return null
  return { start: first, end: last === null ? size : Math.min(size, last + 1) }
}

export async function prepareTelegramMedia(signal: AbortSignal): Promise<void> {
  if (!window.isSecureContext || !navigator.serviceWorker) {
    throw new Error('当前浏览器需要在 HTTPS 页面启用 Service Worker 才能直连播放。')
  }
  const workers = navigator.serviceWorker
  const scriptUrl = new URL(workerPath, location.href).href
  const wait = AbortSignal.any([signal, AbortSignal.timeout(8000)])
  await untilAborted(wait, () => workers.register(workerPath))
  await new Promise<void>((resolve, reject) => {
    const check = () => {
      if (workers.controller?.scriptURL !== scriptUrl) return
      wait.removeEventListener('abort', timedOut)
      workers.removeEventListener('controllerchange', check)
      resolve()
    }
    const timedOut = () => {
      workers.removeEventListener('controllerchange', check)
      reject(new Error('直连播放器启动超时，请重新打开视频。'))
    }
    if (wait.aborted) { timedOut(); return }
    wait.addEventListener('abort', timedOut, { once: true })
    workers.addEventListener('controllerchange', check)
    check()
  })
}

export function openTelegramMedia(source: TelegramMediaSource, lifetime: AbortSignal): string {
  lifetime.throwIfAborted()
  const token = crypto.randomUUID()
  const workers = navigator.serviceWorker
  workers.addEventListener('message', (event: MessageEvent) => {
    const data = event.data
    const port = event.ports[0]
    if (event.source !== workers.controller || data?.type !== 'telegram-media' || data.token !== token || !port) return
    const range = telegramMediaRange(data.range, source.size)
    const headers: Record<string, string> = {
      'Accept-Ranges': 'bytes',
      'Cache-Control': 'private, no-store',
      'Content-Type': source.mime,
      'X-Content-Type-Options': 'nosniff',
    }
    if (!range) {
      headers['Content-Range'] = `bytes */${source.size}`
      port.postMessage({ status: 416, headers })
      port.close()
      return
    }
    const { start, end } = range
    headers['Content-Length'] = String(end - start)
    if (data.range !== null) headers['Content-Range'] = `bytes ${start}-${end - 1}/${source.size}`
    port.postMessage({ status: data.range === null ? 200 : 206, headers })
    if (data.method === 'HEAD') { port.close(); return }

    let position = start
    let reading = false
    const request = new AbortController()
    function stop(): void {
      lifetime.removeEventListener('abort', closePlayback)
      request.abort()
      port.close()
    }
    function closePlayback(): void {
      port.postMessage({ error: '直连播放已关闭。' })
      stop()
    }
    lifetime.addEventListener('abort', closePlayback, { once: true })
    port.onmessage = async ({ data: action }) => {
      if (action === 'cancel') { stop(); return }
      if (action !== 'read' || reading) return
      if (position === end) {
        port.postMessage({ done: true })
        stop()
        return
      }
      reading = true
      try {
        const chunk = await source.read(position, end, request.signal)
        request.signal.throwIfAborted()
        if (!chunk.length || position + chunk.length > end) throw new Error('视频数据不完整')
        position += chunk.length
        port.postMessage({ chunk }, [chunk.buffer])
      } catch {
        if (!request.signal.aborted) port.postMessage({ error: 'Telegram 视频读取中断，请重试加载。' })
        stop()
      } finally {
        reading = false
      }
    }
  }, { signal: lifetime })
  return mediaPath + token
}
