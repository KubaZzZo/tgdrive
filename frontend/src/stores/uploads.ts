import { defineStore } from 'pinia'
import { computed, markRaw, ref } from 'vue'

import { api, ApiError, uploadChunk } from '@/api/client'
import type { UploadRecord } from '@/api/types'
import { formatBytes, formatEta } from '@/utils/format'
import { useAuthStore } from './auth'
import { useToastStore } from './toast'

export type JobState = 'queued' | 'paused' | 'uploading' | 'processing' | 'done' | 'error' | 'canceling' | 'canceled'

export interface UploadJob {
  imageHost: boolean
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
function resumeStorageKey(): string {
  const meta = useAuthStore().meta
  return meta?.workspace_is_default === false ? `${LS_KEY}:${meta.storage.chat_id}` : LS_KEY
}
const wait = (ms: number): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, ms))

function loadResumeMap(): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(resumeStorageKey()) ?? '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

function saveResume(key: string, id: string | null): void {
  try {
    const map = loadResumeMap()
    if (id) map[key] = id
    else delete map[key]
    localStorage.setItem(resumeStorageKey(), JSON.stringify(map))
  } catch {
    // Upload remains available when local storage is disabled.
  }
}

function fingerprint(parentId: string | null, file: File, imageHost = false): string {
  return [parentId ?? '', file.name, file.size, file.lastModified || 0].join('|') + (imageHost ? '|image-host' : '')
}

function retryable(cause: unknown): boolean {
  return cause instanceof ApiError && (cause.status === 0 || cause.status === 429 || cause.status >= 500)
}

