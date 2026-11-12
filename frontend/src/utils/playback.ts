const KEY = 'tgdrive_playback_v1'
const MODE_KEY = 'tgdrive_playback_mode'
export type PlaybackMode = 'auto' | 'compat' | 'native'
export type PlaybackPath = Exclude<PlaybackMode, 'auto'>

export function savedPlaybackMode(): PlaybackMode {
  try {
    const mode = localStorage.getItem(MODE_KEY)
    if (mode === 'native' || mode === 'compat') return mode
  } catch { /* Storage can be unavailable in private browsing. */ }
  return 'auto'
}

export function savePlaybackMode(mode: PlaybackMode): void {
  try { localStorage.setItem(MODE_KEY, mode) } catch { /* Playback works without storage. */ }
}

export function playbackPaths(mode: PlaybackMode, kind: 'audio' | 'video', mime: string): PlaybackPath[] {
  if (kind === 'audio' || mode === 'native' || /^video\/(?:webm|ogg|x-matroska|x-msvideo|mpeg)(?:;|$)/i.test(mime))
    return ['native']
  return mode === 'auto' && /^video\/(?:mp4|x-m4v)(?:;|$)/i.test(mime)
    ? ['native', 'compat'] : ['compat', 'native']
}
interface SavedProgress {
  time: number
  updated: number
}
type ProgressMap = Record<string, SavedProgress>
function read(): ProgressMap {
  try {
    const value = JSON.parse(localStorage.getItem(KEY) ?? '{}')
    return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
  } catch {
    return {}
  }
}
export function playbackPosition(id: string, duration: number): number {
  const time = Number(read()[id]?.time)
  return Number.isFinite(duration) && Number.isFinite(time) && time >= 2 && time < duration - 2
    ? time
    : 0
}
export function savePlayback(id: string, time: number, duration: number): void {
  if (!Number.isFinite(time) || !Number.isFinite(duration) || duration <= 0) return
  const data = read()
  if (time < 2 || time >= duration - 2) delete data[id]
  else data[id] = { time, updated: Date.now() }
  const entries = Object.entries(data)
    .filter(([, v]) => v && Number.isFinite(v.time) && Number.isFinite(v.updated))
    .sort((a, b) => b[1].updated - a[1].updated)
    .slice(0, 100)
  try {
    localStorage.setItem(KEY, JSON.stringify(Object.fromEntries(entries)))
  } catch {
    /* Playback still works without storage. */
  }
}
export function mediaTime(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00'
  const whole = Math.floor(seconds)
  const minutes = Math.floor(whole / 60)
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, '0')}:${String(whole % 60).padStart(2, '0')}`
    : `${minutes}:${String(whole % 60).padStart(2, '0')}`
}
