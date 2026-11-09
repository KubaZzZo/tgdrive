import { MediaTransfer, readMediaBytes, withMediaTimeout } from './mediaTransfer'

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

function secondsAhead(buffered: TimeRanges, target: number, initialGap = false): number {
  for (let i = 0; i < buffered.length; i++) {
    if (target >= buffered.start(i) && target < buffered.end(i)) return buffered.end(i) - target
  }
  if (initialGap && buffered.length && target < buffered.start(0)) return buffered.end(0) - buffered.start(0)
  return 0
}

interface Fragment { offset: number; time: number; sync: boolean }

const recentSeekPoints = new Map<string, Map<number, Fragment>>()

function seekPoints(key: string | undefined): Map<number, Fragment> {
  if (key === undefined) return new Map()
  const points = recentSeekPoints.get(key) ?? new Map<number, Fragment>()
  recentSeekPoints.delete(key)
  recentSeekPoints.set(key, points)
  if (recentSeekPoints.size > 8) recentSeekPoints.delete(recentSeekPoints.keys().next().value!)
  return points
}

function seekTrack(bytes: Uint8Array) {
  const moov = boxes(bytes, 0, bytes.length).find(box => box.type === 'moov')
  if (!moov) return
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  for (const track of boxes(bytes, moov.data, moov.end).filter(box => box.type === 'trak')) {
    const children = boxes(bytes, track.data, track.end)
    const header = children.find(box => box.type === 'tkhd')
    const media = children.find(box => box.type === 'mdia')
    if (!header || !media) continue
    const fields = boxes(bytes, media.data, media.end)
    const handler = fields.find(box => box.type === 'hdlr')
    const clock = fields.find(box => box.type === 'mdhd')
    if (!handler || handler.data + 12 > handler.end || view.getUint32(handler.data + 8) !== 0x76696465 || !clock)
      continue
    const idOffset = header.data + (bytes[header.data] === 1 ? 20 : 12)
    const scaleOffset = clock.data + (bytes[clock.data] === 1 ? 20 : 12)
    if (idOffset + 4 > header.end || scaleOffset + 4 > clock.end) continue
    const id = view.getUint32(idOffset), timescale = view.getUint32(scaleOffset)
    if (!timescale) continue
    const mvex = boxes(bytes, moov.data, moov.end).find(box => box.type === 'mvex')
    const trex = mvex && boxes(bytes, mvex.data, mvex.end).find(box =>
      box.type === 'trex' && box.data + 24 <= box.end && view.getUint32(box.data + 4) === id)
    return { id, timescale, flags: trex ? view.getUint32(trex.data + 20) : 0, start: moov.end }
  }
}

type SeekTrack = NonNullable<ReturnType<typeof seekTrack>>

interface SegmentIndex { start: number; end: number; keyframes: Fragment[] }

function segmentIndexes(bytes: Uint8Array, track: SeekTrack, size: number): SegmentIndex[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const result: SegmentIndex[] = []
  for (const box of boxes(bytes, 0, bytes.length)) {
    if (box.type !== 'sidx') continue
    const version = bytes[box.data]
    if (version !== 0 && version !== 1) continue
    const countOffset = box.data + (version === 1 ? 30 : 22)
    if (countOffset + 2 > box.end || view.getUint32(box.data + 4) !== track.id) continue
    const scale = view.getUint32(box.data + 8)
    const count = view.getUint16(countOffset)
    if (!scale || !count || countOffset + 2 + count * 12 !== box.end) continue
    const start = version === 1 ? Number(view.getBigUint64(box.data + 12)) : view.getUint32(box.data + 12)
    let offset = box.end + (version === 1 ? Number(view.getBigUint64(box.data + 20)) : view.getUint32(box.data + 16))
    let time = start
    const keyframes: Fragment[] = []
    let valid = offset >= track.start && offset < size && Number.isSafeInteger(start)
    for (let entry = countOffset + 2; valid && entry < box.end; entry += 12) {
      const reference = view.getUint32(entry)
      const length = reference & 0x7fffffff
      const duration = view.getUint32(entry + 4)
      const access = view.getUint32(entry + 8)
      // Indirect indexes require an extra lookup. Use only direct references.
      valid = !(reference >>> 31) && length > 0 && duration > 0 && offset + length <= size &&
        Number.isSafeInteger(time + duration)
      // FFmpeg can set starts_with_SAP while leaving the type unspecified.
      const accessType = access >>> 28
      if (accessType >= 8 && accessType <= 10 && (access & 0x0fffffff) === 0)
        keyframes.push({ offset, time: time / scale, sync: true })
      offset += length
      time += duration
    }
    if (valid && keyframes.length) result.push({ start: start / scale, end: time / scale, keyframes })
  }
  return result
}

