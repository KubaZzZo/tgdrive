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
        <strong>上传队列</strong>
        <span v-if="uploads.activeCount" class="upload-active">{{ uploads.activeCount }} 个进行中</span>
      </div>
      <div class="upload-head-actions">
        <button
          class="icon-btn"
          type="button"
          :disabled="uploads.activeCount > 0"
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
