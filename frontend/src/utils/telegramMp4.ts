import { ISOFile, MP4BoxBuffer } from 'mp4box'
import type { Movie } from 'mp4box'
import type { TelegramMediaSource } from './telegramMedia'

function mediaSourceType(): typeof MediaSource | undefined {
  return window.MediaSource ?? (window as Window & { ManagedMediaSource?: typeof MediaSource }).ManagedMediaSource
}

export function supportsTelegramMp4(): boolean {
  return Boolean(mediaSourceType())
}

function waitForEvent(target: EventTarget, name: string, signal: AbortSignal, action?: () => void): Promise<void> {
  return new Promise((resolve, reject) => {
    signal.throwIfAborted()
    const listeners = new AbortController()
    function finish(error?: unknown): void {
      listeners.abort()
      if (error) reject(error)
      else resolve()
    }
    const options = { once: true, signal: listeners.signal }
    signal.addEventListener('abort', () => finish(signal.reason), options)
    target.addEventListener(name, () => finish(), options)
    target.addEventListener('error', () => finish(new Error('浏览器无法处理此 MP4 视频。')), options)
    try { action?.() }
    catch (error) { finish(error) }
  })
}

function bufferedUntil(media: HTMLMediaElement, time: number): number {
  for (let index = 0; index < media.buffered.length; index++) {
    if (media.buffered.start(index) <= time + 0.1 && media.buffered.end(index) > time) {
      return media.buffered.end(index)
    }
  }
  return time
}

export async function playTelegramMp4(source: TelegramMediaSource, video: HTMLMediaElement, signal: AbortSignal): Promise<void> {
  const Source = mediaSourceType()
  if (!Source) throw new Error('当前 WebView 无法播放 Telegram 直连视频。')
  signal.throwIfAborted()
  const mediaSource = new Source()
  const url = URL.createObjectURL(mediaSource)
  const parser = new ISOFile<SourceBuffer>(undefined, false)
  let seekTo: number | undefined
  let reading = new AbortController()
  function seek(): void {
    if (bufferedUntil(video, video.currentTime) > video.currentTime) return
    seekTo = video.currentTime
    reading.abort()
    reading = new AbortController()
  }
  function release(): void {
    URL.revokeObjectURL(url)
    parser.stop()
  }
  signal.addEventListener('abort', release, { once: true })
  try {
    video.disableRemotePlayback = true
    await waitForEvent(mediaSource, 'sourceopen', signal, () => { video.src = url })
    let movie: Movie | undefined
    let bytesRead = 0
    parser.onReady = (info) => { movie = info }
    parser.onError = () => { throw new Error('读取 MP4 索引失败。') }
    async function appendAt(offset: number, readSignal: AbortSignal): Promise<number> {
      const data = await source.read(offset, source.size, readSignal)
      readSignal.throwIfAborted()
      if (!data.length) throw new Error('Telegram 返回的 MP4 数据不完整。')
      bytesRead += data.length
      const end = offset + data.length
      const next = parser.appendBuffer(MP4BoxBuffer.fromArrayBuffer(data.buffer, offset), end === source.size)
      return next > offset ? next : end
    }

    let offset = 0
    while (!movie && offset < source.size && bytesRead < 32 * 1024 * 1024) {
      offset = await appendAt(offset, signal)
    }
    if (!movie || !movie.videoTracks.length || !movie.timescale || !movie.duration || movie.isFragmented) {
      throw new Error('此 WebView 的直连播放需要带完整索引的普通 MP4 视频。')
    }
    const tracks = [movie.videoTracks[0], ...movie.audioTracks.slice(0, 1)]
    const duration = movie.duration / movie.timescale
    const samples = Math.max(1, Math.min(60, Math.round(tracks[0].nb_samples / duration)))
    const segments: { buffer: SourceBuffer; data: ArrayBuffer }[] = []
    parser.onSegment = (id, buffer, data, nextSample) => {
      segments.push({ buffer, data })
      parser.releaseUsedSamples(id, nextSample)
    }
    for (const track of tracks) {
      const mime = `${track.audio ? 'audio' : 'video'}/mp4; codecs="${track.codec}"`
      if (!Source.isTypeSupported(mime)) throw new Error('当前 WebView 无法解码这个 MP4 视频。')
      const buffer = mediaSource.addSourceBuffer(mime)
      parser.setSegmentOptions(track.id, buffer, { nbSamples: samples, rapAlignement: false })
    }
    const initialization = parser.initializeSegmentation('per-track')
    parser.discardMdatData = true
    for (const { user, buffer } of initialization) {
      await waitForEvent(user, 'updateend', signal, () => user.appendBuffer(buffer))
    }
    mediaSource.duration = duration
    video.addEventListener('seeking', seek, { signal })
    seekTo = 0

    while (!signal.aborted) {
      const readSignal = AbortSignal.any([signal, reading.signal])
      if (seekTo !== undefined) {
        offset = parser.seek(seekTo, true).offset
        seekTo = undefined
        parser.start()
      }
      try {
        for (const { buffer, data } of segments.splice(0)) {
          await waitForEvent(buffer, 'updateend', signal, () => buffer.appendBuffer(data))
        }
        if (readSignal.aborted) continue
        if (offset >= source.size) {
          if (mediaSource.readyState === 'open') mediaSource.endOfStream()
          await waitForEvent(readSignal, 'abort', signal)
        } else if (bufferedUntil(video, video.currentTime) - video.currentTime > 16) {
          await waitForEvent(video, 'timeupdate', readSignal)
        } else {
          const keepFrom = Math.max(0, video.currentTime - 30)
          for (const { user } of initialization) {
            if (user.buffered.length && user.buffered.start(0) < keepFrom - 10) {
              await waitForEvent(user, 'updateend', signal, () => user.remove(0, keepFrom))
            }
          }
          offset = await appendAt(offset, readSignal)
        }
      } catch (error) {
        if (!readSignal.aborted) throw error
      }
    }
  } finally {
    signal.removeEventListener('abort', release)
    release()
  }
}
