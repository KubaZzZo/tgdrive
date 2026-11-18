<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { useUploadStore, type UploadJob } from '@/stores/uploads'
import AppIcon from '@/components/ui/AppIcon.vue'

const uploads = useUploadStore()
const mobileViewport = window.matchMedia('(max-width: 760px)')
const collapsed = ref(mobileViewport.matches)

function resetPanel(event: MediaQueryListEvent): void {
  collapsed.value = event.matches
}

onMounted(() => mobileViewport.addEventListener('change', resetPanel))
onBeforeUnmount(() => mobileViewport.removeEventListener('change', resetPanel))

function canPause(job: UploadJob): boolean {
  return job.state === 'queued' || job.state === 'uploading'
}

function canCancel(job: UploadJob): boolean {
  return canPause(job) || job.state === 'paused' || (job.state === 'error' && job.uploadId !== null)
}

const failedCount = computed(() => uploads.jobs.filter((job) => job.state === 'error').length)
const hasPausable = computed(() => uploads.jobs.some(canPause))
const hasPaused = computed(() => uploads.jobs.some((job) => job.state === 'paused'))
const hasFinished = computed(() => uploads.jobs.some((job) =>
  job.state === 'done' || (job.state === 'canceled' && job.uploadId === null),
))
const processingCount = computed(() => uploads.jobs.filter((job) => job.state === 'processing').length)

</script>

<template>
  <aside v-if="uploads.hasJobs" class="upload-panel" aria-label="上传队列">
    <button class="upload-summary" type="button" :aria-expanded="!collapsed" aria-controls="upload-jobs"
      @click="collapsed = !collapsed">
      <span class="upload-summary-copy">
        <strong>上传队列 · {{ uploads.jobs.length }} 项</strong>
        <span class="upload-counts">
          <span v-if="uploads.activeCount">{{ uploads.activeCount }} 项进行中</span>
          <span v-if="hasPaused">有任务暂停</span>
          <span v-if="failedCount" class="error-text">{{ failedCount }} 项失败</span>
        </span>
      </span>
      <span class="upload-toggle">{{ collapsed ? '展开' : '收起' }}
        <AppIcon name="chevron" :size="14" />
      </span>
    </button>

    <div v-show="!collapsed" id="upload-jobs" class="upload-list">
      <div class="upload-overview">
        <span>浏览器传输 {{ Math.round(uploads.totalProgress) }}%</span>
        <span v-if="processingCount">{{ processingCount }} 项服务器处理中</span>
      </div>
      <div v-if="hasPausable || hasPaused || failedCount || hasFinished" class="upload-head-actions">
        <button v-if="hasPausable" class="icon-btn" type="button" @click="uploads.pauseAll">全部暂停</button>
        <button v-if="hasPaused" class="icon-btn" type="button" @click="uploads.resumeAll">全部继续</button>
        <button v-if="failedCount" class="icon-btn" type="button" @click="uploads.retryAll">重试失败</button>
        <button v-if="hasFinished" class="icon-btn" type="button" @click="uploads.clearFinished">清空已结束</button>
      </div>

      <div v-for="job in uploads.jobs" :key="job.key" class="upload-row">
        <div class="upload-name">
          <span :title="job.file.name">{{ job.file.name }}</span>
          <span v-if="job.progress !== null && (job.state === 'uploading' || job.state === 'processing')" class="pct"
            aria-hidden="true">{{ Math.round(job.progress) }}%</span>
        </div>
        <progress v-if="job.state === 'uploading' || job.state === 'processing'" class="bar"
          :value="job.progress ?? undefined" max="100" :aria-label="job.message" />
        <div class="upload-foot">
          <details v-if="job.state === 'error'" class="upload-error">
            <summary>失败详情</summary>
            <p>{{ job.error || job.message }}</p>
          </details>
          <span v-else class="upload-status" :class="{ done: job.state === 'done' }">{{ job.message }}</span>
          <div v-if="canCancel(job) || job.state === 'error' || job.state === 'canceled'" class="upload-row-actions">
            <button v-if="canPause(job)" class="text-btn" type="button" @click="uploads.pause(job)">暂停</button>
            <button v-if="job.state === 'paused'" class="text-btn" type="button" @click="uploads.resume(job)">继续</button>
            <button v-if="canCancel(job)" class="text-btn danger-text"
              type="button" @click="uploads.cancel(job)">{{ job.state === 'error' ? '取消并清理' : '取消' }}</button>
            <button v-if="job.state === 'error' || job.state === 'canceled'" class="text-btn"
              type="button" @click="uploads.retry(job)">重试</button>
          </div>
        </div>
      </div>
    </div>
  </aside>
</template>

<style scoped>
.upload-panel {
  position: fixed;
  right: 22px;
  bottom: 22px;
  width: min(430px, calc(100% - 32px));
  max-height: 45dvh;
  overflow: auto;
  z-index: 30;
  padding: 15px;
  border: 1px solid var(--line);
  border-radius: 13px;
  background: var(--panel);
  box-shadow: 0 10px 50px #20324d24;
}
.upload-summary {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
  width: 100%;
  padding: 0;
  text-align: left;
  color: var(--text);
  border: 0;
  background: var(--panel);
}
.upload-summary-copy, .upload-counts, .upload-toggle {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.upload-summary strong { font-size: 13px; }
.upload-counts, .upload-toggle, .upload-overview {
  font-size: 12px;
  color: var(--muted);
}
.upload-toggle { flex-shrink: 0; }
.upload-toggle .app-icon { transform: rotate(90deg); }
.upload-summary[aria-expanded="true"] .app-icon { transform: rotate(-90deg); }
.upload-list {
  margin-top: 8px;
}
.upload-overview, .upload-head-actions, .upload-row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
.upload-overview { padding: 5px 0 7px; }
.upload-head-actions { margin-bottom: 7px; }
.upload-row {
  border-top: 1px solid var(--line);
  padding: 11px 0;
}
.upload-name {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  margin-bottom: 6px;
}
.upload-name span:first-child {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.pct {
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.bar {
  display: block;
  width: 100%;
  height: 6px;
  accent-color: var(--accent);
}
.upload-foot {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  margin-top: 6px;
}
.upload-status, .upload-error {
  flex: 1;
  min-width: 0;
  font-size: 12px;
  color: var(--muted);
  overflow-wrap: anywhere;
}
.upload-status.done { color: var(--ok); }
.error-text, .upload-error { color: var(--danger); }
.upload-error summary { cursor: pointer; }
.upload-error p {
  margin: 4px 0 0;
  white-space: pre-wrap;
}
.upload-row-actions { flex-shrink: 0; }
@media (max-width: 760px) {
  .upload-panel {
    bottom: max(10px, env(safe-area-inset-bottom));
    right: 10px;
    width: calc(100% - 20px);
    max-height: 43dvh;
    padding: 7px 12px;
  }
  .upload-summary, .upload-head-actions .icon-btn,
  .upload-foot .text-btn, .upload-error summary {
    min-height: 40px;
  }
  .upload-status, .upload-error summary { padding-top: 9px; }
  .upload-error summary { padding-bottom: 9px; }
}
</style>
