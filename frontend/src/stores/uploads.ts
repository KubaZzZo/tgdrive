import { defineStore } from 'pinia'
import { computed, markRaw, ref } from 'vue'

import { api, ApiError, uploadChunk } from '@/api/client'
import type { UploadRecord } from '@/api/types'
import { formatBytes, formatEta } from '@/utils/format'
import { useAuthStore } from './auth'
import { useToastStore } from './toast'

export type JobState = 'queued' | 'uploading' | 'processing' | 'done' | 'error' | 'canceled'

export interface UploadJob {
  key: string
  file: File
  parentId: string | null
  uploadId: string | null
  state: JobState
  progress: number
  received: number
  message: string
  error: string
  speed: number | null
  eta: number | null
  canceled: boolean
  controller: AbortController | null
}

const LS_KEY = 'tgdrive_resume_v2'
const MAX_ACTIVE = 2

function wait(ms: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, ms))
}

function loadResumeMap(): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(LS_KEY) ?? '{}')
    return parsed && typeof parsed === 'object' ? (parsed as Record<string, string>) : {}
  } catch {
    return {}
  }
}

function saveResumeMap(map: Record<string, string>): void {
  try {
    localStorage.setItem(LS_KEY, JSON.stringify(map))
  } catch {
    // Private mode or storage quota: upload still works without resume hint.
  }
}

function fingerprint(parentId: string | null, file: File): string {
  return [parentId ?? '', file.name, file.size, file.lastModified || 0].join('|')
}

function isAbort(error: unknown): boolean {
  return error instanceof DOMException && error.name === 'AbortError'
}