function fragments(bytes: Uint8Array, offset: number, track: SeekTrack): Fragment[] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  const result: Fragment[] = []
  // A byte probe usually starts inside mdat. Require a complete moof with its
  // movie-fragment header and a following mdat before trusting a signature.
  for (let tag = bytes.indexOf(0x6d, 4); tag >= 4 && tag + 12 <= bytes.length; tag = bytes.indexOf(0x6d, tag + 1)) {
    if (view.getUint32(tag) !== 0x6d6f6f66 || view.getUint32(tag + 8) !== 0x6d666864) continue
    const start = tag - 4, size = view.getUint32(start), end = start + size
    if (size < 24 || end + 8 > bytes.length || view.getUint32(end + 4) !== 0x6d646174) continue
    for (const traf of boxes(bytes, start + 8, end).filter(box => box.type === 'traf')) {
      const fields = boxes(bytes, traf.data, traf.end)
      const header = fields.find(box => box.type === 'tfhd')
      const decode = fields.find(box => box.type === 'tfdt')
      const run = fields.find(box => box.type === 'trun')
      if (!header || header.data + 8 > header.end || view.getUint32(header.data + 4) !== track.id ||
        !decode || !run || run.data + 8 > run.end || !view.getUint32(run.data + 4)) continue
      const wide = bytes[decode.data] === 1
      if (decode.data + (wide ? 12 : 8) > decode.end) continue
      const time = (wide ? Number(view.getBigUint64(decode.data + 4)) : view.getUint32(decode.data + 4)) / track.timescale
      const headerFlags = view.getUint32(header.data)
      const runFlags = view.getUint32(run.data)
      let flags = track.flags
      if (headerFlags & 0x20) {
        const index = header.data + 8 + (headerFlags & 1 ? 8 : 0) + (headerFlags & 2 ? 4 : 0) +
          (headerFlags & 8 ? 4 : 0) + (headerFlags & 16 ? 4 : 0)
        if (index + 4 > header.end) continue
        flags = view.getUint32(index)
      }
      if (runFlags & 0x404) {
        let index = run.data + 8 + (runFlags & 1 ? 4 : 0)
        if (!(runFlags & 4)) index += (runFlags & 0x100 ? 4 : 0) + (runFlags & 0x200 ? 4 : 0)
        if (index + 4 > run.end) continue
        flags = view.getUint32(index)
      }
      result.push({ offset: offset + start, time, sync: !(flags & 0x10000) && ((flags >>> 24) & 3) !== 1 })
      break
    }
    // Skip the payload of the recognized fragment, even for audio-only moofs.
    const mdatSize = view.getUint32(end)
    tag = mdatSize >= 8 ? end + mdatSize - 4 : end + 3
  }
  return result
}

