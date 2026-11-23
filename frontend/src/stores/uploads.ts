import { defineStore } from 'pinia'
import { computed, markRaw, ref } from 'vue'

import { api, ApiError, taskEvents, uploadChunk } from '@/api/client'
import type { UploadRecord, UploadStage } from '@/api/types'
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
  stage: UploadStage | 'verifying' | 'receiving' | null
  progress: number | null
  workspaceKey: string
  workspaceId: string | null
  resumeKey: string | null
  received: number
  message: string
  error: string
  speed: number | null
  eta: number | null
  canceled: boolean
  controller: AbortController | null
}

const LS_KEY = 'tgdrive_resume_v2'
const MAX_SENDERS = 2
const MAX_RESERVED_SAVES = 4
const MAX_RESERVED_BYTES = 4 * 1024 ** 3
const INITIAL_CHUNK_BYTES = 256 * 1024
const MIN_CHUNK_BYTES = 32 * 1024
const MAX_CHUNK_BYTES = 8 * 1024 ** 2
const HASH_BLOCK_BYTES = 4 * 1024 ** 2
const wait = (ms: number): Promise<void> => new Promise((resolve) => window.setTimeout(resolve, ms))

function loadResumeMap(storageKey: string): Record<string, string> {
  try {
    const parsed = JSON.parse(localStorage.getItem(storageKey) ?? '{}')
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : {}
  } catch {
    return {}
  }
}

function saveResume(job: UploadJob, id: string | null): void {
  if (!job.resumeKey) return
  try {
    const map = loadResumeMap(job.workspaceKey)
    if (id) map[job.resumeKey] = id
    else if (map[job.resumeKey] === job.uploadId) delete map[job.resumeKey]
    localStorage.setItem(job.workspaceKey, JSON.stringify(map))
  } catch {
    // Upload remains available when local storage is disabled.
  }
}

function fingerprint(parentId: string | null, file: File, imageHost = false): string {
  return [parentId ?? '', file.name, file.size, file.lastModified || 0].join('|') + (imageHost ? '|image-host' : '')
}

function retryable(cause: unknown): boolean {
  return cause instanceof ApiError &&
    ([0, 408, 429].includes(cause.status) || (cause.status >= 500 && cause.status !== 507))
}

