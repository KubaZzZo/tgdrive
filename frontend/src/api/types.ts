export type FileAction = 'download' | 'share' | 'rename' | 'remove' | 'people'
export interface TelegramIdentity { id: number; username: string | null; first_name: string | null; bot: boolean }
export interface TelegramProfile { id: string; chat_id: string; user_id: number; username: string | null; first_name: string | null; title: string }
export interface TelegramAccountStatus {
  active_id: string | null; account: TelegramIdentity | null; chat_id: string; connected: boolean
  can_login: boolean; login_error: string | null; switching: boolean; profiles: TelegramProfile[]
}
export interface TelegramLogin {
  id: string; state: 'connecting' | 'waiting' | 'checking' | 'password' | 'ready' | 'error' | 'expired' | 'canceled'
  qr_image: string | null; error: string | null; account: TelegramIdentity | null
}
export interface TelegramChannel { id: string; title: string; writable: boolean }
export interface TrafficTotals { inbound: number; outbound: number }
export interface CDNStatus {
  configured: boolean; available: boolean; stale: boolean; blocked: boolean; warning: boolean
  error: string | null; updated_at: string | null; warning_percent: number; stop_percent: number
  subscriptions: {
    id: string; name: string; period: string; quota_bytes: number | null
    provider_used: number; used: number; remaining: number | null; percent: number | null
    usage_updated_at: string; stale: boolean; warning: boolean; blocked: boolean
  }[]
}
export interface TrafficStatus {
  cdn?: CDNStatus | null
  day: string
  timezone: string
  quota_bytes: number
  started_at: string
  sources: { web: TrafficTotals; telegram: TrafficTotals }
  host: {
    available: boolean; stale: boolean; error: string | null
    updated_at?: string; started_at?: string; interfaces?: string[]
    inbound?: number; outbound?: number; used?: number; remaining?: number; percent?: number
    warning?: 'near' | 'exceeded' | null; incomplete?: boolean
    history?: (TrafficTotals & { day: string; incomplete?: boolean })[]
  }
}
export type FileCategory =
  | 'image'
  | 'video'
  | 'audio'
  | 'document'
  | 'archive'
  | 'code'
  | 'application'
  | 'other'
export interface CategoryStats {
  id: FileCategory
  label: string
  count: number
  bytes: number
}
export interface ItemQuery {
  scope?: 'folder' | 'all' | 'recent'
  category?: FileCategory
  q?: string
  person?: string
  sort?: 'name' | 'size' | 'created_at' | 'updated_at'
  direction?: 'asc' | 'desc'
  limit?: number
  offset?: number
}

export interface PersonLabel {
  key: string
  name: string
  display_name: string
}

export interface VideoPerson extends PersonLabel {
  count: number
}

export type ItemKind = 'file' | 'folder'
export type UploadState = 'uploading' | 'processing' | 'done' | 'error'

export interface DriveItem {
  id: string
  parent_id: string | null
  source?: 'web' | 'channel'
  kind: ItemKind
  category: FileCategory | null
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
  workspace_is_default?: boolean
  app_name: string
  authenticated: boolean
  storage: StorageStatus
  chunk_size: number
  max_file_size: number
}

export interface ItemListResponse {
  person_display_name?: string
  categories: CategoryStats[]
  total: number
  limit: number
  offset: number
  items: DriveItem[]
  path: PathItem[]
  stats: Stats
}

export interface UploadRecord {
  image_host?: number
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

export interface HostedImage extends DriveItem {
  token: string
  url: string
  published_at: string
  hosted_mime: string
}

export interface ShareRecord {
  token: string
  url: string
  expires_at: string | null
}

export interface SyncStatus {
  enabled: boolean
  state: 'disabled' | 'unavailable' | 'idle' | 'syncing' | 'rate_limited' | 'error' | 'auth_required' | 'stopped'
  running: boolean
  scanned: number
  progress: number | null
  retry_after: number | null
  bot_state: 'connecting' | 'listening' | 'error' | null
  bot_error: string | null
  imported: number
  files: number
  revision: number
  last_success: string | null
  error: string | null
  interval: number
}