async function seekOffset(
  target: number, duration: number, size: number, track: SeekTrack, known: Map<number, Fragment>,
  indexes: SegmentIndex[],
  read: (start: number, end: number, signal: AbortSignal) => Promise<Uint8Array>,
  signal: AbortSignal, probeSignal: AbortSignal,
): Promise<number | null> {
  // Start a little before the requested frame so separately stored audio and
  // video fragments can both cover it.
  const goal = target - 4
  if (goal <= 6) return track.start
  for (const index of indexes) {
    if (goal < index.start || goal >= index.end) continue
    for (let i = index.keyframes.length - 1; i >= 0; i--) {
      const frame = index.keyframes[i]!
      if (frame.time > goal) continue
      if (goal - frame.time <= 45) return frame.offset
      break
    }
  }
  let best: Fragment = { offset: track.start, time: 0, sync: true }
  let low = best, high = { offset: size, time: duration }
  function bracket(fragment: Fragment) {
    if (fragment.time <= goal) {
      if (fragment.offset > low.offset) low = fragment
      if (fragment.sync && fragment.time > best.time) best = fragment
    } else if (fragment.offset < high.offset) high = fragment
  }
  function remember(fragment: Fragment) {
    known.delete(fragment.offset)
    known.set(fragment.offset, fragment)
    if (known.size > 256) known.delete(known.keys().next().value!)
  }
  known.forEach(bracket)
  let limit = high.offset
  const window = 512 * 1024
  let remaining = 8 * 1024 * 1024
  probes: for (let probe = 0; probe < 14 && remaining && goal - best.time > 5 && limit > low.offset + 8; probe++) {
    const span = limit - low.offset
    const fraction = (goal - low.time) / (high.time - low.time)
    const estimate = (high.offset - low.offset) * fraction
    // Keep the estimate close to the expected time even when the byte bracket
    // is wide. Periodically bisect to make progress on irregular bitrates.
    const guard = Math.min(window, span / 8)
    const step = probe % 4 === 3 ? span / 2 : Math.max(guard, Math.min(span - guard, estimate))
    const start = low.offset + Math.max(1, Math.floor(step))
    let found: Fragment[] = []
    let bytes = new Uint8Array(0)
    // Large fragments may need a wider probe to reach the next video header.
    for (const length of [window, 4 * window, 16 * window]) {
      const next = start + bytes.length
      const end = Math.min(high.offset - 1, start + length - 1, next + remaining - 1)
      if (end < next) break
      remaining -= end - next + 1
      let more: Uint8Array
      try { more = await read(next, end, probeSignal) }
      catch (error) {
        signal.throwIfAborted()
        if (probeSignal.aborted) break probes
        throw error
      }
      const expanded = new Uint8Array(bytes.length + more.length)
      expanded.set(bytes)
      expanded.set(more, bytes.length)
      bytes = expanded
      found = fragments(bytes, start, track)
      if (found.length || end === high.offset - 1) break
    }
    if (!found.length) { limit = start; continue }
    for (const fragment of found) {
      remember(fragment)
      bracket(fragment)
    }
    limit = high.offset
  }
  signal.throwIfAborted()
  if (goal - best.time > 45) return null
  remember(best)
  return best.offset
}

export interface MediaSession { streaming: boolean; dispose: () => void }
export interface MediaPrefix { bytes: Uint8Array; etag: string }

