// OPFS belongs to this browser/device. No media cache is written on the server.
const directory = 'tgdrive-video-cache-v1'
export const deviceCacheAvailable = () => !!navigator.storage?.getDirectory && !!navigator.locks
async function root(): Promise<FileSystemDirectoryHandle> {
  return (await navigator.storage.getDirectory()).getDirectoryHandle(directory, { create: true })
}
export async function cachedVideo(key: string, size: number): Promise<File | null> {
  try {
    return await navigator.locks.request(directory, { mode: 'shared', ifAvailable: true }, async lock => {
      if (!lock) return null
      const dir = await root()
      const info = JSON.parse(await (await (await dir.getFileHandle('complete.json')).getFile()).text())
      const file = await (await dir.getFileHandle('video.bin')).getFile()
      return info.key === key && info.size === size && file.size === size ? file : null
    })
  } catch { return null }
}
export async function clearVideoCache(): Promise<void> {
  await navigator.locks.request(directory, { ifAvailable: true }, async lock => {
    if (!lock) throw new Error('另一个标签页正在缓存视频')
    await (await navigator.storage.getDirectory()).removeEntry(directory, { recursive: true }).catch((e: DOMException) => {
      if (e.name !== 'NotFoundError') throw e
    })
  })
}
export async function cacheVideo(
  url: string, key: string, size: number, signal: AbortSignal, progress: (bytes: number) => void,
): Promise<File> {
  return navigator.locks.request(directory, { ifAvailable: true }, async lock => {
    if (!lock) throw new Error('另一个标签页正在缓存视频')
    const estimate = await navigator.storage.estimate()
    if (estimate.quota && size > estimate.quota - (estimate.usage ?? 0))
      throw new Error('本机空间不足，请先清除旧缓存或释放存储空间')
    const dir = await root()
    await dir.removeEntry('complete.json').catch((e: DOMException) => { if (e.name !== 'NotFoundError') throw e })
    const handle = await dir.getFileHandle('video.bin', { create: true })
    const writer = await handle.createWritable()
    const controller = new AbortController()
    const stop = () => controller.abort()
    signal.addEventListener('abort', stop, { once: true })
    if (signal.aborted) stop()
    let offset = 0, received = 0
    let writes = Promise.resolve()
    const chunk = 2 * 1024 * 1024
    const worker = async () => {
      while (offset < size) {
        controller.signal.throwIfAborted()
        const start = offset, end = Math.min(size, start + chunk) - 1
        offset = end + 1
        let data: ArrayBuffer | undefined
        for (let attempt = 0; attempt < 3; attempt++) {
          try {
            const response = await fetch(url, { headers: { Range: `bytes=${start}-${end}` }, signal: controller.signal })
            if (response.status !== 206 || response.headers.get('Content-Range') !== `bytes ${start}-${end}/${size}`) {
              await response.body?.cancel(); throw new Error('视频分段响应无效，请重新加载')
            }
            data = await response.arrayBuffer()
            if (data.byteLength !== end - start + 1) throw new Error('视频分段不完整')
            break
          } catch (error) { if (attempt === 2 || controller.signal.aborted) throw error }
        }
        writes = writes.then(() => writer.write({ type: 'write', position: start, data: data! }))
        await writes
        received += data!.byteLength
        progress(received)
      }
    }
    try {
      // Three bounded requests overlap Telegram round trips without holding a
      // whole movie in phone RAM. Writes use explicit byte positions.
      const workers = Array.from({ length: 3 }, () => worker().catch(error => { controller.abort(); throw error }))
      const results = await Promise.allSettled(workers)
      const failed = results.find(result => result.status === 'rejected')
      if (failed?.status === 'rejected') throw failed.reason
      controller.signal.throwIfAborted()
      await writer.close()
      const manifest = await (await dir.getFileHandle('complete.json', { create: true })).createWritable()
      await manifest.write(JSON.stringify({ key, size })); await manifest.close()
      return await handle.getFile()
    } catch (error) {
      controller.abort()
      await writer.abort().catch(() => {})
      await dir.removeEntry('video.bin').catch(() => {})
      throw error
    } finally { signal.removeEventListener('abort', stop) }
  })
}
