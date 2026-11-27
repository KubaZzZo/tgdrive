import type {
  DriveItem,
  GoogleDriveItem,
  ClientCredential,
  ClientScope,
  HostedImage,
  ItemListResponse,
  ItemQuery,
  Meta,
  ShareRecord,
  ManagedShare, TrashItem, BatchItemResult,
  SyncStatus,
  UploadRecord,
  TrafficStatus,
  TelegramAccountStatus, TelegramLogin, TelegramChannel,
  PersonLabel, VideoPerson,
} from './types'

export class ApiError extends Error {
  readonly status: number
  readonly requestId: string | null

  constructor(message: string, status: number, requestId: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.requestId = requestId
  }
}

type JsonBody = Record<string, unknown>
let workspaceId: string | null = null
export function setWorkspaceId(value: string | null): void { workspaceId = value }

export function taskEvents<T>(
  path: string, onSnapshot: (snapshot: T) => void, onDisconnect: () => void,
): (() => void) | null {
  if (typeof EventSource === 'undefined') return null
  const query = workspaceId ? `?workspace_id=${encodeURIComponent(workspaceId)}` : ''
  const source = new EventSource(path + query)
  const close = () => {
    source.onmessage = null
    source.onerror = null
    source.close()
  }
  source.onmessage = (event) => {
    let snapshot: T
    try { snapshot = JSON.parse(event.data) as T }
    catch { close(); onDisconnect(); return }
    onSnapshot(snapshot)
  }
  source.onerror = () => {
    close()
    onDisconnect()
  }
  return close
}

export function reloadAfterWorkspaceChange(): void {
  if ((window.location.pathname === '/drive' && (window.location.search || window.location.hash))
    || window.location.hash.startsWith('#drive?')) {
    window.location.replace('/drive')
    return
  }
  window.location.reload()
}

async function parsePayload(response: Response): Promise<unknown> {
  const contentType = response.headers.get('content-type') ?? ''
  if (contentType.includes('application/json')) {
    return response.json().catch(() => null)
  }
  return response.text().catch(() => '')
}

function errorMessage(payload: unknown, fallback: string): string {
  if (payload && typeof payload === 'object') {
    const record = payload as Record<string, unknown>
    const detail = record.detail ?? record.error
    if (typeof detail === 'string') return detail
    if (Array.isArray(detail)) return detail.map(String).join('; ')
  }
  return fallback
}

export async function mediaFailure(url: string, fallback: string, signal: AbortSignal): Promise<string> {
  try {
    const options = { signal, credentials: 'same-origin' as const, cache: 'no-store' as const }
    const response = await fetch(url, { ...options, headers: { Range: 'bytes=0-0' } })
    if (response.ok) {
      void response.body?.cancel().catch(() => {})
      return fallback
    }
    return errorMessage(await parsePayload(response), `媒体读取失败 (${response.status})`)
  } catch {
    return '无法连接媒体服务，请检查网络后重试。'
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  headers.set('Accept', 'application/json')
  if (workspaceId) headers.set('X-Workspace-ID', workspaceId)
  if (init.body && typeof init.body === 'string') headers.set('Content-Type', 'application/json')

  let response: Response
  try {
    response = await fetch(path, {
      credentials: 'same-origin',
      ...init,
      signal: init.signal ?? AbortSignal.timeout(30_000),
      headers,
    })
  } catch {
    throw new ApiError('网络连接中断', 0)
  }
  const payload = await parsePayload(response)
  if (!response.ok) {
    if (response.status === 409 && (payload as { workspace_changed?: boolean })?.workspace_changed) reloadAfterWorkspaceChange()
    if (response.status === 401 && !path.endsWith('/login')) {
      window.dispatchEvent(new CustomEvent('tgdrive:unauthorized'))
    }
    const requestId = response.headers.get('X-Request-ID')
    throw new ApiError(
      errorMessage(payload, response.status === 429 && path.includes('/download')
        ? 'CDN 流量保护已开启，请查看流量面板的余量和数据状态' : `请求失败 (${response.status})`),
      response.status,
      requestId,
    )
  }
  return payload as T
}

function jsonRequest<T>(method: string, path: string, body: JsonBody): Promise<T> {
  return request<T>(path, { method, body: JSON.stringify(body) })
}

export interface ChunkResponse {
  offset: number
  received: number
  size: number
  duplicate?: boolean
}

export type ChunkResult =
  | { kind: 'ok'; data: ChunkResponse }
  | { kind: 'conflict'; offset: number }
  | { kind: 'error'; status: number; message: string }

export async function uploadChunk(
  uploadId: string,
  offset: number,
  blob: Blob,
  signal?: AbortSignal,
): Promise<ChunkResult> {
  try {
    const response = await fetch(`/api/uploads/${encodeURIComponent(uploadId)}`, {
      method: 'PATCH',
      credentials: 'same-origin',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/octet-stream',
        'Upload-Offset': String(offset),
        ...(workspaceId ? { 'X-Workspace-ID': workspaceId } : {}),
      },
      body: blob,
      signal,
    })
    const payload = (await parsePayload(response)) as Record<string, unknown> | null
    if (response.status === 409 && payload?.workspace_changed) reloadAfterWorkspaceChange()
    if (response.ok) {
      // The server may have committed the chunk before the response body was lost.
      if (!Number.isSafeInteger(payload?.offset)) throw new ApiError('未收到服务器确认的上传进度', 0)
      return { kind: 'ok', data: payload as unknown as ChunkResponse }
    }
    if (response.status === 401) {
      window.dispatchEvent(new CustomEvent('tgdrive:unauthorized'))
    }
    if (response.status === 409 && typeof payload?.offset === 'number' && Number.isSafeInteger(payload.offset)) {
      return { kind: 'conflict', offset: payload.offset }
    }
    return {
      kind: 'error',
      status: response.status,
      message: errorMessage(payload, `上传失败 (${response.status})`),
    }
  } catch (error) {
    if (error instanceof ApiError || (error instanceof DOMException && error.name === 'AbortError')) throw error
    throw new ApiError('网络连接中断', 0)
  }
}

