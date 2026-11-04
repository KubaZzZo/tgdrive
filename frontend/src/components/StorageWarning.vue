<script setup lang="ts">
import type { StorageStatus } from '@/api/types'

defineProps<{ storage: StorageStatus | null }>()
</script>

<template>
  <div v-if="storage && !storage.connected" class="warning" role="alert">
    <strong>Telegram 尚未连接：</strong>
    {{ storage.error || '请检查 .env 配置并重启服务。' }}
  </div>
</template>
