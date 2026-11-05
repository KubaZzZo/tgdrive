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
import { useToastStore } from './toast'

export const useDriveStore = defineStore('drive', () => {
  const folderId = ref<string | null>(null)
  const items = ref<DriveItem[]>([])
  const path = ref<PathItem[]>([])
  const stats = ref<Stats>({ bytes: 0, files: 0, folders: 0 })
  const loading = ref(false)
  const error = ref('')
  const categories = ref<CategoryStats[]>(defaultCategories.map((c) => ({ ...c })))
  const view = ref<'all' | 'recent' | FileCategory>('all')
  const query = ref('')
  const sortOption = ref('newest')
  const page = ref(1)
  const total = ref(0)
  const pageSize = 60
  const layout = ref<'list' | 'grid'>('list')
  const pageCount = computed(() => Math.max(1, Math.ceil(total.value / pageSize)))
  const title = computed(() =>
    folderId.value
      ? (path.value.at(-1)?.name ?? '文件夹')
      : view.value === 'all'
        ? '我的文件'
        : view.value === 'recent'
          ? '最近上传'
          : (categories.value.find((c) => c.id === view.value)?.label ?? '我的文件'),
  )
  let loadVersion = 0
  let controller: AbortController | null = null

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
    const category = view.value !== 'all' && view.value !== 'recent' ? view.value : undefined
    const scope = category
      ? 'all'
      : view.value === 'recent'
        ? 'recent'
        : query.value.trim() && !folderId.value
          ? 'all'
          : 'folder'
    try {
      const data = await api.listItems(
        folderId.value,
        {
          scope,
          category,
          q: query.value,
          ...sorting[sortOption.value],
          limit: pageSize,
          offset: (page.value - 1) * pageSize,
        },
        controller.signal,
      )
      if (version !== loadVersion) return
      items.value = data.items
      path.value = data.path
      stats.value = data.stats
      categories.value = data.categories
      total.value = data.total
      if (page.value > pageCount.value) {
        page.value = pageCount.value
        await load()
      }
    } catch (cause) {
      if (version === loadVersion)
        error.value = cause instanceof ApiError ? cause.message : '加载文件列表失败'
    } finally {
      if (version === loadVersion) loading.value = false
    }
  }

  async function selectView(next: typeof view.value): Promise<void> {
    view.value = next
    folderId.value = null
    path.value = []
    query.value = ''
    page.value = 1
    await load()
  }

  async function openFolder(nextId: string | null): Promise<void> {
    view.value = 'all'
    folderId.value = nextId
    query.value = ''
    page.value = 1
    await load()
  }

  async function search(): Promise<void> {
    page.value = 1
    await load()
  }
  async function setPage(next: number): Promise<void> {
    page.value = Math.max(1, Math.min(next, pageCount.value))
    await load()
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
      else useToastStore().success('已删除')
    } catch (cause) {
      useToastStore().error(cause instanceof Error ? cause.message : '删除失败')
      throw cause
    }
  }

  return {
    view,
    query,
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
