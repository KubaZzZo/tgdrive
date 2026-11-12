import { createFile, MP4BoxBuffer } from 'mp4box'
import { boxes, type MediaSession, type MediaStage } from './fragmentedMp4'
import { MediaTransfer, readMediaBytes, withMediaTimeout } from './mediaTransfer'

const MiB = 1024 * 1024
const metadataLimit = 8 * MiB
const sampleBudget = 32 * MiB
const sampleCountLimit = 300_000
const seekPreroll = 0.25
type ReadRange = (start: number, end: number, signal: AbortSignal) => Promise<{
  reader: ReadableStreamDefaultReader<Uint8Array>; size: number; latency: number; local: boolean
}>

// Fast-start MP4 stores its complete sample index before the media payload.
function movieEnd(prefix: Uint8Array, size: number): number {
  const view = new DataView(prefix.buffer, prefix.byteOffset, prefix.byteLength)
  for (let offset = 0; offset + 8 <= prefix.length;) {
    const type = String.fromCharCode(...prefix.subarray(offset + 4, offset + 8))
    let length = view.getUint32(offset)
    const headerLength = length === 1 ? 16 : 8
    if (headerLength === 16) {
      if (offset + headerLength > prefix.length) return 0
      length = Number(view.getBigUint64(offset + 8))
    }
    const end = offset + length
    if (length < headerLength || end > size || end > metadataLimit) return 0
    if (type === 'moov') return end
    if (!['ftyp', 'free', 'skip', 'wide', 'pdin'].includes(type)) return 0
    offset = end
  }
  return 0
}

export function hasSafeSampleTables(header: Uint8Array): boolean {
  const view = new DataView(header.buffer, header.byteOffset, header.byteLength)
  const moov = boxes(header, 0, header.length, 128).find(box => box.type === 'moov')
  if (!moov) return false
  const tracks = boxes(header, moov.data, moov.end, 128).filter(box => box.type === 'trak')
  if (!tracks.length || tracks.length > 2) return false
  let total = 0
  const knownTables = new Set<number>()
  for (const track of tracks) {
    const media = boxes(header, track.data, track.end, 128).find(box => box.type === 'mdia')
    const info = media && boxes(header, media.data, media.end, 128).find(box => box.type === 'minf')
    const table = info && boxes(header, info.data, info.end, 128).find(box => box.type === 'stbl')
    const sizes = table && boxes(header, table.data, table.end, 128).filter(box => box.type === 'stsz' || box.type === 'stz2')
    if (sizes?.length !== 1) return false
    const [entry] = sizes
    if (!entry || entry.data + 12 > entry.end) return false
    knownTables.add(entry.start + 4)
    const count = view.getUint32(entry.data + 8)
    total += count
    // The parser expands even a constant-size table into an array of samples.
    if (!count || total > sampleCountLimit) return false
    const bits = entry.type === 'stsz'
      ? (view.getUint32(entry.data + 4) ? 0 : 32)
      : view.getUint8(entry.data + 7)
    if (entry.type === 'stz2' && ![4, 8, 16].includes(bits)) return false
    if (entry.data + 12 + Math.ceil(count * bits / 8) > entry.end) return false
  }
  // MP4Box can dispatch these parsers even when a box is nested in an
  // unexpected container. Send unfamiliar layouts to the browser.
  for (let offset = 4; offset + 4 <= header.length; offset++) {
    const type = view.getUint32(offset)
    if (type === 0x7472756e) return false // trun
    if ((type === 0x7374737a || type === 0x73747a32) && !knownTables.has(offset)) return false
  }
  return true
}

function wait(signal: AbortSignal): Promise<void> {
  signal.throwIfAborted()
  return new Promise((resolve, reject) => {
    const abort = () => { clearTimeout(timer); reject(signal.reason) }
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve() }, 150)
    signal.addEventListener('abort', abort, { once: true })
  })
}

function bufferedAhead(media: HTMLMediaElement, time: number): number {
  const ranges = media.buffered
  for (let i = 0; i < ranges.length; i++) {
    if (ranges.start(i) <= time + 0.05 && time < ranges.end(i)) return ranges.end(i) - time
  }
  return 0
}

