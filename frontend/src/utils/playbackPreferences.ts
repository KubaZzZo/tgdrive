export type PlaybackMode = 'server' | 'telegram'
export interface PlaybackState {
  // An unopened source still needs to restore its saved position after metadata arrives.
  time?: number
  paused: boolean
  rate: number
  volume: number
  muted: boolean
}

const key = 'tgdrive_playback_preferences_v1'
interface Preferences {
  defaultMode: PlaybackMode
  videos: Record<string, PlaybackMode>
}
const isMode = (value: unknown): value is PlaybackMode => value === 'server' || value === 'telegram'

function read(): Preferences {
  try {
    const value = JSON.parse(localStorage.getItem(key) ?? '{}')
    const videos = Object.fromEntries(Object.entries(value?.videos ?? {})
      .filter((entry): entry is [string, PlaybackMode] => isMode(entry[1])))
    return { defaultMode: isMode(value?.defaultMode) ? value.defaultMode : 'server', videos }
  } catch {
    return { defaultMode: 'server', videos: {} }
  }
}

export function videoPlaybackKey(workspace: string | null, id: string): string {
  return JSON.stringify([workspace, id])
}

export function playbackPreference(videoKey: string): { preferred: PlaybackMode | null; defaultMode: PlaybackMode } {
  const value = read()
  return { preferred: value.videos[videoKey] ?? null, defaultMode: value.defaultMode }
}

export function savePlaybackPreference(videoKey: string, mode: PlaybackMode | null): void {
  const value = read()
  delete value.videos[videoKey]
  if (mode) value.videos[videoKey] = mode
  value.videos = Object.fromEntries(Object.entries(value.videos).slice(-300))
  try { localStorage.setItem(key, JSON.stringify(value)) } catch { /* Playback works without storage. */ }
}

export function saveDefaultPlaybackMode(mode: PlaybackMode): void {
  try { localStorage.setItem(key, JSON.stringify({ ...read(), defaultMode: mode })) }
  catch { /* Playback works without storage. */ }
}

export function resolvePlaybackMode(preferred: PlaybackMode | null, defaultMode: PlaybackMode, directAvailable: boolean): PlaybackMode {
  return (preferred ?? defaultMode) === 'telegram' && directAvailable ? 'telegram' : 'server'
}