export const useUploadStore = defineStore('uploads', () => {
  const jobs = ref<UploadJob[]>([])
  const completedCount = ref(0)
  const restoredPending = ref(0)
  let active = 0

  const activeCount = computed(
    () => jobs.value.filter((job) => job.state === 'uploading' || job.state === 'processing').length,
  )
  const hasJobs = computed(() => jobs.value.length > 0)

  function enqueue(files: FileList | File[], parentId: string | null): number {
    const toast = useToastStore()
    const limit = Number(useAuthStore().meta?.max_file_size ?? 0)
    const nonEmpty = Array.from(files).filter((file) => file.size > 0)
    const selected = limit > 0 ? nonEmpty.filter((file) => file.size <= limit) : nonEmpty
    if (nonEmpty.length > selected.length) {
      toast.error(`有 ${nonEmpty.length - selected.length} 个文件超过大小限制`)
    }
    if (!selected.length) {
      if (!nonEmpty.length) toast.error('暂不支持空文件')
      return 0
    }
    for (const file of selected) {
      const job: UploadJob = {
        key: `${Date.now()}-${Math.random().toString(16).slice(2)}`,
        file: markRaw(file),
        parentId,
        uploadId: null,
        state: 'queued',
        progress: 0,
        received: 0,
        message: '等待上传',
        error: '',
        speed: null,
        eta: null,
        canceled: false,
        controller: null,
      }
      jobs.value.push(job)
    }
    toast.push(`已加入 ${selected.length} 个上传任务`)
    pump()
    return selected.length
  }

  function pump(): void {
    while (active < MAX_ACTIVE) {
      const job = jobs.value.find((candidate) => candidate.state === 'queued' && !candidate.canceled)
      if (!job) break
      active += 1
      void runJob(job).finally(() => {
        active -= 1
        pump()
      })
    }
  }

  async function ensureUpload(job: UploadJob): Promise<{ id: string; chunkSize: number; offset: number }> {
    const map = loadResumeMap()
    const key = fingerprint(job.parentId, job.file)
    const known = map[key]
    if (known) {
      try {
        const info = await api.getUpload(known)
        if (
          Number(info.size) === job.file.size &&
          info.state !== 'done' &&
          Number(info.received) <= job.file.size
        ) {
          if (info.state === 'processing') {
            throw new Error('该文件正在 Telegram 写入中，请稍后查看结果')
          }
          job.uploadId = info.id
          return {
            id: info.id,
            chunkSize: Number(info.chunk_size),
            offset: Number(info.received),
          }
        }
      } catch (cause) {
        if (cause instanceof Error && cause.message.includes('正在 Telegram')) throw cause
      }
      delete map[key]
      saveResumeMap(map)
    }

    const created = await api.createUpload({
      name: job.file.name,
      size: job.file.size,
      mime: job.file.type || 'application/octet-stream',
      parent_id: job.parentId,
    })
    map[key] = created.id
    saveResumeMap(map)
    job.uploadId = created.id
    return { id: created.id, chunkSize: Number(created.chunk_size), offset: 0 }
  }

  async function runJob(job: UploadJob): Promise<void> {
    const toast = useToastStore()
    job.state = 'uploading'
    job.message = '连接服务器…'
    job.error = ''
    job.canceled = false
    let attempt = 0
    let offset = 0

    try {
      const source = await ensureUpload(job)
      offset = source.offset
      job.received = offset
      job.progress = Math.min(80, (offset / job.file.size) * 80)
      if (offset > 0) job.message = `断点续传：已有 ${formatBytes(offset)}`
      else job.message = '上传中'

      let lastTime = performance.now()
      let lastOffset = offset

      while (offset < job.file.size) {
        if (job.canceled) return
        const end = Math.min(offset + source.chunkSize, job.file.size)
        const controller = new AbortController()
        job.controller = controller

        try {
          const result = await uploadChunk(
            source.id,
            offset,
            job.file.slice(offset, end),
            controller.signal,
          )
          if (result.kind === 'conflict') {
            offset = result.offset
            job.received = offset
            attempt = 0
            continue
          }
          if (result.kind === 'error') {
            if (!result.retryable || attempt >= 4) throw new Error(result.message)
            attempt += 1
            job.message = `服务异常，${2 ** (attempt - 1)} 秒后重试…`
            await wait(1000 * 2 ** (attempt - 1))
            continue
          }

          offset = Number(result.data.offset)
          job.received = offset
          attempt = 0
          const now = performance.now()
          const elapsed = (now - lastTime) / 1000
          if (elapsed >= 0.6) {
            job.speed = (offset - lastOffset) / elapsed
            lastTime = now
            lastOffset = offset
          }
          job.progress = Math.min(80, (offset / job.file.size) * 80)
          job.eta = job.speed && job.speed > 0 ? (job.file.size - offset) / job.speed : null
          job.message =
            `上传中 ${formatBytes(offset)} / ${formatBytes(job.file.size)}` +
            (job.speed ? ` · ${formatBytes(job.speed)}/s` : '') +
            (job.eta ? ` · 剩 ${formatEta(job.eta)}` : '')
        } catch (cause) {
          if (job.canceled || isAbort(cause)) return
          attempt += 1
          if (attempt > 4) throw cause
          job.message = `网络中断，${2 ** (attempt - 1)} 秒后重试…`
          await wait(1000 * 2 ** (attempt - 1))
        } finally {
          job.controller = null
        }
      }

      if (job.canceled) return
      job.state = 'processing'
      job.progress = 80
      job.message = '提交到 Telegram…'
      await api.completeUpload(source.id)

      let polls = 0
      while (true) {
        await wait(1200)
        if (job.canceled) return
        const record: UploadRecord = await api.getUpload(source.id)
        if (record.state === 'done') {
          job.state = 'done'
          job.progress = 100
          job.message = '完成'
          job.speed = null
          job.eta = null
          const map = loadResumeMap()
          delete map[fingerprint(job.parentId, job.file)]
          saveResumeMap(map)
          completedCount.value += 1
          toast.success(`${job.file.name} 上传完成`)
          return
        }
        if (record.state === 'error') throw new Error(record.error || 'Telegram 保存失败')
        const ratio = record.size ? Number(record.progress) / Number(record.size) : 0
        job.progress = 80 + ratio * 20
        job.message = `Telegram 保存中 ${formatBytes(record.progress)} / ${formatBytes(record.size)}`
        polls += 1
        if (polls > 1500) throw new Error('Telegram 保存超时，请稍后重试')
      }
    } catch (cause) {
      if (job.canceled || isAbort(cause)) return
      const message = cause instanceof Error ? cause.message : '上传失败'
      job.state = 'error'
      job.error = message
      job.message = message
      job.speed = null
      job.eta = null
      toast.error(`${job.file.name}：${message}`)
    }
  }

  async function cancel(job: UploadJob): Promise<void> {
    if (job.state === 'processing') {
      useToastStore().error('正在写入 Telegram，暂时不能取消')
      return
    }
    if (job.state === 'done' || job.state === 'canceled') return
    job.canceled = true
    job.controller?.abort()
    if (job.uploadId) {
      try {
        await api.cancelUpload(job.uploadId)
      } catch {
        // The request may already be gone; local cancellation still wins.
      }
    }
    const map = loadResumeMap()
    delete map[fingerprint(job.parentId, job.file)]
    saveResumeMap(map)
    job.state = 'canceled'
    job.message = '已取消'
    job.speed = null
    job.eta = null
  }

  function retry(job: UploadJob): void {
    if (job.state !== 'error' && job.state !== 'canceled') return
    job.canceled = false
    job.error = ''
    job.state = 'queued'
    job.progress = 0
    job.received = 0
    job.message = '等待重试'
    pump()
  }

  function clearFinished(): void {
    jobs.value = jobs.value.filter(
      (job) => job.state !== 'done' && job.state !== 'error' && job.state !== 'canceled',
    )
  }

  async function restore(): Promise<number> {
    try {
      const data = await api.listUploads()
      restoredPending.value = data.uploads.filter((upload) => Number(upload.received) > 0).length
      return restoredPending.value
    } catch {
      return 0
    }
  }

  return {
    jobs,
    completedCount,
    restoredPending,
    activeCount,
    hasJobs,
    enqueue,
    cancel,
    retry,
    clearFinished,
    restore,
  }
})

export { fingerprint as uploadFingerprint }
export { formatBytes, ApiError }
