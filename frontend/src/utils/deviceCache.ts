import { MediaTransfer, readMediaBytes, strongMediaTag } from './mediaTransfer'

const directory = 'tgdrive-video-cache-v1'
const MiB = 1024 * 1024
const capacity = 512 * MiB
const prefixSize = 64 * 1024
const indexName = 'index.json'

type CacheEntry = { key: string; etag: string; name: string; bytes: number; touched: number; prefix: boolean }
type CachedVideo = { file: File; etag: string; release: () => void }

export const deviceCacheAvailable = () => !!navigator.storage?.getDirectory && !!navigator.locks

async function root(): Promise<FileSystemDirectoryHandle> {
  return (await navigator.storage.getDirectory()).getDirectoryHandle(directory, { create: true })
}

async function readIndex(dir: FileSystemDirectoryHandle): Promise<CacheEntry[]> {
  try {
    const index = await (await dir.getFileHandle(indexName)).getFile()
    return JSON.parse(await index.text()) as CacheEntry[]
  } catch (error) {
    if (error instanceof DOMException && error.name === 'NotFoundError') return []
    throw error
  }
}

async function writeIndex(dir: FileSystemDirectoryHandle, entries: CacheEntry[]): Promise<void> {
  const writer = await (await dir.getFileHandle(indexName, { create: true })).createWritable()
  try {
    await writer.write(JSON.stringify(entries))
    await writer.close()
  } catch (error) {
    await writer.abort().catch(() => {})
    throw error
  }
}

async function removeEntry(dir: FileSystemDirectoryHandle, name: string): Promise<void> {
  await dir.removeEntry(name).catch((error: DOMException) => {
    if (error.name !== 'NotFoundError') throw error
  })
}

function entryLock(name: string): string { return directory + ':' + name }

function pin(name: string): Promise<() => void> {
  return new Promise((resolve, reject) => {
    void navigator.locks.request(entryLock(name), { mode: 'shared' }, () =>
      new Promise<void>(release => resolve(release))).catch(reject)
  })
}

async function evict(dir: FileSystemDirectoryHandle, name: string): Promise<boolean> {
  return navigator.locks.request(entryLock(name), { ifAvailable: true }, async lock => {
    if (!lock) return false
    await removeEntry(dir, name)
    return true
  })
}

export async function cachedVideo(key: string, size: number, allowPrefix = false): Promise<CachedVideo | null> {
  try {
    // Touch the LRU index under the same lock used by writers and eviction.
    return await navigator.locks.request(directory, { ifAvailable: true }, async lock => {
      if (!lock) return null
      const dir = await root()
      const entries = await readIndex(dir)
      for (const bytes of allowPrefix && size > prefixSize ? [size, prefixSize] : [size]) {
        const entry = entries.find(value => value.key === key && value.bytes === bytes)
        if (!entry || !strongMediaTag(entry.etag)) continue
        const file = await (await dir.getFileHandle(entry.name)).getFile()
        if (file.size !== bytes) continue
        entry.touched = Date.now()
        await writeIndex(dir, entries).catch(() => {})
        return { file, etag: entry.etag, release: await pin(entry.name) }
      }
      return null
    })
  } catch { return null }
}

export async function clearVideoCache(releasePlaying?: () => void): Promise<void> {
  await navigator.locks.request(directory, { ifAvailable: true }, async lock => {
    if (!lock) throw new Error('另一个标签页正在缓存视频')
    const locks = await navigator.locks.query()
    const pinned = locks.held?.filter(held => held.name?.startsWith(directory + ':')) ?? []
    if (pinned.length > (releasePlaying ? 1 : 0))
      throw new Error('请关闭正在使用本机缓存的视频后再清除缓存')
    releasePlaying?.()
    await (await navigator.storage.getDirectory()).removeEntry(directory, { recursive: true }).catch((error: DOMException) => {
      if (error.name !== 'NotFoundError') throw error
    })
  })
}

