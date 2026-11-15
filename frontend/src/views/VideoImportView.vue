<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'

import { api, type VideoImportStatus } from '@/api/client'
import { formatBytes } from '@/utils/format'

const links = ref('')
const status = ref<VideoImportStatus>()
const busy = ref(false)
const error = ref('')
const refreshError = ref('')
let timer: ReturnType<typeof setInterval> | undefined

const labels: Record<string, string> = {
  queued: '等待下载',
  downloading: '下载中',
  uploading: '正在上传到 Telegram',
  error: '处理失败',
  done: '已入库并清理',
  cancelled: '已取消并清理',
}

async function update(
  action: () => Promise<VideoImportStatus>,
  actionError = error,
): Promise<boolean> {
  if (busy.value) return false
  busy.value = true
  try {
    status.value = await action()
    actionError.value = ''
    refreshError.value = ''
    return true
  } catch (cause) {
    actionError.value = cause instanceof Error ? cause.message : '请求失败'
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

function refresh(): void {
  void update(api.videoImport, refreshError)
}

onMounted(() => {
  refresh()
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
    <ul v-if="status?.jobs.length">
      <li v-for="(job, index) in status.jobs" :key="job.id">
        <strong>{{ job.name || `视频 ${index + 1}` }}</strong>
        <span>{{ labels[job.state] || job.state }} · 已下载 {{ formatBytes(job.downloaded) }}</span>
        <span v-if="job.uploaded !== undefined"> · 已上传 {{ formatBytes(job.uploaded) }}</span>
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
.actions { display: flex; flex-wrap: wrap; gap: 8px; }
</style>