export interface VideoImportStatus {
  batch_id: string | null
  active: boolean
  running: boolean
  jobs: {
    id: string
    state: string
    name?: string
    error?: string
    size?: number
    downloaded: number
    uploaded?: number
    ed2k_sources?: number
    ed2k_network?: {
      server?: 'offline' | 'connecting' | 'low_id' | 'high_id'
      kad?: 'offline' | 'firewalled' | 'open'
    }
    torrent_seeds?: number
  }[]
}

export interface TelegramDiagnostics {
  channel_id: string | null
  session: DiagnosticConnection & { mode: 'user' | 'bot' | null }
  bot: DiagnosticConnection
  sync: { state: string; error: string | null; last_success: string | null; bot_state: string | null; bot_error: string | null }
  test_channel_id: string | null
}

export interface IndexRestorePreview {
  channel_id: string
  backup: { app_version: string; source_schema_version: number; created_at?: string }
  preview_id: string
  added: Record<string, number>
  unchanged_items: number
  conflict_count: number
  conflicts: { name: string; reason: string }[]
  skipped_trash: number
  skipped_links: number
}

interface DiagnosticConnection {
  state: string
  can_post: boolean | null
  can_delete: boolean | null
  error?: string
}

export interface VideoImportPreviewRow {
  protocol: 'http' | 'ed2k' | 'magnet' | 'unknown'
  name?: string
  size?: number
  error?: string
}

export interface QBDownload {
  hash: string
  name: string
  state: string
  progress: number
  size: number
  downloaded: number
  ready: boolean
  importing: boolean
}

