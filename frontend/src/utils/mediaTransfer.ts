const KiB = 1024
const MiB = 1024 * KiB

export class MediaTransfer {
  bytesPerSecond = 0
  latencyMs = 0
  private readonly memoryMiB: number

  constructor(memoryMiB = 48) {
    this.memoryMiB = memoryMiB
  }

  sample(bytes: number, milliseconds: number, latencyMs = 0): void {
    this.latencyMs = this.latencyMs ? (this.latencyMs + latencyMs) / 2 : latencyMs
    // Tiny reads and cache hits are too short to estimate network throughput.
    if (bytes < 128 * KiB || milliseconds < 12) return
    const rate = bytes * 1000 / milliseconds
    const weight = rate < this.bytesPerSecond ? 0.65 : 0.25
    this.bytesPerSecond = this.bytesPerSecond ? this.bytesPerSecond * (1 - weight) + rate * weight : rate
  }

  playbackBytes(bitrateBytes: number, ahead: number): number {
    if (ahead < 3) return 512 * KiB
    const byMedia = bitrateBytes > 0 ? bitrateBytes * 4 : 2 * MiB
    const byNetwork = this.bytesPerSecond ? this.bytesPerSecond * (1.5 + Math.min(1, this.latencyMs / 1000)) : byMedia
    const aligned = Math.ceil(Math.min(byMedia, byNetwork) / (64 * KiB)) * (64 * KiB)
    return Math.min(4 * MiB, Math.max(512 * KiB, aligned))
  }

  bufferSeconds(bitrateBytes: number, paused: boolean, hidden: boolean, selected?: number, playbackRate = 1): number {
    const sustainable = !this.bytesPerSecond || this.bytesPerSecond >= bitrateBytes * playbackRate
    const target = paused || hidden ? 8 : selected ?? (sustainable ? 30 : 16)
    const memoryWindow = bitrateBytes > 0 ? Math.max(3, this.memoryMiB * MiB / bitrateBytes) : target
    return Math.min(target, memoryWindow)
  }

  historySeconds(bitrateBytes: number): number {
    return bitrateBytes > 0 ? Math.max(2, Math.min(60, this.memoryMiB / 2 * MiB / bitrateBytes)) : 60
  }

  cacheConcurrency(foreground: boolean): number {
    if (foreground || this.bytesPerSecond < 2 * MiB) return 1
    return this.bytesPerSecond < 5 * MiB ? 2 : 3
  }
}

export function strongMediaTag(tag: string | null): tag is string {
  return tag !== null && /^"[^"]+"$/.test(tag)
}

export async function readMediaBytes(
  reader: ReadableStreamDefaultReader<Uint8Array>, length: number, onFirstByte?: () => void,
): Promise<Uint8Array<ArrayBuffer>> {
  const bytes = new Uint8Array(length)
  let offset = 0
  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break
      if (offset === 0 && value.length) onFirstByte?.()
      if (offset + value.length > length) throw new Error('Oversized media segment')
      bytes.set(value, offset)
      offset += value.length
    }
    if (offset !== length) throw new Error('Incomplete media segment')
    return bytes
  } finally { await reader.cancel().catch(() => {}) }
}

export async function withMediaTimeout<T>(
  parent: AbortSignal, milliseconds: number, operation: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  parent.throwIfAborted()
  const controller = new AbortController()
  const abort = () => controller.abort(parent.reason)
  parent.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(() => controller.abort(new DOMException('Media request timed out', 'TimeoutError')), milliseconds)
  try {
    const result = await operation(controller.signal)
    controller.signal.throwIfAborted()
    return result
  } catch (cause) {
    controller.signal.throwIfAborted()
    throw cause
  }
  finally {
    clearTimeout(timer)
    parent.removeEventListener('abort', abort)
  }
}
