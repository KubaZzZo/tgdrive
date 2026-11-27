export class TransferRate {
  private samples: { bytes: number; at: number }[] = []

  sample(bytes: number | null, at: number): number | null {
    const last = this.samples.at(-1)
    if (bytes === null || !Number.isFinite(bytes) || bytes < 0 || !Number.isFinite(at)) {
      this.samples = []
      return null
    }
    if (last && (bytes < last.bytes || at <= last.at || at - last.at > 5000)) this.samples = []
    this.samples.push({ bytes, at })
    // Keep a baseline at the start of the last three seconds, including polling jitter.
    while (this.samples.length > 2 && this.samples[1]!.at <= at - 3000) this.samples.shift()
    const first = this.samples[0]!
    return at > first.at ? (bytes - first.bytes) * 1000 / (at - first.at) : null
  }
}

export function bufferedSeconds(media: Pick<HTMLMediaElement, 'buffered' | 'currentTime'>): number {
  for (let i = 0; i < media.buffered.length; i++) {
    if (media.buffered.start(i) <= media.currentTime && media.currentTime <= media.buffered.end(i)) {
      return Math.max(0, media.buffered.end(i) - media.currentTime)
    }
  }
  return 0
}
