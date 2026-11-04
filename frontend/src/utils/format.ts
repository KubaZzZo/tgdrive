import type { DriveItem } from '@/api/types'

export function formatBytes(bytes: number | string | null | undefined): string {
  const value = Number(bytes ?? 0)
  if (!Number.isFinite(value) || value <= 0) return '0 B'
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  let size = value
  let index = 0
  while (size >= 1024 && index < units.length - 1) {
    size /= 1024
    index += 1
  }
  return `${size.toFixed(index === 0 || size >= 10 ? 0 : 1)} ${units[index]}`
}

export function formatDate(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return date.toLocaleString('zh-CN', { hour12: false })
}

export function iconFor(item: DriveItem): string {
  if (item.kind === 'folder') return '📁'
  const mime = item.mime ?? ''
  const name = item.name.toLowerCase()
  if (mime.startsWith('video/') || /\.(mp4|webm|mov|mkv|avi)$/.test(name)) return '🎬'
  if (mime.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|svg)$/.test(name)) return '🖼️'
  if (mime.startsWith('audio/') || /\.(mp3|wav|flac|m4a|ogg)$/.test(name)) return '🎵'
  if (mime === 'application/pdf' || name.endsWith('.pdf')) return '📕'
  if (/\.(zip|rar|7z|tar|gz)$/.test(name)) return '🗜️'
  if (/\.(docx?|xlsx?|pptx?)$/.test(name)) return '📄'
  return '📄'
}

export type PreviewKind = 'video' | 'image' | 'audio' | 'none'

export function previewKind(item: DriveItem): PreviewKind {
  const mime = item.mime ?? ''
  if (mime.startsWith('video/')) return 'video'
  if (mime.startsWith('image/')) return 'image'
  if (mime.startsWith('audio/')) return 'audio'
  return 'none'
}

export function formatEta(seconds: number | null): string {
  if (!seconds || !Number.isFinite(seconds) || seconds <= 0) return ''
  if (seconds < 60) return `${Math.ceil(seconds)} 秒`
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes} 分 ${Math.ceil(seconds % 60)} 秒`
  return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`
}