export const useUploadStore = defineStore('uploads', () => {
  const jobs = ref<UploadJob[]>([])
  const completedCount = ref(0)
  const auth = useAuthStore()
  const running = new Map<string, Promise<void>>()
  const reservations = new Map<string, number>()
  const senders = new Set<string>()
  const workspaceId = () => auth.meta?.storage.chat_id ?? null
  const workspaceKey = () => workspaceId() ? `${LS_KEY}:${workspaceId()}` : LS_KEY
  const sameWorkspace = (job: UploadJob) => job.workspaceId === workspaceId() && job.workspaceKey === workspaceKey()
  const activeCount = computed(() => jobs.value.filter((j) => j.state === 'uploading' || j.state === 'processing').length)
  const sendingCount = computed(() => jobs.value.filter((job) => job.state === 'uploading').length)
  const processingCount = computed(() => jobs.value.filter((job) => job.state === 'processing').length)
  const hasJobs = computed(() => jobs.value.length > 0)
  const totalProgress = computed(() => {
    const included = jobs.value.filter((j) => j.state !== 'canceled')
    const size = included.reduce((sum, j) => sum + j.file.size, 0)
    return size ? included.reduce((sum, j) => sum + j.received, 0) / size * 100 : 0
  })
  const interrupted = (job: UploadJob) => job.canceled || job.state === 'paused' || job.state === 'queued'
  function stopped(job: UploadJob): boolean {
    if (interrupted(job)) return true
    if (!sameWorkspace(job)) throw new Error('存储空间已切换，请返回原空间后继续')
    return false
  }

  function enqueue(files: FileList | File[], parentId: string | null, imageHost = false): number {
    return enqueueFiles(Array.from(files, (file) => ({ file, parentId })), imageHost)
  }

  function enqueueFiles(entries: { file: File; parentId: string | null }[], imageHost = false): number {
    const toast = useToastStore()
    const limit = Number(auth.meta?.max_file_size ?? 0)
    const queued = new Set(jobs.value
      .filter((job) => sameWorkspace(job) && !['done', 'canceled'].includes(job.state))
      .map((job) => fingerprint(job.parentId, job.file, job.imageHost)))
    let added = 0
    for (const { file, parentId } of entries) {
      if (imageHost && (file.size > 20 * 1024 * 1024 || !/\.(jpe?g|png|gif|webp)$/i.test(file.name))) {
        toast.error(`${file.name}：请选择 20 MiB 以内的 JPEG、PNG、GIF 或 WebP 图片`)
        continue
      }
      if (!file.size || (limit > 0 && file.size > limit)) {
        toast.error(`${file.name}：文件为空或超过大小限制`)
        continue
      }
      const key = fingerprint(parentId, file, imageHost)
      if (queued.has(key)) {
        toast.push(`${file.name} 已在上传队列中`)
        continue
      }
      queued.add(key)
      jobs.value.push({
        key: crypto.randomUUID(), file: markRaw(file), parentId, uploadId: null, imageHost,
        workspaceId: workspaceId(), workspaceKey: workspaceKey(), resumeKey: null,
        state: 'queued', stage: null, progress: null, received: 0, message: '等待上传', error: '',
        speed: null, eta: null, canceled: false, controller: null,
      })
      added += 1
    }
    if (added) toast.push(`已加入 ${added} 个上传任务`)
    pump()
    return added
  }

  function pump(): void {
    while (senders.size < MAX_SENDERS && reservations.size < MAX_RESERVED_SAVES) {
      const job = jobs.value.find((j) => j.state === 'queued' && !j.canceled && !running.has(j.key) && sameWorkspace(j))
      if (!job) break

      // Reserve through the server save so a burst of uploads cannot overfill the save queue.
      // A single file larger than the byte budget can proceed on its own.
      const bytes = Math.min(job.file.size, MAX_RESERVED_BYTES)
      const reserved = [...reservations.values()].reduce((sum, size) => sum + size, 0)
      if (reserved + bytes > MAX_RESERVED_BYTES) break

      reservations.set(job.key, bytes)
      senders.add(job.key)
      const task = runJob(job).finally(() => {
        senders.delete(job.key)
        reservations.delete(job.key)
        running.delete(job.key)
        pump()
      })
      running.set(job.key, task)
    }
  }

  async function withRetry<T>(job: UploadJob, operation: () => Promise<T>): Promise<T> {
    for (let attempt = 0; ; attempt += 1) {
      if (stopped(job)) throw new Error('上传已暂停或取消')
      try { return await operation() }
      catch (cause) {
        if (!retryable(cause) || attempt >= 4 || stopped(job)) throw cause
        await wait(500 * 2 ** attempt)
      }
    }
  }

  async function discardUpload(job: UploadJob): Promise<void> {
    if (!sameWorkspace(job)) throw new Error('存储空间已切换，请返回原空间后取消')
    if (job.uploadId) {
      try { await api.cancelUpload(job.uploadId) }
      catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause
      }
    }
    forgetUpload(job)
  }

  function forgetUpload(job: UploadJob): void {
    saveResume(job, null)
    job.uploadId = null
    job.received = 0
    job.progress = null
  }

  async function verifyFile(job: UploadJob): Promise<void> {
    if (job.resumeKey) return
    job.stage = 'verifying'
    job.progress = 0
    job.message = '正在校验本地文件…'
    // Chain fixed-size SHA-256 blocks to cover every byte with bounded memory.
    // Metadata-only resume entries cannot safely identify previously selected files.
    let digest = new Uint8Array(32)
    for (let start = 0; start < job.file.size; start += HASH_BLOCK_BYTES) {
      const end = Math.min(start + HASH_BLOCK_BYTES, job.file.size)
      const block = new Uint8Array(digest.length + end - start)
      block.set(digest)
      block.set(new Uint8Array(await job.file.slice(start, end).arrayBuffer()), digest.length)
      if (stopped(job)) return
      digest = new Uint8Array(await crypto.subtle.digest('SHA-256', block))
      if (stopped(job)) return
      job.progress = end / job.file.size * 100
    }
    const hash = Array.from(digest, (byte) => byte.toString(16).padStart(2, '0')).join('')
    job.resumeKey = JSON.stringify([job.parentId, job.file.name, job.file.size, job.imageHost, hash])
  }

  async function ensureUpload(job: UploadJob, key: string): Promise<Pick<UploadRecord, 'id' | 'received' | 'chunk_size' | 'state'>> {
    const known = job.uploadId ?? loadResumeMap(job.workspaceKey)[key]
    if (typeof known === 'string') {
      try {
        const info = await withRetry(job, () => api.getUpload(known))
        if (info?.id === known && info.state !== 'invalid' &&
          Number(info.size) === job.file.size && info.name === job.file.name &&
          info.parent_id === job.parentId && Boolean(info.image_host) === job.imageHost) {
          job.uploadId = info.id
          return info
        }
      } catch (cause) {
        if (!(cause instanceof ApiError) || cause.status !== 404) throw cause
      }
      if (!stopped(job)) {
        job.uploadId = known
        forgetUpload(job)
      }
    }
    if (stopped(job)) throw new Error('上传已暂停或取消')
    // Creation can succeed even when the browser never receives the response.
    const created = await api.createUpload({ name: job.file.name, size: job.file.size,
      mime: job.file.type || 'application/octet-stream', parent_id: job.parentId, image_host: job.imageHost })
    if (!created?.id) throw new ApiError('未收到服务器创建的上传任务', 0)
    job.uploadId = created.id
    saveResume(job, created.id)
    return { ...created, received: created.offset, state: 'uploading' }
  }

  function confirmedOffset(job: UploadJob, value: number): number {
    if (!Number.isSafeInteger(value) || value < 0 || value > job.file.size) {
      throw new Error('服务器返回了无效的上传进度')
    }
    return value
  }

  function showTransfer(job: UploadJob, label: string, sent: number): void {
    job.progress = sent / job.file.size * 100
    job.eta = job.speed ? (job.file.size - sent) / job.speed : null
    job.message = `${label} ${formatBytes(sent)} / ${formatBytes(job.file.size)}` +
      (job.speed ? ` · ${formatBytes(job.speed)}/s` : '') + (job.eta ? ` · 剩 ${formatEta(job.eta)}` : '')
  }

  async function sendChunks(job: UploadJob, id: string, serverLimit: number): Promise<void> {
    if (!Number.isSafeInteger(serverLimit) || serverLimit <= 0) throw new Error('服务器返回了无效的分片大小')
    const maxChunk = Math.min(serverLimit, MAX_CHUNK_BYTES)
    const minChunk = Math.min(maxChunk, MIN_CHUNK_BYTES)
    let chunkSize = Math.min(maxChunk, INITIAL_CHUNK_BYTES)
    let retries = 0
    let conflicts = 0
    let lastTime = performance.now()
    let lastOffset = job.received
    job.stage = 'receiving'
    showTransfer(job, '发送至服务器', job.received)
    while (job.received < job.file.size && !stopped(job)) {
      const offset = job.received
      const end = Math.min(offset + chunkSize, job.file.size)
      const controller = new AbortController()
      job.controller = controller
      const start = performance.now()
      const timeout = window.setTimeout(() => controller.abort(), 120_000)
      try {
        const result = await uploadChunk(id, offset, job.file.slice(offset, end), controller.signal)
        if (stopped(job)) return
        if (result.kind === 'error') throw new ApiError(result.message, result.status)
        const next = confirmedOffset(job, result.kind === 'conflict' ? result.offset : result.data.offset)
        const elapsed = Math.max(1, performance.now() - start)
        if (result.kind === 'conflict') {
          if (++conflicts > 4) throw new Error('上传进度持续冲突，请稍后重试')
        } else {
          if (next <= offset) throw new Error('上传进度没有前进，请稍后重试')
          conflicts = 0
          if (!result.data.duplicate) {
            // Aim for eight seconds per chunk. Grow gradually as throughput improves.
            const target = Math.floor((end - offset) / elapsed * 8000)
            chunkSize = Math.max(minChunk, Math.min(maxChunk, chunkSize * 2, target))
          }
        }
        retries = 0
        job.received = next
        const now = performance.now()
        if (result.kind === 'conflict' || result.data.duplicate || next < lastOffset) {
          job.speed = null
          lastOffset = next
          lastTime = now
        } else if (now - lastTime >= 600) {
          job.speed = Math.max(0, (next - lastOffset) * 1000 / (now - lastTime))
          lastOffset = next
          lastTime = now
        }
        showTransfer(job, '发送至服务器', next)
      } catch (cause) {
        if (stopped(job)) return
        const error = controller.signal.aborted ? new ApiError('上传请求超时', 0) : cause
        if (!retryable(error) || retries >= 4) throw error
        chunkSize = Math.max(minChunk, Math.floor(chunkSize / 2))
        job.speed = null
        job.eta = null
        job.message = `连接不稳定，正在从 ${formatBytes(offset)} 重试…`
        await wait(500 * 2 ** retries++)
        lastTime = performance.now()
        lastOffset = offset
      } finally {
        window.clearTimeout(timeout)
        job.controller = null
      }
    }
  }

  async function watchSave(job: UploadJob, id: string, needsComplete: boolean): Promise<void> {
    const labels = {
      queued: '等待 Telegram 保存',
      preparing: '准备 Telegram 文件',
      hashing: '计算文件校验值',
      sending: '发送至 Telegram',
      committing: '写入文件记录',
    }
    let lastTime = 0
    let lastBytes = 0
    const showProgress = (record: UploadRecord): void => {
      if (!sameWorkspace(job)) return
      const stage = record.stage ?? (record.progress > 0 ? 'sending' : 'queued')
      const sent = Math.max(0, Math.min(job.file.size, Number(record.progress) || 0))
      const now = performance.now()
      if (job.stage !== stage || sent < lastBytes || !lastTime) {
        job.speed = null
        job.eta = null
        lastTime = now
        lastBytes = sent
      } else if (now - lastTime >= 600) {
        job.speed = Math.max(0, (sent - lastBytes) * 1000 / (now - lastTime))
        lastTime = now
        lastBytes = sent
      }
      job.stage = stage
      if (stage === 'sending') showTransfer(job, labels.sending, sent)
      else {
        job.progress = null
        job.message = labels[stage]
      }
    }

    let nextStreamAt = 0
    let failures = 0
    let waitingForSave = needsComplete
    for (let polls = 0; ; polls += 1) {
      if (stopped(job)) return
      let record: UploadRecord | null = null
      try {
        if (waitingForSave) {
          const result = await api.completeUpload(id)
          if (!result) throw new ApiError('未收到服务器确认的保存状态', 0)
          record = 'state' in result ? result : result.upload ?? null
          waitingForSave = false
        }
        if (!record && Date.now() >= nextStreamAt) {
          record = await new Promise<UploadRecord | null>((resolve) => {
            let idle: number | undefined
            const finish = (update: UploadRecord | null) => {
              window.clearTimeout(idle)
              close?.()
              resolve(update)
            }
            const resetIdle = () => {
              window.clearTimeout(idle)
              idle = window.setTimeout(() => finish(null), 35_000)
            }
            const close = taskEvents<UploadRecord>(
              `/api/uploads/${encodeURIComponent(id)}/events`,
              (update) => {
                if (!sameWorkspace(job) || update.state !== 'processing') finish(update)
                else {
                  showProgress(update)
                  resetIdle()
                }
              },
              () => finish(null),
            )
            if (close) resetIdle()
            else resolve(null)
          })
          nextStreamAt = Date.now() + 12_000
        }
        record ??= await api.getUpload(id)
        if (!record?.state) throw new ApiError('未收到服务器确认的保存状态', 0)
        failures = 0
      } catch (cause) {
        if (!retryable(cause) || ++failures >= 5) throw cause
        job.speed = null
        job.eta = null
        lastTime = 0
        job.message = '连接不稳定，正在重新获取保存进度…'
        await wait(Math.min(5000, 1200 + polls * 200))
        continue
      }
      if (stopped(job)) return
      if (record.state === 'done') return
      if (record.state === 'invalid') {
        forgetUpload(job)
        throw new Error(record.error || '服务器临时文件已失效，请重新上传')
      }
      if (record.state === 'error') throw new Error(record.error || 'Telegram 保存失败')
      if (record.state === 'uploading') waitingForSave = true
      else showProgress(record)
      await wait(Math.min(5000, 1200 + polls * 200))
    }
  }

  async function runJob(job: UploadJob): Promise<void> {
    job.state = 'uploading'
    job.error = ''
    try {
      await verifyFile(job)
      if (stopped(job) || !job.resumeKey) return
      job.message = '连接服务器…'
      job.progress = null
      const source = await ensureUpload(job, job.resumeKey)
      if (stopped(job)) return
      job.received = confirmedOffset(job, Number(source.received))
      if (source.state === 'uploading' || source.state === 'error') {
        if (job.received < job.file.size) await sendChunks(job, source.id, Number(source.chunk_size))
        if (stopped(job)) return
      }
      if (source.state !== 'done') {
        job.state = 'processing'
        job.stage = 'queued'
        job.message = '等待 Telegram 保存…'
        job.progress = null
        job.speed = null
        job.eta = null
        senders.delete(job.key)
        pump()
        await watchSave(job, source.id, source.state !== 'processing')
        if (stopped(job)) return
      }
      job.state = 'done'
      job.stage = null
      job.received = job.file.size
      job.progress = 100
      job.message = '完成'
      saveResume(job, null)
      completedCount.value += 1
      useToastStore().success(`${job.file.name} 上传完成`)
    } catch (cause) {
      if (interrupted(job)) return
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
    job.message = '已暂停，继续时从服务器确认的位置续传'
    job.controller?.abort()
  }

  function resume(job: UploadJob): void {
    if (!['paused', 'error', 'canceled'].includes(job.state)) return
    if (!sameWorkspace(job)) {
      useToastStore().error('存储空间已切换，请返回原空间后继续')
      return
    }
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
    try { await discardUpload(job) }
    catch (cause) {
      job.state = 'error'
      job.canceled = false
      job.error = cause instanceof Error ? cause.message : '取消失败，请重试'
      job.message = job.error
      return
    }
    job.state = 'canceled'
    job.stage = null
    job.error = ''
    job.message = '已取消'
  }

  function pauseAll(): void { jobs.value.forEach(pause) }
  function resumeAll(): void { jobs.value.filter((j) => j.state === 'paused').forEach(resume) }
  function retryAll(): void { jobs.value.filter((j) => j.state === 'error').forEach(resume) }
  function clearFinished(): void {
    jobs.value = jobs.value.filter((j) => j.state !== 'done' && (j.state !== 'canceled' || j.uploadId !== null))
  }
  return { jobs, completedCount, activeCount, sendingCount, processingCount, hasJobs, totalProgress,
    enqueue, enqueueFiles, pause, resume, pauseAll, resumeAll, retryAll, cancel, retry: resume, clearFinished }
})

export { fingerprint as uploadFingerprint }
export { formatBytes, ApiError }
