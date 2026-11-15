<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api, type VideoImportPreviewRow, type VideoImportStatus } from '@/api/client'
import { formatBytes } from '@/utils/format'
import { splitVideoImportLinks } from '@/utils/video-import-links'

const links = ref('')
const previewRows = ref<(VideoImportPreviewRow & { id: number; link: string })[]>([])
const selected = ref<number[]>([])
const previewing = ref(false)
const previewError = ref('')
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

const protocols = { http: 'HTTP(S)', ed2k: 'eD2k', magnet: '磁力', unknown: '未知类型' }

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

async function preview(): Promise<void> {
  if (previewing.value) return
  const urls = splitVideoImportLinks(links.value)
  if (urls.length < 1 || urls.length > 100) {
    previewError.value = '请输入 1 至 100 条链接，每行一条'
    return
  }
  previewing.value = true
  try {
    const { rows } = await api.previewVideoImport(urls)
    previewRows.value = rows.map((row, index) => ({ ...row, link: urls[index], id: index + 1 }))
    previewError.value = ''
  } catch (cause) {
    previewError.value = cause instanceof Error ? cause.message : '预览链接失败'
  } finally {
    previewing.value = false
  }
}

function editLinks(): void {
  links.value = previewRows.value.map(row => row.link).join('\n')
  previewRows.value = []
  selected.value = []
  error.value = ''
}

async function submit(): Promise<void> {
  if (busy.value || !status.value || status.value.active) return
  const chosen = previewRows.value.filter(row => !row.error && selected.value.includes(row.id))
  if (chosen.length < 1 || chosen.length > 2) return
  if (await update(() => api.startVideoImport(chosen.map(row => row.link)))) {
    const submitted = new Set(chosen.map(row => row.id))
    previewRows.value = previewRows.value.filter(row => !submitted.has(row.id))
    selected.value = []
    if (!previewRows.value.length) links.value = ''
  }
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
    <form v-if="!previewRows.length" @submit.prevent="preview">
      <label for="video-links">粘贴 HTTP(S) 视频直链、eD2k 或磁力链接，每行一条，最多 100 条</label>
      <textarea id="video-links" v-model="links" rows="4" maxlength="409800" :readonly="previewing" required />
      <button class="primary" type="submit" :disabled="previewing">{{ previewing ? '正在识别…' : '预览链接' }}</button>
    </form>
    <p v-if="previewError" class="form-error" role="alert">{{ previewError }}</p>
    <form v-if="previewRows.length" @submit.prevent="submit">
      <fieldset class="link-preview">
        <legend>选择本批下载的链接（已选 {{ selected.length }}/2）</legend>
        <label v-for="row in previewRows" :key="row.id" class="preview-row">
          <input
            v-if="!row.error"
            v-model="selected"
            type="checkbox"
            :value="row.id"
            :disabled="busy || (selected.length >= 2 && !selected.includes(row.id))"
          />
          <span>
            <strong>{{ row.name || `链接 ${row.id}` }}</strong>
            <small>{{ protocols[row.protocol] }}<template v-if="row.size !== undefined"> · {{ formatBytes(row.size) }}</template></small>
            <small v-if="row.error" class="form-error">{{ row.error }}</small>
            <small v-else-if="row.protocol === 'http'">文件名和大小在下载时确认</small>
          </span>
        </label>
      </fieldset>
      <div class="actions">
        <button class="ghost" type="button" :disabled="busy" @click="editLinks">编辑链接</button>
        <button class="primary" type="submit" :disabled="busy || !status || status.active || !selected.length">开始导入</button>
      </div>
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
          <template v-if="job.state === 'downloading' && job.torrent_seeds !== undefined">
            · {{ job.torrent_seeds }} 个做种者
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
.link-preview { width: 100%; min-width: 0; margin: 0; padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px; }
.preview-row { display: flex; align-items: flex-start; gap: 8px; padding: 8px 0; overflow-wrap: anywhere; }
.preview-row + .preview-row { border-top: 1px solid var(--line); }
.preview-row span { display: grid; gap: 4px; min-width: 0; }
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
