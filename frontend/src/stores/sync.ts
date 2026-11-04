import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/client'
import type { SyncStatus } from '@/api/types'
import { useDriveStore } from './drive'

export const useSyncStore = defineStore('sync', () => {
  const status = ref<SyncStatus | null>(null)
  const error = ref('')
  const requesting = ref(false)
  const busy = computed(() => requesting.value || Boolean(status.value?.running))
  let timer: ReturnType<typeof setInterval> | null = null
  let revision: number | null = null
  let refreshing = false
  let generation = 0

  async function refresh(): Promise<void> {
    if (refreshing) return
    refreshing = true
    const version = generation
    try {
      const result = await api.syncStatus()
      if (version !== generation) return
      status.value = result
      error.value = ''
      if (
        (revision === null && result.revision > 0) ||
        (revision !== null && revision !== result.revision)
      )
        await useDriveStore().load()
      revision = result.revision
    } catch (cause) {
      if (version === generation)
        error.value = cause instanceof Error ? cause.message : '无法读取同步状态'
    } finally {
      refreshing = false
    }
  }
  function onVisible(): void {
    if (document.visibilityState === 'visible') void refresh()
  }
  function start(): void {
    if (timer) return
    void refresh()
    timer = setInterval(onVisible, 5000)
    document.addEventListener('visibilitychange', onVisible)
  }
  function stop(): void {
    generation++
    if (timer) clearInterval(timer)
    timer = null
    document.removeEventListener('visibilitychange', onVisible)
    revision = null
    status.value = null
  }
  async function run(full = false): Promise<void> {
    requesting.value = true
    error.value = ''
    try {
      status.value = await api.requestSync(full)
      await refresh()
    } catch (cause) {
      error.value = cause instanceof Error ? cause.message : '同步请求失败'
    } finally {
      requesting.value = false
    }
  }
  return { status, error, busy, refresh, start, stop, run }
})
