<script setup lang="ts">
import { ref } from 'vue'

import { useUploadStore } from '@/stores/uploads'
import { formatBytes } from '@/utils/format'

const uploads = useUploadStore()
const collapsed = ref(false)

function stateClass(state: string): string {
  if (state === 'done') return 'done'
  if (state === 'error') return 'error'
  if (state === 'canceled') return 'canceled'
  return ''
}
</script>

<template>
  <aside v-if="uploads.hasJobs" class="upload-panel" :class="{ collapsed }">
    <header class="upload-head">
      <div>
        <strong>上传队列 · {{ Math.round(uploads.totalProgress) }}%</strong>
        <span v-if="uploads.activeCount" class="upload-active">{{ uploads.activeCount }} 个进行中</span>
      </div>
      <div class="upload-head-actions">
        <button class="icon-btn" type="button" @click="uploads.pauseAll">全部暂停</button>
        <button class="icon-btn" type="button" @click="uploads.resumeAll">全部继续</button>
        <button class="icon-btn" type="button" @click="uploads.retryAll">重试失败</button>
        <button
          class="icon-btn"
          type="button"
          @click="uploads.clearFinished"
        >
          清空已完成
        </button>
        <button class="icon-btn" type="button" @click="collapsed = !collapsed">
          {{ collapsed ? '展开' : '收起' }}
        </button>
      </div>
    </header>

    <div v-if="!collapsed" class="upload-list">
      <div v-for="job in uploads.jobs" :key="job.key" class="upload-row">
        <div class="upload-name">
          <span :title="job.file.name">{{ job.file.name }}</span>
          <span class="pct">{{ Math.round(job.progress) }}%</span>
        </div>
        <div class="bar">
          <div :style="{ width: `${Math.max(0, Math.min(100, job.progress))}%` }" />
        </div>
        <div class="upload-foot">
          <span class="upload-status" :class="stateClass(job.state)">{{ job.message }}</span>
          <span class="upload-size">{{ formatBytes(job.received) }} / {{ formatBytes(job.file.size) }}</span>
          <button
            v-if="job.state === 'queued' || job.state === 'uploading'"
            class="text-btn" type="button" @click="uploads.pause(job)"
          >暂停</button>
          <button
            v-if="job.state === 'paused'"
            class="text-btn" type="button" @click="uploads.resume(job)"
          >继续</button>
          <button
            v-if="job.state === 'queued' || job.state === 'uploading' || job.state === 'paused'"
            class="text-btn danger-text"
            type="button"
            @click="uploads.cancel(job)"
          >
            取消
          </button>
          <button
            v-else-if="job.state === 'error' || job.state === 'canceled'"
            class="text-btn"
            type="button"
            @click="uploads.retry(job)"
          >
            重试
          </button>
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
.upload-head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 12px;
}
.upload-head strong {
  font-size: 13px;
}
.upload-active {
  color: var(--accent);
  font-size: 10px;
  margin-left: 8px;
}
.upload-head-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
}
.upload-list {
  margin-top: 8px;
}
.upload-row {
  border-top: 1px solid var(--line);
  padding: 11px 0;
}
.upload-name {
  display: flex;
  justify-content: space-between;
  gap: 10px;
  font-size: 12px;
  margin-bottom: 8px;
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
  height: 5px;
  background: var(--panel-2);
  border-radius: 5px;
  overflow: hidden;
}
.bar > div {
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width 0.2s;
}
.upload-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 7px;
}
.upload-status {
  flex: 1;
  font-size: 10px;
  color: var(--muted);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.upload-status.error {
  color: var(--danger);
}
.upload-status.done {
  color: var(--ok);
}
.upload-size {
  font-size: 10px;
  color: var(--muted);
  white-space: nowrap;
}
@media (max-width: 760px) {
  .upload-panel {
    bottom: max(10px, env(safe-area-inset-bottom));
    right: 10px;
    width: calc(100% - 20px);
    max-height: 43dvh;
  }
  .upload-head-actions .icon-btn,
  .upload-foot .text-btn {
    min-height: 40px;
    padding: 7px;
  }
  .upload-size {
    display: none;
  }
}
</style>
