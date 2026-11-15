<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api, type VideoImportStatus } from '@/api/client'
import { formatBytes } from '@/utils/format'

const links = ref('')
const status = ref<VideoImportStatus>()
const busy = ref(false)
const error = ref('')
const refreshError = ref('')
let timer: ReturnType<typeof setInterval> | undefined
let refreshing = false
let actionVersion = 0

const labels: Record<string, string> = {
  queued: '等待下载',
  downloading: '下载中',
  uploading: '正在上传到 Telegram',
  error: '处理失败',
  done: '已入库并清理',
  cancelled: '已取消并清理',
}

const jobs = computed(() => status.value?.jobs.map(job => {
  const uploading = job.state === 'uploading'
  const transferred = uploading ? job.uploaded : job.downloaded
  let percent: number | undefined
  if (job.state === 'done') {
    percent = 100
  } else if (job.size && transferred !== undefined) {
    percent = transferred >= job.size ? 100 : Math.min(99.9, Math.round(transferred / job.size * 1000) / 10)
  }
  return { ...job, uploading, percent }
}) ?? [])

async function update(action: () => Promise<VideoImportStatus>): Promise<boolean> {
  if (busy.value) return false
  busy.value = true
  actionVersion += 1
  try {
    status.value = await action()
    error.value = ''
    refreshError.value = ''
    return true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '请求失败'
    return false
  } finally {
    busy.value = false
  }
}

async function submit(): Promise<void> {
  const urls = links.value.split(/\r?\n/).map(value => value.trim()).filter(Boolean)
  if (urls.length < 1 || urls.length > 2) {
    error.value = '请输入 1 至 2 条链接，每行一条'
    return
  }
  if (await update(() => api.startVideoImport(urls))) links.value = ''
}

async function refresh(): Promise<void> {
  if (refreshing || busy.value) return
  refreshing = true
  const version = actionVersion
  try {
    const latest = await api.videoImport()
    if (version !== actionVersion) return
    status.value = latest
    refreshError.value = ''
  } catch (cause) {
    if (version === actionVersion) {
      refreshError.value = cause instanceof Error ? cause.message : '读取导入状态失败'
    }
  } finally {
    refreshing = false
  }
}

onMounted(() => {
  void refresh()
  timer = setInterval(refresh, 2500)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <section class="import-view" aria-labelledby="import-title">
    <h1 id="import-title">视频链接导入</h1>
    <p class="muted">每批最多两个链接。服务器会下载视频、上传到当前 Telegram 频道，并清理本批临时文件。导入的视频出现在网盘根目录。</p>
    <form @submit.prevent="submit">
      <label for="video-links">HTTP(S) 视频直链或 eD2k 文件链接，每行一条</label>
      <textarea id="video-links" v-model="links" rows="3" maxlength="8194" :disabled="status?.active" required />
      <button class="primary" type="submit" :disabled="busy || !status || status.active">开始导入</button>
    </form>
    <p v-if="error || refreshError" class="form-error" role="alert">{{ error || refreshError }}</p>
    <p v-if="status?.active" role="status">本批清理完成后可开始下一批。</p>
    <ul v-if="jobs.length">
      <li v-for="(job, index) in jobs" :key="job.id">
        <strong>{{ job.name || `视频 ${index + 1}` }}</strong>
        <span>
          {{ job.state === 'downloading' && job.ed2k_sources === 0
            ? '正在查找下载源'
            : labels[job.state] || job.state }}
          · 已下载{{ job.state === 'downloading' && job.ed2k_sources !== undefined && job.downloaded ? '约' : '' }}
          {{ formatBytes(job.downloaded) }}
          <template v-if="job.size !== undefined"> / {{ formatBytes(job.size) }}</template>
          <template v-if="job.state === 'downloading' && job.ed2k_sources !== undefined">
            · {{ job.ed2k_sources }} 个来源
          </template>
        </span>
        <span v-if="job.uploading && job.uploaded !== undefined">
          已上传 {{ formatBytes(job.uploaded) }} / {{ formatBytes(job.size) }}
        </span>
        <div
          v-if="job.state === 'downloading' || job.uploading || job.percent !== undefined && ['queued', 'done'].includes(job.state)"
          class="job-progress"
        >
          <progress
            :value.attr="job.percent"
            max="100"
            :aria-label="`${job.name || `视频 ${index + 1}`} ${job.uploading ? 'Telegram 上传进度' : job.state === 'done' ? '导入进度' : '下载进度'}`"
          />
          <span>{{ job.percent === undefined ? (job.uploading ? '等待上传进度' : '总大小未知') : `${job.percent}%` }}</span>
        </div>
        <p v-if="job.error" class="form-error">{{ job.error }}</p>
      </li>
    </ul>
    <div v-if="status?.active" class="actions">
      <button v-if="!status.running" class="ghost" :disabled="busy" @click="update(api.retryVideoImport)">重试未完成任务</button>
      <button class="ghost" :disabled="busy" @click="update(api.cancelVideoImport)">取消并清理</button>
    </div>
  </section>
</template>

<style scoped>
.import-view {
  max-width: 760px;
  margin: 24px auto;
  padding: 24px;
  border: 1px solid var(--line);
  border-radius: 16px;
  background: white;
}
form, li { display: grid; gap: 8px; }
form { justify-items: start; }
textarea {
  width: 100%;
  resize: vertical;
  padding: 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  font: inherit;
}
ul { padding-left: 0; list-style: none; }
li { padding: 12px 0; border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.job-progress { display: flex; align-items: center; gap: 8px; font-variant-numeric: tabular-nums; }
.job-progress progress { flex: 1; min-width: 0; height: 12px; accent-color: var(--accent); }
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