export const api = {
  restoreIndex: (file: File, includeLinks: boolean, previewId?: string) =>
    request<IndexRestorePreview>(
      `/api/clients/index-restore/${previewId ? 'confirm' : 'preview'}?include_links=${includeLinks}`,
      {
        method: 'POST',
        body: file,
        headers: { 'Content-Type': 'application/zip', ...(previewId ? { 'X-Restore-Preview': previewId } : {}) },
        signal: AbortSignal.timeout(120_000),
      },
    ),
  telegramDiagnostics: () => request<TelegramDiagnostics>('/api/telegram/diagnostics'),
  testTelegramMessage: (channelId: string) => jsonRequest<{ channel_id: string }>(
    'POST', '/api/telegram/diagnostics/test-message', { chat_id: channelId },
  ),
  clientCredentials: () => request<{ channel_id: string; credentials: ClientCredential[] }>('/api/clients/credentials'),
  addClientCredential: (scope: ClientScope, label: string) =>
    jsonRequest<ClientCredential & { secret: string }>('POST', '/api/clients/credentials', { scope, label }),
  revokeClientCredential: (id: string) => request<{ ok: boolean }>(
    `/api/clients/credentials/${encodeURIComponent(id)}`, { method: 'DELETE' },
  ),
  videoImport: () => request<VideoImportStatus>('/api/video-import'),
  qbDownloads: () => request<{ enabled: boolean; tasks: QBDownload[] }>('/api/video-import/downloads'),
  importQBDownload: (hash: string) => request<VideoImportStatus>(
    `/api/video-import/downloads/${encodeURIComponent(hash)}/import`, { method: 'POST' },
  ),
  deleteQBDownload: (hash: string) => request<void>(
    `/api/video-import/downloads/${encodeURIComponent(hash)}`, { method: 'DELETE' },
  ),
  previewVideoImport: (links: string[]) => request<{ rows: VideoImportPreviewRow[] }>('/api/video-import/preview', {
    method: 'POST', body: JSON.stringify({ links }),
  }),
  startVideoImport: (links: string[]) => request<VideoImportStatus>('/api/video-import', {
    method: 'POST', body: JSON.stringify({ links }),
  }),
  retryVideoImport: () => request<VideoImportStatus>('/api/video-import/retry', { method: 'POST' }),
  cancelVideoImport: () => request<VideoImportStatus>('/api/video-import/cancel', { method: 'POST' }),

  listImages: (q = '', offset = 0) => request<{ images: HostedImage[]; total: number }>(
    `/api/images?${new URLSearchParams({ q, offset: String(offset), limit: '24' })}`),
  publishImage: (id: string) => request<{ url: string }>(`/api/images/${encodeURIComponent(id)}`, { method: 'POST' }),
  unpublishImage: (id: string) => request<{ ok: boolean }>(`/api/images/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  thumbnailUrl: (itemId: string, version: string) => {
    const query = new URLSearchParams({ v: version })
    if (workspaceId) query.set('workspace_id', workspaceId)
    return `/api/files/${encodeURIComponent(itemId)}/thumbnail?${query}`
  },
  telegramAccounts: () => request<TelegramAccountStatus>('/api/telegram'),
  telegramLogin: () => request<TelegramLogin>('/api/telegram/login', { method: 'POST' }),
  telegramLoginStatus: (id: string) => request<TelegramLogin>(`/api/telegram/login/${encodeURIComponent(id)}`),
  telegramCheckScan: (id: string) => request<TelegramLogin>(`/api/telegram/login/${encodeURIComponent(id)}/check`, { method: 'POST' }),
  telegramCancelLogin: (id: string) => request<{ ok: boolean }>(`/api/telegram/login/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  telegramPassword: (id: string, password: string) => jsonRequest<TelegramLogin>('POST', `/api/telegram/login/${encodeURIComponent(id)}/password`, { password }),
  telegramChannels: (id: string) => request<{ channels: TelegramChannel[] }>(`/api/telegram/login/${encodeURIComponent(id)}/channels`),
  telegramActivate: (id: string, chatId: string) => request<TelegramAccountStatus>(`/api/telegram/login/${encodeURIComponent(id)}/activate`, { method: 'POST', body: JSON.stringify({ chat_id: chatId }), signal: AbortSignal.timeout(180_000) }),
  telegramSwitch: (id: string) => request<TelegramAccountStatus>(`/api/telegram/profiles/${encodeURIComponent(id)}/activate`, { method: 'POST', signal: AbortSignal.timeout(180_000) }),
  telegramDeleteProfile: (id: string) => request<TelegramAccountStatus>(`/api/telegram/profiles/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  traffic: () => request<TrafficStatus>('/api/traffic'),
  playbackTransfer: (itemId: string, sessionId: string, signal: AbortSignal) => request<{ bytes: number | null; sampled_at: number }>(
    `/api/files/${encodeURIComponent(itemId)}/playback/${sessionId}`, { signal, cache: 'no-store' },
  ),
  saveTrafficQuota: (quotaBytes: number) => jsonRequest<TrafficStatus>('PUT', '/api/traffic/settings', { quota_bytes: quotaBytes }),
  syncStatus: () => request<SyncStatus>('/api/sync'),
  requestSync: (full = false) => jsonRequest<SyncStatus>('POST', '/api/sync', { full }),
  meta: () => request<Meta>('/api/meta'),
  telegramDirectCredentials: (signal: AbortSignal) => request<{ api_id: number; api_hash: string }>(
    '/api/telegram-direct/credentials', { method: 'POST', cache: 'no-store', signal },
  ),
  login: (password: string) => jsonRequest<{ ok: boolean }>('POST', '/api/login', { password }),
  logout: () => request<{ ok: boolean }>('/api/logout', { method: 'POST' }),

  googleDriveItems: (path: string, signal: AbortSignal) => request<{ items: GoogleDriveItem[] }>(
    `/api/google-drive/items?${new URLSearchParams({ path })}`, { signal },
  ),
  googleDriveContentUrl: (path: string, download = false) => {
    const query = new URLSearchParams({ path })
    if (download) query.set('download', '1')
    return `/api/google-drive/content?${query}`
  },

  listItems: (folderId: string | null, options: ItemQuery = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (folderId) query.set('parent_id', folderId)
    Object.entries(options).forEach(([key, value]) => { if (value !== undefined && value !== '') query.set(key, String(value)) })
    return request<ItemListResponse>(`/api/items?${query}`, { signal })
  },
  listPeople: (q: string, limit: number, offset: number, signal: AbortSignal) => {
    const query = new URLSearchParams({ q, limit: String(limit), offset: String(offset) })
    return request<{ people: VideoPerson[]; total: number }>(`/api/people?${query}`, { signal })
  },
  createPeopleLabel: (name: string) => jsonRequest<{ ok: boolean }>('POST', '/api/people', { name }),
  deletePeopleLabel: (name: string) => request<{ ok: boolean }>(
    `/api/people?${new URLSearchParams({ name })}`, { method: 'DELETE' },
  ),
  videoPeople: (itemId: string, signal: AbortSignal) => request<{ people: PersonLabel[] }>(
    `/api/items/${encodeURIComponent(itemId)}/people`, { signal },
  ),
  editVideoPeople: (itemId: string, add: string[], remove: string[]) => jsonRequest<{ ok: boolean }>(
    'PATCH', `/api/items/${encodeURIComponent(itemId)}/people`, { add, remove },
  ),
  createFolder: (name: string, parentId: string | null) =>
    jsonRequest<DriveItem>('POST', '/api/folders', { name, parent_id: parentId }),
  createFolderPaths: (parentId: string | null, paths: string[][]) =>
    jsonRequest<{ ids: string[] }>('POST', '/api/folders/paths', { parent_id: parentId, paths }),
  updateItem: (itemId: string, body: { name?: string; parent_id?: string | null; move?: boolean }) =>
    jsonRequest<DriveItem>('PATCH', `/api/items/${encodeURIComponent(itemId)}`, body),
  deleteItem: (itemId: string) =>
    request<{ ok: boolean; deleted: number; warning: string | null }>(
      `/api/items/${encodeURIComponent(itemId)}`,
      { method: 'DELETE' },
    ),
  createShare: (itemId: string, expiresHours = 0) =>
    jsonRequest<ShareRecord>('POST', `/api/items/${encodeURIComponent(itemId)}/share`, {
      expires_hours: expiresHours,
    }),
  listShares: (offset: number) => request<{ shares: ManagedShare[]; total: number }>(
    `/api/shares?offset=${offset}`,
  ),
  updateShare: (token: string, expiresHours: number) =>
    jsonRequest<{ expires_at: string | null }>('PATCH', '/api/shares', {
      token, expires_hours: expiresHours,
    }),
  revokeShare: (token: string) => jsonRequest<{ ok: boolean }>('DELETE', '/api/shares', { token }),
  listTrash: (offset: number) => request<{ items: TrashItem[]; total: number; retention_days: number }>(
    `/api/trash?offset=${offset}`,
  ),
  restoreItem: (id: string) => request<DriveItem>(
    `/api/trash/${encodeURIComponent(id)}/restore`, { method: 'POST' },
  ),
  purgeItem: (id: string) => request<{ ok: boolean; warning: string | null }>(
    `/api/trash/${encodeURIComponent(id)}`, { method: 'DELETE' },
  ),
  emptyTrash: () => request<{ ok: boolean; deleted: number }>('/api/trash', { method: 'DELETE' }),
  batchItems: (ids: string[], action: 'move' | 'trash', parentId: string | null = null) =>
    jsonRequest<{ results: BatchItemResult[] }>('POST', '/api/items/batch', {
      ids, action, parent_id: parentId,
    }),

  createUpload: (body: {
    image_host?: boolean
    name: string
    size: number
    mime: string
    parent_id: string | null
  }) =>
    request<{ id: string; offset: number; chunk_size: number; size: number }>('/api/uploads', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getUpload: (uploadId: string) =>
    request<UploadRecord>(`/api/uploads/${encodeURIComponent(uploadId)}`, { cache: 'no-store' }),
  listUploads: () => request<{ uploads: UploadRecord[] }>('/api/uploads'),
  completeUpload: (uploadId: string) =>
    request<{ ok?: boolean; upload?: UploadRecord } | UploadRecord>(
      `/api/uploads/${encodeURIComponent(uploadId)}/complete`,
      { method: 'POST' },
    ),
  cancelUpload: (uploadId: string) =>
    request<{ ok: boolean }>(`/api/uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' }),

  checkDownload: (itemId: string) =>
    request<void>(`/api/files/${encodeURIComponent(itemId)}/download/check`, { cache: 'no-store' }),

  downloadUrl: (itemId: string, download = false) => {
    const query = new URLSearchParams()
    if (download) query.set('download', '1')
    if (workspaceId) query.set('workspace_id', workspaceId)
    return `/api/files/${encodeURIComponent(itemId)}/download${query.size ? '?' + query : ''}`
  },
}