async function writeCached(
  dir: FileSystemDirectoryHandle, key: string, bytes: number,
  fill: (writer: FileSystemWritableFileStream) => Promise<string>,
  prefix = false,
  signal?: AbortSignal,
): Promise<File> {
  const entries = (await readIndex(dir)).sort((a, b) => a.touched - b.touched)
  const existing = new Set(entries.map(entry => entry.name))
  // Remove interrupted writes and the old cache format, which had no ETag.
  for await (const [name] of dir.entries()) {
    if (name !== indexName && !existing.has(name)) await evict(dir, name)
  }
  const limit = Math.max(capacity, bytes)
  const locked = new Set((await navigator.locks.query()).held?.map(lock => lock.name))
  const kept = entries.filter(entry => entry.key !== key || entry.bytes !== bytes)
  let used = kept.reduce((sum, entry) => sum + entry.bytes, 0)
  for (const oldest of kept.slice()) {
    if (used + bytes <= limit && kept.length < 128) break
    if ((prefix && !oldest.prefix) || locked.has(entryLock(oldest.name))) continue
    kept.splice(kept.indexOf(oldest), 1)
    used -= oldest.bytes
  }
  if (used + bytes > limit || kept.length >= 128) throw new Error('本机缓存正在使用，请稍后重试')
  const estimate = await navigator.storage.estimate()
  if (estimate.quota && bytes > estimate.quota - (estimate.usage ?? 0))
    throw new Error('本机空间不足，请先清除旧缓存或释放存储空间')

  const name = crypto.randomUUID()
  const handle = await dir.getFileHandle(name, { create: true })
  try {
    const writer = await handle.createWritable()
    try {
      const etag = await fill(writer)
      await writer.close()
      const file = await handle.getFile()
      if (file.size !== bytes) throw new Error('本机缓存大小不符')
      signal?.throwIfAborted()
      await writeIndex(dir, [...kept, { key, bytes, etag, name, touched: Date.now(), prefix }])
      for (const entry of entries) {
        if (!kept.includes(entry)) await evict(dir, entry.name).catch(() => {})
      }
      return file
    } catch (error) {
      await writer.abort().catch(() => {})
      throw error
    }
  } catch (error) {
    await removeEntry(dir, name).catch(() => {})
    throw error
  }
}

export async function cacheVideoPrefix(key: string, bytes: Uint8Array, etag: string): Promise<void> {
  if (bytes.length !== prefixSize || !strongMediaTag(etag)) return
  await navigator.locks.request(directory, { ifAvailable: true }, async lock => {
    if (!lock) return
    const dir = await root()
    // Speculative data must never evict a whole video saved by the user.
    const entries = await readIndex(dir)
    if (entries.some(entry => entry.key === key && entry.bytes === bytes.length && entry.etag === etag)) return
    await writeCached(dir, key, bytes.length, async writer => {
      await writer.write(bytes as Uint8Array<ArrayBuffer>)
      return etag
    }, true)
  }).catch(() => {})
}

export async function cacheVideo(
  url: string, key: string, size: number, signal: AbortSignal, progress: (bytes: number) => void,
  foreground: () => boolean = () => false,
): Promise<File> {
  return navigator.locks.request(directory, { ifAvailable: true }, async lock => {
    if (!lock) throw new Error('另一个标签页正在缓存视频')
    signal.throwIfAborted()
    if (size <= 0) throw new Error('空文件无法缓存')
    return writeCached(await root(), key, size, async writer => {
      const controller = new AbortController()
      const stop = () => controller.abort()
      signal.addEventListener('abort', stop, { once: true })
      if (signal.aborted) stop()
      const transfer = new MediaTransfer()
      let offset = 0
      let received = 0
      let writes = Promise.resolve()
      let etag = ''

      const part = async (chunk: number) => {
        controller.signal.throwIfAborted()
        const start = offset
        const end = Math.min(size, start + chunk) - 1
        offset = end + 1
        for (let attempt = 0; ; attempt++) {
          const headers: Record<string, string> = { Range: 'bytes=' + start + '-' + end }
          if (etag) headers['If-Range'] = etag
          let response: Response
          let data: Uint8Array<ArrayBuffer>
          const started = performance.now()
          let bodyStarted: number
          try {
            response = await fetch(url, { headers, signal: controller.signal })
            bodyStarted = performance.now()
          } catch (error) {
            if (attempt === 2 || controller.signal.aborted) throw error
            continue
          }
          const tag = response.headers.get('ETag')
          if (response.status !== 206 || !response.body || response.headers.get('Content-Range') !== 'bytes ' + start + '-' + end + '/' + size ||
            !strongMediaTag(tag) || (etag && tag !== etag)) {
            await response.body?.cancel()
            if (response.status >= 500 && attempt < 2) continue
            throw new Error(response.status === 429 ? '流量额度保护已开启，请稍后重试' : '视频版本已变化或分段无效，请重新打开视频')
          }
          etag = tag
          try {
            data = await readMediaBytes(response.body.getReader(), end - start + 1)
          } catch (error) {
            if (attempt === 2 || controller.signal.aborted) throw error
            continue
          }
          transfer.sample(data.byteLength, performance.now() - bodyStarted, bodyStarted - started)
          writes = writes.then(() => writer.write({ type: 'write', position: start, data }))
          await writes
          received += data.byteLength
          progress(received)
          return
        }
      }

      try {
        // Read one range before allowing other requests to pin the same version.
        await part(512 * 1024)
        while (offset < size) {
          const active = foreground()
          const chunk = active ? 512 * 1024 : Math.min(2 * MiB, transfer.playbackBytes(MiB, 3))
          const count = Math.min(transfer.cacheConcurrency(active), Math.ceil((size - offset) / chunk))
          const results = await Promise.allSettled(Array.from({ length: count }, () =>
            part(chunk).catch(error => { controller.abort(); throw error })))
          const failed = results.find(result => result.status === 'rejected')
          if (failed) throw failed.reason
        }
        controller.signal.throwIfAborted()
        return etag
      } finally {
        controller.abort()
        signal.removeEventListener('abort', stop)
      }
    }, false, signal)
  })
}
