import { defineStore } from 'pinia'
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { TrafficStatus } from '@/api/types'

export function gb(bytes: number): string {
  return (bytes / 1e9).toFixed(2)
}

export const useTrafficStore = defineStore('traffic', () => {
  const saving = ref(false)
  const data = ref<TrafficStatus | null>(null)
  const error = ref('')
  let timer: ReturnType<typeof setInterval> | undefined
  let version = 0
  const usage = computed(() => data.value?.usage)
  const cdn = computed(() => data.value?.cdn)
  const label = computed(() => {
    if (cdn.value?.configured && cdn.value.blocked) return 'CDN 传输保护中'
    if (cdn.value?.configured && cdn.value.warning) return 'CDN 余量预警'
    if (cdn.value?.configured && cdn.value.available) return 'CDN 流量正常'
    if (error.value) return '流量读取失败'
    return usage.value ? `今日 ${gb(usage.value.used)} GB` : '流量'
  })
  async function refresh(): Promise<void> {
    const current = ++version
    try {
      const result = await api.traffic()
      if (current !== version) return
      data.value = result
      error.value = ''
    } catch (cause) {
      if (current === version) error.value = cause instanceof Error ? cause.message : '无法读取流量'
    }
  }

  async function save(bytes: number): Promise<void> {
    if (saving.value) return
    if (!Number.isSafeInteger(bytes) || bytes <= 0) {
      error.value = '请输入有效的每日额度'
      return
    }
    saving.value = true
    const current = ++version
    try {
      const result = await api.saveTrafficQuota(bytes)
      if (current === version) { data.value = result; error.value = '' }
    } catch (cause) {
      if (current === version) error.value = cause instanceof Error ? cause.message : '保存失败'
    } finally { saving.value = false }
  }
  function start(): void {
    if (timer !== undefined) return
    void refresh()
    timer = setInterval(() => { if (document.visibilityState === 'visible' && !saving.value) void refresh() }, 15_000)
  }
  function stop(): void {
    version++
    clearInterval(timer)
    timer = undefined
    data.value = null
    error.value = ''
  }
  const warning = computed(() => Boolean(cdn.value?.warning || usage.value?.warning || error.value))
  return { data, error, saving, usage, cdn, label, warning, start, stop, save }
})
