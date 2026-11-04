export type ItemKind = 'file' | 'folder'
export type UploadState = 'uploading' | 'processing' | 'done' | 'error'

export interface DriveItem {
  id: string
  parent_id: string | null
  kind: ItemKind
  name: string
  size: number
  mime: string | null
  sha256: string | null
  tg_chat_id: string | null
  created_at: string
  updated_at: string
  download_url?: string | null
}

export interface PathItem {
  id: string
  name: string
}

export interface Stats {
  bytes: number
  files: number
  folders: number
}

export interface StorageStatus {
  configured: boolean
  connected: boolean
  mode: 'user' | 'bot' | string
  error: string | null
  chat_id: string | null
  segment_size: number
}

export interface Meta {
  app_name: string
  authenticated: boolean
  storage: StorageStatus
  chunk_size: number
  max_file_size: number
}

export interface ItemListResponse {
  items: DriveItem[]
  path: PathItem[]
  stats: Stats
}

export interface UploadRecord {
  id: string
  parent_id: string | null
  name: string
  size: number
  mime: string | null
  received: number
  chunk_size: number
  state: UploadState
  progress: number
  item_id: string | null
  error: string | null
  created_at: string
  updated_at: string
}

export interface ShareRecord {
  token: string
  url: string
  expires_at: string | null
}
