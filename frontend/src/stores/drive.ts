import { defineStore } from 'pinia'
import { ref } from 'vue'

import { api, ApiError } from '@/api/client'
import type { DriveItem, PathItem, Stats } from '@/api/types'
import { useToastStore } from './toast'

export const useDriveStore = defineStore('drive', () => {
  const folderId = ref<string | null>(null)
  const items = ref<DriveItem[]>([])
  const path = ref<PathItem[]>([])
  const stats = ref<Stats>({ bytes: 0, files: 0, folders: 0 })
  const loading = ref(false)
  const error = ref('')
  let loadVersion = 0

  async function load(nextFolderId: string | null = folderId.value): Promise<void> {
    const version = ++loadVersion
    loading.value = true
    error.value = ''
    try {
      const data = await api.listItems(nextFolderId)
      if (version !== loadVersion) return
      folderId.value = nextFolderId
      items.value = data.items
      path.value = data.path
      stats.value = data.stats
    } catch (cause) {
      if (version === loadVersion) {
        error.value = cause instanceof ApiError ? cause.message : '加载文件列表失败'
      }
    } finally {
      if (version === loadVersion) loading.value = false
    }
  }

  async function createFolder(name: string): Promise<void> {
    try {
      await api.createFolder(name, folderId.value)
      await load()
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

  async function share(item: DriveItem): Promise<string> {
    try {
      const result = await api.createShare(item.id)
      return result.url
    } catch (cause) {
      useToastStore().error(cause instanceof Error ? cause.message : '创建分享链接失败')
      throw cause
    }
  }

  return {
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
    share,
  }
})
