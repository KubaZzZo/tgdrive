import type { FileCategory, ItemQuery } from '@/api/types'

export type DriveView = 'all' | 'recent' | 'people' | FileCategory
export type SearchScope = 'folder' | 'all' | ''
export type DriveFilters = Pick<ItemQuery, 'created_from' | 'created_to' | 'min_size' | 'max_size' | 'extension'>

export interface DriveLocation {
  folder: string | null
  view: Exclude<DriveView, 'people'>
  query: string
  sort: string
  page: number
  layout: 'list' | 'grid'
  scope: SearchScope
  filters: DriveFilters
}

const views = ['all', 'recent', 'image', 'video', 'audio', 'document', 'archive', 'code', 'application', 'other']
const sorts = ['newest', 'oldest', 'name', 'size']

function integer(value: string | null, minimum: number): number | undefined {
  if (!value || !/^\d+$/.test(value)) return undefined
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed >= minimum ? parsed : undefined
}

function date(value: string | null): string | undefined {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value) || value.startsWith('0000')) return undefined
  const parsed = new Date(value)
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().startsWith(value) ? value : undefined
}

export function readDriveLocation(hash: string): DriveLocation {
  const params = new URLSearchParams(hash.startsWith('#drive?') ? hash.slice(7) : '')
  const folder = params.get('folder')
  const view = params.get('view') ?? ''
  const sort = params.get('sort') ?? ''
  const scope = params.get('scope')
  const extension = params.get('extension')
  const from = date(params.get('created_from'))
  const to = date(params.get('created_to'))
  const min = integer(params.get('min_size'), 0)
  const max = integer(params.get('max_size'), 0)
  return {
    folder: folder && /^[\w-]{1,64}$/.test(folder) ? folder : null,
    view: views.includes(view) ? view as DriveLocation['view'] : 'all',
    query: (params.get('q') ?? '').slice(0, 255),
    sort: sorts.includes(sort) ? sort : 'newest',
    page: Math.min(integer(params.get('page'), 1) ?? 1, Math.floor(Number.MAX_SAFE_INTEGER / 200)),
    layout: params.get('layout') === 'grid' ? 'grid' : 'list',
    scope: scope === 'folder' || scope === 'all' ? scope : '',
    filters: {
      created_from: from,
      created_to: from && to && from > to ? undefined : to,
      min_size: min,
      max_size: min !== undefined && max !== undefined && min > max ? undefined : max,
      extension: extension && /^\.?[a-zA-Z0-9][a-zA-Z0-9._+-]{0,31}$/.test(extension) ? extension : undefined,
    },
  }
}

export function writeDriveLocation(state: DriveLocation): string {
  const params = new URLSearchParams()
  if (state.folder) params.set('folder', state.folder)
  if (state.view !== 'all') params.set('view', state.view)
  if (state.query) params.set('q', state.query)
  if (state.sort !== 'newest') params.set('sort', state.sort)
  if (state.page > 1) params.set('page', String(state.page))
  if (state.layout !== 'list') params.set('layout', state.layout)
  if (state.scope) params.set('scope', state.scope)
  for (const [key, value] of Object.entries(state.filters)) {
    if (value !== undefined) params.set(key, String(value))
  }
  return params.size ? `#drive?${params}` : '#drive'
}