export const useUploadStore = defineStore('uploads', () => {
  const jobs = ref<UploadJob[]>([])
  const completedCount = ref(0)
  const restoredPending = ref(0)
  const running = new Map<string, Promise<void>>()
  const activeCount = computed(() => jobs.value.filter((j) => j.state === 'uploading' || j.state === 'processing').length)
  const hasJobs = computed(() => jobs.value.length > 0)
  const totalProgress = computed(() => {
    const included = jobs.value.filter((j) => j.state !== 'canceled')
    const size = included.reduce((sum, j) => sum + j.file.size, 0)
    return size ? included.reduce((sum, j) => sum + j.file.size * j.progress, 0) / size : 0
  })
  const stopped = (job: UploadJob): boolean => job.canceled || job.state === 'paused' || job.state === 'queued'

  function enqueue(files: FileList | File[], parentId: string | null, imageHost = false): number {
    const toast = useToastStore()
    const limit = Number(useAuthStore().meta?.max_file_size ?? 0)
    let added = 0
    for (const file of Array.from(files)) {
      if (imageHost && (file.size > 20 * 1024 * 1024 || !/\.(jpe?g|png|gif|webp)$/i.test(file.name))) {
        toast.error(`${file.name}：请选择 20 MiB 以内的 JPEG、PNG、GIF 或 WebP 图片`)
        continue
      }
      if (!file.size || (limit > 0 && file.size > limit)) {
        toast.error(`${file.name}：文件为空或超过大小限制`)
        continue
      }
      const key = fingerprint(parentId, file, imageHost)
      if (jobs.value.some((j) => !['done', 'canceled'].includes(j.state) && fingerprint(j.parentId, j.file, j.imageHost) === key)) {
        toast.push(`${file.name} 已在上传队列中`)
        continue
      }
      jobs.value.push({
        key: crypto.randomUUID(), file: markRaw(file), parentId, uploadId: null, imageHost,
        state: 'queued', progress: 0, received: 0, message: '等待上传', error: '',
        speed: null, eta: null, canceled: false, controller: null,
      })
      added += 1
    }
    if (added) toast.push(`已加入 ${added} 个上传任务`)
    pump()
    return added
  }

  function pump(): void {
    while (running.size < MAX_ACTIVE) {
      const job = jobs.value.find((j) => j.state === 'queued' && !j.canceled && !running.has(j.key))
      if (!job) break
      const task = runJob(job).finally(() => {
        running.delete(job.key)
        pump()
      })
      running.set(job.key, task)
    }
  }

  async function withRetry<T>(operation: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt += 1) {
      try { return await operation() }
      catch (cause) {
        if (!retryable(cause) || attempt >= 4) throw cause
        await wait(1000 * 2 ** attempt)
      }
    }
  }

  async function ensureUpload(job: UploadJob): Promise<Pick<UploadRecord, 'id' | 'received' | 'chunk_size' | 'state'>> {
    const key = fingerprint(job.parentId, job.file, job.imageHost)
    const known = job.uploadId ?? loadResumeMap()[key]
    if (known) {
      try {
        const info = await withRetry(() => api.getUpload(known))
        if (Number(info.size) === job.file.size && info.name === job.file.name && info.parent_id === job.parentId && Boolean(info.image_host) === job.imageHost) {
          job.uploadId = info.id
          return info
        }
      } catch (cause) {
        // Keep the existing upload on network/auth errors; only 404 permits recreation.
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause
      }
      job.uploadId = null
      saveResume(key, null)
    }
    // Creation is not retried automatically: a lost response may already have created a task.
    const created = await api.createUpload({ name: job.file.name, size: job.file.size,
      mime: job.file.type || 'application/octet-stream', parent_id: job.parentId, image_host: job.imageHost })
    job.uploadId = created.id
    saveResume(key, created.id)
    return { ...created, received: created.offset, state: 'uploading' }
  }

  async function runJob(job: UploadJob): Promise<void> {
    job.state = 'uploading'
    job.message = '连接服务器…'
    job.error = ''
    try {
      const source = await ensureUpload(job)
      if (stopped(job)) return
      let offset = Number(source.received)
      job.received = offset
      job.progress = Math.min(80, offset / job.file.size * 80)
      if (!['processing', 'done'].includes(source.state)) {
        let attempt = 0
        let conflicts = 0
        let lastTime = performance.now()
        let lastOffset = offset
        while (offset < job.file.size) {
          if (stopped(job)) return
          const end = Math.min(offset + Number(source.chunk_size), job.file.size)
          if (end <= offset) throw new Error('服务器返回了无效的分片大小')
          const controller = new AbortController()
          job.controller = controller
          const timeout = window.setTimeout(() => controller.abort(), 60_000)
          try {
            const result = await uploadChunk(source.id, offset, job.file.slice(offset, end), controller.signal)
            if (stopped(job)) return
            if (result.kind === 'error') throw new ApiError(result.message, result.status)
            const next = Number(result.kind === 'conflict' ? result.offset : result.data.offset)
            if (!Number.isSafeInteger(next) || next < 0 || next > job.file.size) throw new Error('服务器返回了无效的上传进度')
            if (result.kind === 'conflict') {
              if (++conflicts > 4) throw new Error('上传进度持续冲突，请稍后重试')
            } else {
              if (next <= offset) throw new Error('上传进度没有前进，请稍后重试')
              conflicts = 0
            }
            offset = next
            job.received = offset
            attempt = 0
            const now = performance.now()
            if (now - lastTime >= 600) {
              job.speed = Math.max(0, (offset - lastOffset) / ((now - lastTime) / 1000))
              lastTime = now
              lastOffset = offset
            }
            job.progress = Math.min(80, offset / job.file.size * 80)
            job.eta = job.speed ? (job.file.size - offset) / job.speed : null
            job.message = `上传中 ${formatBytes(offset)} / ${formatBytes(job.file.size)}` +
              (job.speed ? ` · ${formatBytes(job.speed)}/s` : '') + (job.eta ? ` · 剩 ${formatEta(job.eta)}` : '')
          } catch (cause) {
            if (stopped(job)) return
            const error = controller.signal.aborted ? new ApiError('上传请求超时', 0) : cause
            if (!retryable(error) || attempt >= 4) throw error
            job.message = `连接异常，${2 ** attempt} 秒后重试…`
            await wait(1000 * 2 ** attempt++)
          } finally {
            window.clearTimeout(timeout)
            job.controller = null
          }
        }
      }
      if (stopped(job)) return
      job.state = 'processing'
      job.message = '等待 Telegram 保存…'
      job.progress = 80
      if (!['processing', 'done'].includes(source.state)) await withRetry(() => api.completeUpload(source.id))
      // Resume observing an existing Telegram task, including one completed while offline.
      for (let polls = 0; ; polls += 1) {
        const record = await withRetry(() => api.getUpload(source.id))
        if (record.state === 'done') {
          job.state = 'done'
          job.received = job.file.size
          job.progress = 100
          job.message = '完成'
          saveResume(fingerprint(job.parentId, job.file, job.imageHost), null)
          completedCount.value += 1
          useToastStore().success(`${job.file.name} 上传完成`)
          return
        }
        if (record.state === 'error') throw new Error(record.error || 'Telegram 保存失败')
        job.progress = 80 + Math.max(0, Math.min(1, Number(record.progress) / job.file.size)) * 20
        job.message = `Telegram 保存中 ${formatBytes(record.progress)} / ${formatBytes(job.file.size)}`
        // Long transfers poll less often to reduce metadata traffic.
        await wait(Math.min(5000, 1200 + polls * 200))
      }
    } catch (cause) {
      if (stopped(job)) return
      job.state = 'error'
      job.error = cause instanceof Error ? cause.message : '上传失败'
      job.message = job.error
      useToastStore().error(`${job.file.name}：${job.error}`)
    } finally {
      job.speed = null
      job.eta = null
    }
  }

  function pause(job: UploadJob): void {
    if (job.state !== 'queued' && job.state !== 'uploading') return
    job.state = 'paused'
    job.message = '已暂停，可继续上传'
    job.controller?.abort()
  }

  function resume(job: UploadJob): void {
    if (!['paused', 'error', 'canceled'].includes(job.state)) return
    job.canceled = false
    job.error = ''
    job.state = 'queued'
    job.message = '等待继续上传'
    pump()
  }

  async function cancel(job: UploadJob): Promise<void> {
    if (job.state === 'processing') {
      useToastStore().error('正在写入 Telegram，暂时不能取消')
      return
    }
    if (job.state === 'done' || job.canceled) return
    job.canceled = true
    job.state = 'canceling'
    job.message = '正在取消…'
    job.controller?.abort()
    await running.get(job.key)
    if (job.uploadId) {
      try { await api.cancelUpload(job.uploadId) }
      catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) {
          job.state = 'error'
          job.canceled = false
          job.message = cause instanceof Error ? cause.message : '取消失败，请重试'
          return
        }
      }
    }
    saveResume(fingerprint(job.parentId, job.file, job.imageHost), null)
    job.uploadId = null
    job.state = 'canceled'
    job.message = '已取消'
  }

  function pauseAll(): void { jobs.value.forEach(pause) }
  function resumeAll(): void { jobs.value.filter((j) => j.state === 'paused').forEach(resume) }
  function retryAll(): void { jobs.value.filter((j) => j.state === 'error').forEach(resume) }
  function clearFinished(): void {
    jobs.value = jobs.value.filter((j) => j.state !== 'done' && (j.state !== 'canceled' || j.uploadId !== null))
  }
  async function restore(): Promise<number> {
    try {
      const data = await api.listUploads()
      restoredPending.value = data.uploads.filter((u) => Number(u.received) > 0).length
      return restoredPending.value
    } catch { return 0 }
  }

  return { jobs, completedCount, restoredPending, activeCount, hasJobs, totalProgress,
    enqueue, pause, resume, pauseAll, resumeAll, retryAll, cancel, retry: resume, clearFinished, restore }
})

export { fingerprint as uploadFingerprint }
export { formatBytes, ApiError }
