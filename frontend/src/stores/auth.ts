import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api, ApiError, setWorkspaceId } from '@/api/client'
import type { Meta, StorageStatus } from '@/api/types'

export const useAuthStore = defineStore('auth', () => {
  const meta = ref<Meta | null>(null)
  const loading = ref(false)
  const error = ref('')
  const ready = ref(false)

  const authenticated = computed(() => Boolean(meta.value?.authenticated))
  const storage = computed<StorageStatus | null>(() => meta.value?.storage ?? null)

  async function bootstrap(): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      meta.value = await api.meta()
      setWorkspaceId(meta.value.storage.chat_id)
    } catch (cause) {
      error.value = cause instanceof ApiError ? cause.message : '无法连接后端服务'
    } finally {
      loading.value = false
      ready.value = true
    }
  }

  async function login(password: string): Promise<void> {
    loading.value = true
    error.value = ''
    try {
      await api.login(password)
      meta.value = await api.meta()
      setWorkspaceId(meta.value.storage.chat_id)
      if (!meta.value.authenticated) throw new Error('登录状态未生效')
    } catch (cause) {
      error.value = cause instanceof ApiError ? cause.message : '登录失败'
      throw cause
    } finally {
      loading.value = false
    }
  }

  async function logout(): Promise<void> {
    try {
      await api.logout()
    } finally {
      meta.value = meta.value ? { ...meta.value, authenticated: false } : null
    }
  }

  async function refreshMeta(): Promise<void> {
    meta.value = await api.meta()
    setWorkspaceId(meta.value.storage.chat_id)
  }

  function markUnauthorized(): void {
    if (meta.value) meta.value = { ...meta.value, authenticated: false }
    else meta.value = null
  }

  return {
    meta,
    loading,
    error,
    ready,
    authenticated,
    storage,
    bootstrap,
    login,
    logout,
    refreshMeta,
    markUnauthorized,
  }
})