export async function attachOrdinaryMp4(
  media: HTMLMediaElement, Source: typeof MediaSource, managed: boolean, prefix: Uint8Array,
  size: number, signal: AbortSignal, read: ReadRange, onError: () => void,
  bufferSeconds?: () => number | undefined,
  onStage?: (stage: MediaStage) => void,
): Promise<MediaSession | null> {
  signal.throwIfAborted()
  const end = movieEnd(prefix, size)
  if (!end) return null
  const header = new Uint8Array(end)
  header.set(prefix.subarray(0, end))
  try {
    await withMediaTimeout(signal, 4_000, async request => {
      if (prefix.length >= end) return
      const range = await read(prefix.length, end - 1, request)
      if (range.size !== size) {
        await range.reader.cancel()
        throw new Error('Media size changed')
      }
      header.set(await readMediaBytes(range.reader, end - prefix.length), prefix.length)
    })
  } catch { return null }
  if (!hasSafeSampleTables(header)) return null

  const queued: ArrayBuffer[] = []
  const parser = (validate = false) => {
    const file = createFile(true)
    file.appendBuffer(MP4BoxBuffer.fromArrayBuffer(header.buffer, 0))
    const info = file.getInfo()
    const duration = info.duration / info.timescale
    if (info.isFragmented || !(duration > 0 && Number.isFinite(duration)) ||
      info.videoTracks.length !== 1 || info.audioTracks.length > 1 ||
      info.tracks.length !== info.videoTracks.length + info.audioTracks.length ||
      !/^avc[13]\./.test(info.videoTracks[0]?.codec ?? '') ||
      info.tracks.some(track => !(track.duration / track.timescale >= duration - seekPreroll)) ||
      info.audioTracks.some(track => !/^mp4a\.40\./.test(track.codec))) return null
    const type = 'video/mp4; codecs="' + info.tracks.map(track => track.codec).join(',') + '"'
    if (!Source.isTypeSupported(type)) return null
    if (validate && info.tracks.some(track => {
      const samples = file.getTrackById(track.id).samples
      return !samples.length || samples.some(sample =>
        !Number.isSafeInteger(sample.offset) || sample.offset < end ||
        sample.offset + sample.size > size || sample.size > sampleBudget,
      )
    })) return null
    // MP4Box requires every segmented track to use the same sample count.
    const samples = Math.max(1, Math.min(120, Math.ceil(info.videoTracks[0].nb_samples / duration / 3)))
    for (const track of info.tracks)
      file.setSegmentOptions(track.id, undefined, { nbSamples: samples, rapAlignement: false })
    file.onSegment = (id, _, bytes, nextSample) => {
      queued.push(bytes)
      file.releaseUsedSamples(id, nextSample)
    }
    return { file, duration, type, init: file.initializeSegmentation().buffer }
  }
  let initial: ReturnType<typeof parser>
  try { initial = parser(true) } catch { return null }
  if (!initial || signal.aborted) return null
  onStage?.('remux-ready')

  const source = new Source() as MediaSource & { readonly streaming?: boolean }
  if (onStage) source.addEventListener('sourceopen', () => onStage('source-open'), { once: true, signal })
  const objectUrl = URL.createObjectURL(source)
  const previousRemotePlayback = media.disableRemotePlayback
  if (managed) media.disableRemotePlayback = true
  media.src = objectUrl
  media.load()
  let active = new AbortController()
  let disposed = false
  let pendingSeek: number | undefined
  let repositioning = false
  const abort = () => active.abort()
  const seek = () => {
    if (!repositioning && bufferedAhead(media, media.currentTime)) return
    repositioning = true
    pendingSeek = media.currentTime
    abort()
  }
  signal.addEventListener('abort', abort, { once: true })
  media.addEventListener('seeking', seek)
  const dispose = () => {
    disposed = true
    abort()
    signal.removeEventListener('abort', abort)
    media.removeEventListener('seeking', seek)
    URL.revokeObjectURL(objectUrl)
    if (managed) media.disableRemotePlayback = previousRemotePlayback
  }

  const pump = async () => {
    let state = initial
    if (!state) return
    initial = null
    while (source.readyState !== 'open' || (managed && source.streaming === false)) {
      if (media.error) throw new Error('Media source did not open')
      await wait(signal)
    }
    const buffer = source.addSourceBuffer(state.type)
    function change(operation: () => void): Promise<void> {
      signal.throwIfAborted()
      if (disposed) return Promise.reject(new DOMException('Media session disposed', 'AbortError'))
      return new Promise((resolve, reject) => {
        const clean = () => {
          buffer.removeEventListener('updateend', done)
          buffer.removeEventListener('error', failed)
          signal.removeEventListener('abort', failed)
        }
        const done = () => { clean(); resolve() }
        const failed = () => { clean(); reject(new Error('Media append failed')) }
        buffer.addEventListener('updateend', done, { once: true })
        buffer.addEventListener('error', failed, { once: true })
        signal.addEventListener('abort', failed, { once: true })
        try { operation() } catch (error) { clean(); reject(error) }
      })
    }
    const transfer = new MediaTransfer(managed ? 24 : 48)
    const duration = state.duration
    const bitrate = size / duration
    async function append(bytes: ArrayBuffer): Promise<void> {
      try { await change(() => buffer.appendBuffer(bytes)) }
      catch (error) {
        const cutoff = media.currentTime - 2
        if (!(error instanceof DOMException) || error.name !== 'QuotaExceededError' ||
          !buffer.buffered.length || buffer.buffered.start(0) >= cutoff) throw error
        await change(() => buffer.remove(0, cutoff))
        await change(() => buffer.appendBuffer(bytes))
      }
    }
    source.duration = duration
    await append(state.init)
    let offset = state.file.seek(0, true).offset
    state.file.start()
    while (!signal.aborted && !disposed) {
      active = new AbortController()
      const request = active.signal
      try {
        if (pendingSeek !== undefined) {
          const target = pendingSeek
          pendingSeek = undefined
          if (!bufferedAhead(media, target)) {
            state.file.stop()
            queued.length = 0
            if (buffer.buffered.length) await change(() => buffer.remove(0, duration))
            if (source.readyState === 'open') buffer.abort()
            const replacement = parser()
            if (!replacement) throw new Error('MP4 index no longer available')
            state = replacement
            await append(state.init)
            offset = state.file.seek(Math.max(0, target - seekPreroll), true).offset
            state.file.start()
          }
        }
        request.throwIfAborted()
        if (!Number.isSafeInteger(offset) || offset < end || offset > size) throw new Error('Invalid MP4 sample offset')
        if (offset === size) {
          if (source.readyState === 'open') {
            state.file.flush()
            for (const bytes of queued.splice(0)) await append(bytes)
            source.endOfStream()
          }
          await wait(request)
          continue
        }
        while ((managed && source.streaming === false) ||
          bufferedAhead(media, media.currentTime) > transfer.bufferSeconds(
            bitrate, media.paused, globalThis.document?.hidden ?? false, bufferSeconds?.(), media.playbackRate,
          )) await wait(request)
        const cutoff = media.currentTime - transfer.historySeconds(bitrate)
        if (cutoff > 0 && buffer.buffered.length && buffer.buffered.start(0) < cutoff - 1)
          await change(() => buffer.remove(0, cutoff))
        request.throwIfAborted()
        const ahead = bufferedAhead(media, media.currentTime)
        const length = Math.min(size - offset, transfer.playbackBytes(bitrate, ahead))
        let data: Uint8Array<ArrayBuffer>
        for (let attempt = 0; ; attempt++) {
          try {
            data = await withMediaTimeout(request, 15_000, async rangeSignal => {
              const range = await read(offset, offset + length - 1, rangeSignal)
              if (range.size !== size) {
                await range.reader.cancel()
                throw new Error('Media size changed')
              }
              const started = performance.now()
              const bytes = await readMediaBytes(range.reader, length)
              if (!range.local) transfer.sample(length, performance.now() - started, range.latency)
              return bytes
            })
            break
          } catch (error) {
            if (attempt === 2 || request.aborted) throw error
            await wait(request)
          }
        }
        request.throwIfAborted()
        // Use original file offsets. The sample table already describes the
        // payload, so parsing mdat as another box is unnecessary on each seek.
        state.file.stream.insertBuffer(MP4BoxBuffer.fromArrayBuffer(data.buffer, offset))
        state.file.processSamples(false)
        state.file.stream.cleanBuffers()
        const retained = state.file.stream.buffers.reduce((total, item) => total + item.byteLength, 0)
        if (retained + state.file.getAllocatedSampleDataSize() > sampleBudget)
          throw new Error('MP4 track data exceeds the memory budget')
        offset += length
        // The parser has consumed these samples. Finish appending them even
        // if a new seek cancels the current read before choosing its anchor.
        for (const bytes of queued.splice(0)) await append(bytes)
        if (bufferedAhead(media, media.currentTime)) repositioning = false
      } catch (error) {
        if (request.aborted) continue
        throw error
      }
    }
  }
  void pump().catch(() => { if (!signal.aborted && !disposed) onError() })
  return { streaming: true, dispose }
}
