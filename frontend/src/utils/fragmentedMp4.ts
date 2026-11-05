// Only inspect complete boxes in the bounded initial range. Other formats use
// the native player; malformed or unusually large headers never trigger a scan.
interface Box { type: string; start: number; end: number; data: number }
function boxes(bytes: Uint8Array, start: number, end: number): Box[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const result: Box[] = []
  while (start + 8 <= end) {
    const size = view.getUint32(start)
    if (size < 8 || start + size > end) break
    result.push({ type: String.fromCharCode(...bytes.subarray(start + 4, start + 8)), start, end: start + size, data: start + 8 })
    start += size
  }
  return result
}

export function fragmentedMp4Type(bytes: Uint8Array): string | null {
  const child = (box: Box, type: string, skip = 0) => boxes(bytes, box.data + skip, box.end).find(b => b.type === type)
  const moov = boxes(bytes, 0, bytes.length).find(b => b.type === 'moov')
  if (!moov || !child(moov, 'mvex')) return null
  const codecs: string[] = []
  for (const track of boxes(bytes, moov.data, moov.end).filter(b => b.type === 'trak')) {
    const mdia = child(track, 'mdia'), minf = mdia && child(mdia, 'minf'), stbl = minf && child(minf, 'stbl'), stsd = stbl && child(stbl, 'stsd')
    if (!stsd) return null
    const entries = boxes(bytes, stsd.data + 8, stsd.end)
    if (entries.length !== 1) return null
    const entry = entries[0]!
    if (entry.type === 'avc1' || entry.type === 'avc3') {
      const config = child(entry, 'avcC', 78)
      if (!config || config.end - config.data < 4) return null
      codecs.push(`${entry.type}.${Array.from(bytes.subarray(config.data + 1, config.data + 4), n => n.toString(16).padStart(2, '0')).join('')}`)
    } else if (entry.type === 'mp4a') {
      const esds = child(entry, 'esds', 28)
      if (!esds) return null
      // ISO ES_Descriptor -> DecoderConfigDescriptor -> DecoderSpecificInfo.
      let position = esds.data + 4
      const descriptor = (tag: number, limit: number): number | null => {
        if (bytes[position++] !== tag) return null
        let size = 0, count = 0, value: number
        do {
          if (position >= limit || count++ === 4) return null
          value = bytes[position++]!; size = size * 128 + (value & 127)
        } while (value & 128)
        return position + size <= limit ? position + size : null
      }
      const esEnd = descriptor(3, esds.end)
      if (!esEnd || position + 3 > esEnd) return null
      position += 2
      const flags = bytes[position++]!
      if (flags & 128) position += 2
      if (flags & 64) position += 1 + (bytes[position] ?? esEnd)
      if (flags & 32) position += 2
      const decoderEnd = descriptor(4, esEnd)
      if (!decoderEnd || position + 13 > decoderEnd || bytes[position] !== 0x40) return null
      position += 13
      const specificEnd = descriptor(5, decoderEnd)
      if (!specificEnd || position >= specificEnd) return null
      const audioType = bytes[position]! >> 3
      if (audioType < 1 || audioType >= 31) return null
      codecs.push(`mp4a.40.${audioType}`)
    } else return null
  }
  return codecs.length ? `video/mp4; codecs="${codecs.join(',')}"` : null
}

export function bufferedSeek(buffered: TimeRanges, target: number): number | null {
  let closest: number | null = null
  for (let i = 0; i < buffered.length; i++) {
    const start = buffered.start(i), end = Math.max(start, buffered.end(i) - 0.1)
    const value = Math.max(start, Math.min(end, target))
    if (closest === null || Math.abs(value - target) < Math.abs(closest - target)) closest = value
  }
  return closest
}

export interface MediaSession { streaming: boolean; dispose: () => void }

