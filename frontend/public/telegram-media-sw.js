const sourcePath = /^\/telegram-media\/([0-9a-f-]{36})$/

self.addEventListener('install', (event) => event.waitUntil(self.skipWaiting()))
self.addEventListener('activate', (event) => event.waitUntil(self.clients.claim()))
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url)
  const match = sourcePath.exec(url.pathname)
  if (url.origin === self.location.origin && match) {
    event.respondWith(serveMedia(event, match[1]))
  }
})

function readReply(port, timeout, action) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error('Telegram media request timed out')), timeout)
    port.onmessage = ({ data }) => {
      clearTimeout(timer)
      if (data.error) reject(new Error(data.error))
      else resolve(data)
    }
    if (action) port.postMessage(action)
  })
}

async function serveMedia(event, token) {
  const { method, headers } = event.request
  if (method !== 'GET' && method !== 'HEAD') return new Response(null, { status: 405 })
  const owner = await self.clients.get(event.clientId)
  if (!owner) return new Response(null, { status: 404 })
  const { port1, port2 } = new MessageChannel()
  const close = () => {
    port1.postMessage('cancel')
    port1.close()
  }
  try {
    const reply = readReply(port1, 3000)
    owner.postMessage({ type: 'telegram-media', token, method, range: headers.get('Range') }, [port2])
    const metadata = await reply
    if (method === 'HEAD' || metadata.status === 416) {
      port1.close()
      return new Response(null, metadata)
    }
    const signal = event.request.signal
    const cancel = () => close()
    signal.addEventListener('abort', cancel, { once: true })
    const finish = () => {
      signal.removeEventListener('abort', cancel)
      close()
    }
    const stream = new ReadableStream({
      async pull(controller) {
        try {
          const result = await readReply(port1, 25_000, 'read')
          if (result.done) { finish(); controller.close() }
          else controller.enqueue(result.chunk)
        } catch (error) {
          finish()
          controller.error(error)
        }
      },
      cancel: finish,
    }, { highWaterMark: 0 })
    return new Response(stream, metadata)
  } catch {
    close()
    return new Response(null, { status: 404, headers: { 'Cache-Control': 'no-store' } })
  }
}
