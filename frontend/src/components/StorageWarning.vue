<script setup lang="ts">
import type { StorageStatus } from '@/api/types'
import { useSyncStore } from '@/stores/sync'

defineProps<{ storage: StorageStatus | null }>()
const sync = useSyncStore()
</script>

<template>
  <div v-if="sync.status?.bot_state === 'listening'" class="warning" role="status">
    <strong>备用 Bot 正在收录新消息。</strong>
    历史扫描和网盘上传需要个人 Telegram 会话。
  </div>
  <div v-else-if="storage && !storage.connected" class="warning" role="alert">
    <strong>Telegram 尚未连接：</strong>
    {{ storage.error || '请检查 .env 配置并重启服务。' }}
  </div>
</template>

<style scoped>
.warning {
  margin: 16px 28px 0;
  padding: 12px 16px;
  color: var(--warning);
  background: var(--warning-soft);
  border: 1px solid var(--warning-border);
  border-radius: 10px;
}
.warning strong {
  display: block;
}
.warning p {
  margin: 4px 0 0;
}
@media (max-width: 760px) {
  .warning {
    margin: 12px 16px;
  }
}
</style>