export async function attachMedia(
  media: HTMLMediaElement, url: string, signal: AbortSignal, onError: () => void,
): Promise<MediaSession> {
  const native = () => {
    if (!signal.aborted) media.src = url
    return { streaming: false, dispose: () => {} }
  }
  if (typeof MediaSource === 'undefined') return native()
  const range = async (start: number, end: number) => {
    const response = await fetch(url, { headers: { Range: `bytes=${start}-${end}` }, signal })
    if (response.status !== 206 || !response.body) {
      await response.body?.cancel()
      throw new Error('Byte ranges unavailable')
    }
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('Content-Range') ?? '')
    if (!match || Number(match[1]) !== start || Number(match[2]) > end) {
      await response.body.cancel(); throw new Error('Invalid media range')
    }
    return { reader: response.body.getReader(), size: Number(match[3]) }
  }
  let first: Uint8Array, size: number
  try {
    const initial = await range(0, 65535)
    size = initial.size
    const chunks: Uint8Array[] = []; let length = 0
    try {
      while (true) {
        const { done, value } = await initial.reader.read()
        if (done) break
        length += value.length
        if (length > 65536) throw new Error('Oversized media header')
        chunks.push(value)
      }
    } finally { await initial.reader.cancel() }
    first = new Uint8Array(length); let cursor = 0
    for (const chunk of chunks) { first.set(chunk, cursor); cursor += chunk.length }
  } catch { return native() }
  const type = fragmentedMp4Type(first)
  if (!type || !MediaSource.isTypeSupported(type) || signal.aborted) return native()
  const source = new MediaSource(), objectUrl = URL.createObjectURL(source)
  media.src = objectUrl
  const abort = () => { if (source.readyState === 'open') { try { source.endOfStream() } catch { /* Updating buffer. */ } } }
  signal.addEventListener('abort', abort, { once: true })
  const dispose = () => { signal.removeEventListener('abort', abort); URL.revokeObjectURL(objectUrl) }
  const wait = () => new Promise<void>((resolve, reject) => {
    const stop = () => { clearTimeout(timer); reject(new DOMException('Aborted', 'AbortError')) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', stop); resolve() }, 200)
    signal.addEventListener('abort', stop, { once: true })
    if (signal.aborted) stop()
  })
  const pump = async () => {
    while (source.readyState !== 'open') { if (signal.aborted) return; await wait() }
    const buffer = source.addSourceBuffer(type)
    const change = (operation: () => void) => new Promise<void>((resolve, reject) => {
      if (signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return }
      const clean = () => { buffer.removeEventListener('updateend', done); buffer.removeEventListener('error', failed); signal.removeEventListener('abort', failed) }
      const done = () => { clean(); resolve() }, failed = () => { clean(); reject(new Error('Media append failed')) }
      buffer.addEventListener('updateend', done, { once: true }); buffer.addEventListener('error', failed, { once: true }); signal.addEventListener('abort', failed, { once: true })
      try { operation() } catch (error) { clean(); reject(error) }
    })
    await change(() => buffer.appendBuffer(first as Uint8Array<ArrayBuffer>))
    let offset = first.length
    while (offset < size && !signal.aborted) {
      // Keep preload, paused playback and normal buffering bounded. Closing the
      // modal aborts the active fetch; no whole-file background download.
      while (!signal.aborted && media.buffered.length &&
        media.buffered.end(media.buffered.length - 1) - media.currentTime > (media.paused ? 5 : 30)) await wait()
      if (signal.aborted) return
      if (media.currentTime > 90 && buffer.buffered.length && buffer.buffered.start(0) < media.currentTime - 60)
        await change(() => buffer.remove(0, media.currentTime - 60))
      const { reader } = await range(offset, Math.min(size - 1, offset + 2 * 1024 * 1024 - 1))
      let received = 0
      try {
        while (!signal.aborted) {
          const { done, value } = await reader.read()
          if (done) break
          received += value.length
          if (received > 2 * 1024 * 1024) throw new Error('Oversized media segment')
          await change(() => buffer.appendBuffer(value as Uint8Array<ArrayBuffer>))
        }
      } finally { await reader.cancel() }
      if (!received) throw new Error('Incomplete media segment')
      offset += received
    }
    if (!signal.aborted && source.readyState === 'open') source.endOfStream()
  }
  void pump().catch(() => { if (!signal.aborted) onError() })
  return { streaming: true, dispose }
}
