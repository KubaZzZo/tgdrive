import type {
  DriveItem,
  ItemListResponse,
  ItemQuery,
  Meta,
  ShareRecord,
  SyncStatus,
  UploadRecord,
  TrafficStatus,
  TelegramAccountStatus, TelegramLogin, TelegramChannel,
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
    if (response.status === 409 && (payload as { workspace_changed?: boolean })?.workspace_changed) window.location.reload()
    if (response.status === 401 && !path.endsWith('/login')) {
      window.dispatchEvent(new CustomEvent('tgdrive:unauthorized'))
    }
    const requestId = response.headers.get('X-Request-ID')
    throw new ApiError(
      errorMessage(payload, `请求失败 (${response.status})`),
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
  | { kind: 'error'; status: number; message: string; retryable: boolean }

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
    if (response.status === 409 && payload?.workspace_changed) window.location.reload()
    if (response.ok) {
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
      retryable: response.status >= 500 || response.status === 429,
    }
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error
    throw new ApiError('网络连接中断', 0)
  }
}

export const api = {
  thumbnailUrl: (itemId: string) => `/api/files/${encodeURIComponent(itemId)}/thumbnail${workspaceId ? '?workspace_id=' + encodeURIComponent(workspaceId) : ''}`,
  telegramAccounts: () => request<TelegramAccountStatus>('/api/telegram'),
  telegramLogin: () => request<TelegramLogin>('/api/telegram/login', { method: 'POST' }),
  telegramLoginStatus: (id: string) => request<TelegramLogin>(`/api/telegram/login/${encodeURIComponent(id)}`),
  telegramCheckScan: (id: string) => request<TelegramLogin>(`/api/telegram/login/${encodeURIComponent(id)}/check`, { method: 'POST' }),
  telegramCancelLogin: (id: string) => request<{ ok: boolean }>(`/api/telegram/login/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  telegramPassword: (id: string, password: string) => jsonRequest<TelegramLogin>('POST', `/api/telegram/login/${encodeURIComponent(id)}/password`, { password }),
  telegramChannels: (id: string) => request<{ channels: TelegramChannel[] }>(`/api/telegram/login/${encodeURIComponent(id)}/channels`),
  telegramActivate: (id: string, chatId: string) => request<TelegramAccountStatus>(`/api/telegram/login/${encodeURIComponent(id)}/activate`, { method: 'POST', body: JSON.stringify({ chat_id: chatId }), signal: AbortSignal.timeout(180_000) }),
  telegramSwitch: (id: string) => request<TelegramAccountStatus>(`/api/telegram/profiles/${encodeURIComponent(id)}/activate`, { method: 'POST', signal: AbortSignal.timeout(180_000) }),
  traffic: () => request<TrafficStatus>('/api/traffic'),
  saveTrafficQuota: (quotaBytes: number) => jsonRequest<TrafficStatus>('PUT', '/api/traffic/settings', { quota_bytes: quotaBytes }),
  syncStatus: () => request<SyncStatus>('/api/sync'),
  requestSync: (full = false) => jsonRequest<SyncStatus>('POST', '/api/sync', { full }),
  meta: () => request<Meta>('/api/meta'),
  login: (password: string) => jsonRequest<{ ok: boolean }>('POST', '/api/login', { password }),
  logout: () => request<{ ok: boolean }>('/api/logout', { method: 'POST' }),

  listItems: (folderId: string | null, options: ItemQuery = {}, signal?: AbortSignal) => {
    const query = new URLSearchParams()
    if (folderId) query.set('parent_id', folderId)
    Object.entries(options).forEach(([key, value]) => { if (value !== undefined && value !== '') query.set(key, String(value)) })
    return request<ItemListResponse>(`/api/items?${query}`, { signal })
  },
  createFolder: (name: string, parentId: string | null) =>
    jsonRequest<DriveItem>('POST', '/api/folders', { name, parent_id: parentId }),
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

  createUpload: (body: {
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
    request<UploadRecord>(`/api/uploads/${encodeURIComponent(uploadId)}`),
  listUploads: () => request<{ uploads: UploadRecord[] }>('/api/uploads'),
  completeUpload: (uploadId: string) =>
    request<{ ok?: boolean; upload?: UploadRecord }>(
      `/api/uploads/${encodeURIComponent(uploadId)}/complete`,
      { method: 'POST' },
    ),
  cancelUpload: (uploadId: string) =>
    request<{ ok: boolean }>(`/api/uploads/${encodeURIComponent(uploadId)}`, { method: 'DELETE' }),

  downloadUrl: (itemId: string, download = false) => {
    const query = new URLSearchParams()
    if (download) query.set('download', '1')
    if (workspaceId) query.set('workspace_id', workspaceId)
    return `/api/files/${encodeURIComponent(itemId)}/download${query.size ? '?' + query : ''}`
  },
}