export async function attachMedia(
  media: HTMLMediaElement, url: string, signal: AbortSignal, onError: () => void,
  options: {
    file?: Blob; bufferSeconds?: () => number | undefined; version?: string
    size?: number; prefix?: MediaPrefix; onPrefix?: (prefix: MediaPrefix) => void
    onSeekUnavailable?: () => void
  } = {},
): Promise<MediaSession> {
  const native = () => {
    const objectUrl = options.file ? URL.createObjectURL(options.file) : null
    if (!signal.aborted) media.src = objectUrl ?? url
    return { streaming: false, dispose: () => { if (objectUrl) URL.revokeObjectURL(objectUrl) } }
  }
  // iPhone exposes ManagedMediaSource rather than the desktop MediaSource API.
  const ManagedSource = (globalThis as typeof globalThis & {
    ManagedMediaSource?: typeof MediaSource
  }).ManagedMediaSource
  const Source = ManagedSource ?? (typeof MediaSource !== 'undefined' ? MediaSource : undefined)
  if (!Source) return native()
  const transfer = new MediaTransfer(Source === ManagedSource ? 24 : 48)
  let rangeVersion: string | null = options.prefix?.etag ?? null
  const range = async (start: number, end: number, requestSignal = signal) => {
    requestSignal.throwIfAborted()
    if (options.file) return { reader: options.file.slice(start, end + 1).stream().getReader(), size: options.file.size, latency: 0 }
    const headers: Record<string, string> = { Range: `bytes=${start}-${end}` }
    if (rangeVersion) headers['If-Range'] = rangeVersion
    const started = performance.now()
    const response = await fetch(url, { headers, signal: requestSignal })
    const latency = performance.now() - started
    if (response.status !== 206 || !response.body) {
      await response.body?.cancel()
      throw new Error('Byte ranges unavailable')
    }
    const match = /^bytes (\d+)-(\d+)\/(\d+)$/.exec(response.headers.get('Content-Range') ?? '')
    if (!match || Number(match[1]) !== start || Number(match[2]) !== Math.min(end, Number(match[3]) - 1)) {
      await response.body.cancel(); throw new Error('Invalid media range')
    }
    const version = response.headers.get('ETag')
    if (rangeVersion && version !== rangeVersion) {
      await response.body.cancel(); throw new Error('Media changed during playback')
    }
    rangeVersion = version
    return { reader: response.body.getReader(), size: Number(match[3]), latency }
  }
  const readHeader = async () => {
    if (options.prefix && options.size) return { first: options.prefix.bytes, size: options.size }
    const initial = await range(0, 65535)
    const first = await readMediaBytes(initial.reader, Math.min(65536, initial.size))
    transfer.sample(0, 0, initial.latency)
    return { first, size: initial.size }
  }
  let first: Uint8Array, size: number
  try {
    ({ first, size } = await readHeader())
  } catch { return native() }
  const type = fragmentedMp4Type(first)
  if (!type || !Source.isTypeSupported(type) || signal.aborted) return native()
  if (!options.prefix && rangeVersion) options.onPrefix?.({ bytes: first, etag: rangeVersion })
  const managed = Source === ManagedSource
  const source = new Source() as MediaSource & { readonly streaming?: boolean }
  // Without an HLS AirPlay alternative, WebKit requires remote playback to be
  // disabled *before* attaching MMS or it never opens on iPhone.
  const previousRemotePlayback = media.disableRemotePlayback
  if (managed) media.disableRemotePlayback = true
  const objectUrl = URL.createObjectURL(source)
  media.src = objectUrl
  media.load()
  let request = new AbortController()
  const abort = () => request.abort()
  signal.addEventListener('abort', abort, { once: true })
  const dispose = () => {
    abort()
    signal.removeEventListener('abort', abort)
    URL.revokeObjectURL(objectUrl)
    if (managed) media.disableRemotePlayback = previousRemotePlayback
  }
  const wait = (requestSignal = signal) => new Promise<void>((resolve, reject) => {
    const stop = () => { clearTimeout(timer); reject(requestSignal.reason) }
    const timer = setTimeout(() => { requestSignal.removeEventListener('abort', stop); resolve() }, 200)
    requestSignal.addEventListener('abort', stop, { once: true })
    if (requestSignal.aborted) stop()
  })
  const reopen = () => {
    if (source.readyState === 'ended') source.duration = media.duration
  }
  const readProbe = async (from: number, to: number, requestSignal: AbortSignal) => {
    const { reader, size: total } = await range(from, to, requestSignal)
    if (total !== size) {
      await reader.cancel()
      throw new Error('Media changed during playback')
    }
    return readMediaBytes(reader, to - from + 1)
  }
  const pump = async () => {
    const deadline = Date.now() + 15000
    while (source.readyState !== 'open') {
      if (signal.aborted) return
      if (Date.now() > deadline || media.error) throw new Error('Media source did not open')
      await wait()
    }
    const buffer = source.addSourceBuffer(type)
    const change = (operation: () => void) => new Promise<void>((resolve, reject) => {
      if (signal.aborted) { reject(new DOMException('Aborted', 'AbortError')); return }
      const clean = () => { buffer.removeEventListener('updateend', done); buffer.removeEventListener('error', failed); signal.removeEventListener('abort', failed) }
      const done = () => { clean(); resolve() }, failed = () => { clean(); reject(new Error('Media append failed')) }
      buffer.addEventListener('updateend', done, { once: true }); buffer.addEventListener('error', failed, { once: true }); signal.addEventListener('abort', failed, { once: true })
      try { operation() } catch (error) { clean(); reject(error) }
    })
    const append = async (bytes: Uint8Array) => {
      try { await change(() => buffer.appendBuffer(bytes as Uint8Array<ArrayBuffer>)) }
      catch (error) {
        // A quota error rejects before consuming these bytes. Evict old playback
        // data and retry the same bytes once, without refetching the range.
        if (!(error instanceof DOMException) || error.name !== 'QuotaExceededError' || media.currentTime <= 10)
          throw error
        const cutoff = media.currentTime - 10
        if (!buffer.buffered.length || buffer.buffered.start(0) >= cutoff) throw error
        await change(() => buffer.remove(0, cutoff))
        await change(() => buffer.appendBuffer(bytes as Uint8Array<ArrayBuffer>))
      }
    }
    // MMS can suspend downloads even while playing. Respect the browser's
    // startstreaming/endstreaming state instead of filling its mobile buffer.
    const streamingWindow = async (requestSignal = signal) => {
      while (managed && source.streaming === false && !requestSignal.aborted) await wait(requestSignal)
      requestSignal.throwIfAborted()
    }
    const track = seekTrack(first)
    const indexes = track ? segmentIndexes(first, track, size) : []
    const known = seekPoints(track && options.version !== undefined
      ? JSON.stringify([url, options.version, size]) : undefined)
    let target: number | undefined
    let lastPosition = media.currentTime
    const rememberPosition = () => {
      if (!media.seeking && target === undefined) lastPosition = media.currentTime
    }
    const seek = () => {
      if (!track || !Number.isFinite(media.duration)) return
      if (target === undefined && secondsAhead(media.buffered, media.currentTime)) return
      target = media.currentTime
      request.abort()
    }
    if (track) {
      media.addEventListener('seeking', seek)
      media.addEventListener('timeupdate', rememberPosition)
    }
    try {
      await streamingWindow()
      await append(first)
      let offset = first.length
      while (!signal.aborted) {
        request = new AbortController()
        const requestSignal = request.signal
        let failures = 0
        try {
          if (target !== undefined && track && !secondsAhead(media.buffered, target)) {
            const requested = target
            const start = await withMediaTimeout(requestSignal, 2500, probeSignal =>
              seekOffset(requested, media.duration, size, track, known, indexes, readProbe, requestSignal, probeSignal))
            requestSignal.throwIfAborted()
            if (start === null) {
              if (!secondsAhead(media.buffered, lastPosition)) throw new Error('No nearby random-access fragment')
              media.currentTime = lastPosition
              options.onSeekUnavailable?.()
            } else {
              reopen()
              buffer.abort()
              offset = start
              if (buffer.buffered.length) await change(() => buffer.remove(0, source.duration))
            }
          }
          requestSignal.throwIfAborted()
          target = undefined
          while (offset < size) {
            const bitrate = Number.isFinite(media.duration) && media.duration > 0 ? size / media.duration : 0
            while (secondsAhead(media.buffered, media.currentTime, media.paused && media.currentTime === 0) >
              transfer.bufferSeconds(bitrate, media.paused, globalThis.document?.hidden ?? false, options.bufferSeconds?.(), media.playbackRate))
              await wait(requestSignal)
            await streamingWindow(requestSignal)
            const cutoff = media.currentTime - transfer.historySeconds(bitrate)
            if (cutoff > 0 && buffer.buffered.length && buffer.buffered.start(0) < cutoff - 1)
              await change(() => buffer.remove(0, cutoff))
            try {
              const bytes = transfer.playbackBytes(bitrate, secondsAhead(media.buffered, media.currentTime))
              const from = offset
              const end = Math.min(size - 1, offset + bytes - 1)
              const { reader, size: total, latency } = await range(offset, end, requestSignal)
              const bodyStarted = performance.now()
              let lastByte = bodyStarted
              try {
                if (total !== size) throw new Error('Media changed during playback')
                while (true) {
                  requestSignal.throwIfAborted()
                  const { done, value } = await reader.read()
                  if (done) break
                  lastByte = performance.now()
                  if (offset + value.length > end + 1) throw new Error('Oversized media segment')
                  await append(value)
                  offset += value.length
                }
                if (offset !== end + 1) throw new Error('Incomplete media segment')
                if (!options.file) transfer.sample(offset - from, lastByte - bodyStarted, latency)
                failures = 0
              } finally { await reader.cancel() }
            } catch (error) {
              requestSignal.throwIfAborted()
              if (++failures > 2 || media.error) throw error
              // Resume after the last successfully appended byte.
              await wait(requestSignal)
            }
          }
          requestSignal.throwIfAborted()
          if (source.readyState === 'open') source.endOfStream()
          if (!track) return
          // An unbuffered seek can reopen a source after the last byte arrived.
          await new Promise<void>(resolve => requestSignal.addEventListener('abort', () => resolve(), { once: true }))
        } catch (error) {
          if (!requestSignal.aborted) throw error
        }
      }
    } finally {
      if (track) {
        media.removeEventListener('seeking', seek)
        media.removeEventListener('timeupdate', rememberPosition)
      }
    }
  }
  void pump().catch(() => { if (!signal.aborted) onError() })
  return { streaming: true, dispose }
}
