import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, ApiError } from '@/api/client'
import type {
  CategoryStats,
  DriveItem,
  FileCategory,
  ItemQuery,
  PathItem,
  Stats,
} from '@/api/types'
import { categories as defaultCategories } from '@/utils/categories'
import { readDriveLocation, writeDriveLocation } from '@/utils/driveLocation'
import type { DriveFilters, SearchScope } from '@/utils/driveLocation'
import { useToastStore } from './toast'

export const useDriveStore = defineStore('drive', () => {
  const folderId = ref<string | null>(null)
  const items = ref<DriveItem[]>([])
  const path = ref<PathItem[]>([])
  const stats = ref<Stats>({ bytes: 0, files: 0, folders: 0 })
  const loading = ref(false)
  const error = ref('')
  const categories = ref<CategoryStats[]>(defaultCategories.map((c) => ({ ...c })))
  const view = ref<'all' | 'recent' | 'people' | FileCategory>('all')
  const person = ref<string | null>(null)
  const personDisplayName = ref('')
  const query = ref('')
  const filters = ref<DriveFilters>({})
  const searchScope = ref<SearchScope>('')
  const sortOption = ref('newest')
  const page = ref(1)
  const total = ref(0)
  const pageSize = computed(() => ['all', 'recent', 'video', 'people'].includes(view.value) ? 10 : 60)
  const layout = ref<'list' | 'grid'>('list')
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize.value)))
  const title = computed(() => {
    if (folderId.value) return path.value.at(-1)?.name ?? '文件夹'
    if (view.value === 'people') return personDisplayName.value || person.value || '人物视频'
    if (view.value === 'recent') return '最近上传'
    return categories.value.find((c) => c.id === view.value)?.label ?? '我的文件'
  })
  let loadVersion = 0
  let controller: AbortController | null = null

  const scope = computed<ItemQuery['scope']>(() => {
    if (searchScope.value) return searchScope.value
    if (view.value === 'recent') return 'recent'
    if (view.value !== 'all' || (!folderId.value && (
      query.value.trim() || Object.values(filters.value).some((value) => value !== undefined)
    ))) return 'all'
    return 'folder'
  })

  function remember(replace = false): void {
    if (typeof window === 'undefined' || !window.history || !window.location) return
    const current = window.location.hash
    if ((current && current !== '#drive' && !current.startsWith('#drive?')) || view.value === 'people') return
    const hash = writeDriveLocation({
      folder: folderId.value, view: view.value, query: query.value, sort: sortOption.value,
      page: page.value, layout: layout.value, scope: searchScope.value, filters: filters.value,
    })
    if (current !== hash) {
      if (replace) window.history.replaceState(null, '', hash)
      else window.history.pushState(null, '', hash)
    }
  }

  async function restoreLocation(hash: string): Promise<void> {
    const route = readDriveLocation(hash)
    folderId.value = route.folder
    view.value = route.view
    query.value = route.query
    sortOption.value = route.sort
    page.value = route.page
    layout.value = route.layout
    searchScope.value = route.scope
    filters.value = route.filters
    person.value = null
    personDisplayName.value = ''
    path.value = []
    await load()
  }

  async function load(): Promise<void> {
    const version = ++loadVersion
    controller?.abort()
    controller = new AbortController()
    loading.value = true
    error.value = ''
    const sorting: Record<string, Pick<ItemQuery, 'sort' | 'direction'>> = {
      newest: { sort: 'created_at', direction: 'desc' },
      oldest: { sort: 'created_at', direction: 'asc' },
      name: { sort: 'name', direction: 'asc' },
      size: { sort: 'size', direction: 'desc' },
    }
    const peopleIndex = view.value === 'people' && !person.value
    const category = view.value === 'people'
      ? 'video'
      : view.value !== 'all' && view.value !== 'recent' ? view.value : undefined
    try {
      const data = await api.listItems(
        folderId.value,
        {
          scope: scope.value,
          category,
          person: person.value ?? undefined,
          q: query.value,
          ...filters.value,
          ...sorting[sortOption.value],
          limit: peopleIndex ? 1 : pageSize.value,
          offset: (page.value - 1) * pageSize.value,
        },
        controller.signal,
      )
      if (version !== loadVersion) return
      personDisplayName.value = data.person_display_name ?? ''
      items.value = data.items
      path.value = data.path
      stats.value = data.stats
      categories.value = data.categories
      total.value = data.total
      if (page.value > pageCount.value) {
        page.value = pageCount.value
        remember(true)
        await load()
      }
    } catch (cause) {
      if (version === loadVersion)
        error.value = cause instanceof ApiError ? cause.message : '加载文件列表失败'
    } finally {
      if (version === loadVersion) loading.value = false
    }
  }

  function resetSearch(): void {
    query.value = ''
    filters.value = {}
    searchScope.value = ''
    page.value = 1
  }

  async function selectView(next: typeof view.value, selectedPerson: string | null = null): Promise<void> {
    view.value = next
    person.value = selectedPerson
    personDisplayName.value = ''
    folderId.value = null
    path.value = []
    resetSearch()
    remember()
    await load()
  }

  async function openFolder(nextId: string | null): Promise<void> {
    view.value = 'all'
    person.value = null
    personDisplayName.value = ''
    folderId.value = nextId
    resetSearch()
    remember()
    await load()
  }

  async function search(): Promise<void> {
    page.value = 1
    remember()
    await load()
  }
  async function setPage(next: number): Promise<void> {
    page.value = Math.max(1, Math.min(next, pageCount.value))
    remember()
    await load()
  }

  function setLayout(next: 'list' | 'grid'): void {
    layout.value = next
    remember()
  }

  async function createFolder(name: string): Promise<void> {
    try {
      const folder = await api.createFolder(name, folderId.value)
      await openFolder(folder.id)
      useToastStore().success('文件夹已创建')
    } catch (cause) {
      useToastStore().error(cause instanceof Error ? cause.message : '创建文件夹失败')
      throw cause
    }
  }

  async function rename(item: DriveItem, name: string): Promise<void> {
    try {
      await api.updateItem(item.id, { name })
      await load()
      useToastStore().success('已重命名')
    } catch (cause) {
      useToastStore().error(cause instanceof Error ? cause.message : '重命名失败')
      throw cause
    }
  }

  async function remove(item: DriveItem): Promise<void> {
    try {
      const result = await api.deleteItem(item.id)
      await load()
      if (result.warning) useToastStore().error(result.warning)
      else useToastStore().success('已移入回收站')
    } catch (cause) {
      useToastStore().error(cause instanceof Error ? cause.message : '删除失败')
      throw cause
    }
  }

  return {
    view,
    person,
    query,
    filters,
    searchScope,
    sortOption,
    page,
    pageCount,
    total,
    pageSize,
    layout,
    title,
    categories,
    selectView,
    openFolder,
    search,
    setPage,
    setLayout,
    restoreLocation,
    folderId,
    items,
    path,
    stats,
    loading,
    error,
    load,
    createFolder,
    rename,
    remove,
  }
})
