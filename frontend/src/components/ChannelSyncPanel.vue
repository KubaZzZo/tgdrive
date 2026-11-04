<script setup lang="ts">
import { computed, ref } from 'vue'
import { useSyncStore } from '@/stores/sync'
import { formatDate } from '@/utils/format'
import AppIcon from './AppIcon.vue'
import BaseModal from './BaseModal.vue'
const sync = useSyncStore()
const open = ref(false)
const label = computed(() =>
  sync.busy ? '正在同步' : sync.status?.state === 'error' ? '同步异常' : '频道同步',
)
const unavailable = computed(
  () =>
    !sync.status?.enabled ||
    ['unavailable', 'stopped', 'disabled'].includes(sync.status?.state ?? 'unavailable'),
)
function show(): void {
  open.value = true
  void sync.refresh()
}
</script>
<template>
  <button class="ghost sync-button" type="button" @click="show">
    <AppIcon name="refresh" :size="16" :class="{ spinning: sync.busy }" />{{ label }}
  </button>
  <BaseModal :open="open" title="频道同步" @close="open = false">
    <div class="sync-details">
      <p>
        直接用手机或电脑 Telegram 将照片、视频、音频和文件发送到存储频道，网盘会自动收录并分类。
      </p>
      <div class="sync-summary">
        <strong>{{ sync.status?.files ?? 0 }}</strong
        ><span>个频道文件已收录</span
        ><span class="sync-state">{{
          sync.busy
            ? `正在检查 · ${sync.status?.scanned ?? 0} 条消息`
            : unavailable
              ? '同步尚未就绪'
              : '自动同步已开启'
        }}</span>
      </div>
      <p v-if="sync.status?.last_success" class="muted">
        最近完成：{{ formatDate(sync.status.last_success) }}
      </p>
      <p v-if="sync.error || sync.status?.error" class="form-error" role="alert">
        {{ sync.error || sync.status?.error }}
      </p>
      <p v-if="unavailable" class="muted">请确认 Telegram 已连接，且频道同步功能已启用。</p>
      <p class="muted">
        新消息实时收录，断线后自动补同步。重新扫描历史会补齐遗漏，并更新频道中已删除或更换的文件；网盘自己上传的分段不会重复导入。
      </p>
      <div class="sync-actions">
        <button class="primary" :disabled="unavailable || sync.busy" @click="sync.run(false)">
          立即同步</button
        ><button class="ghost" :disabled="unavailable || sync.busy" @click="sync.run(true)">
          重新扫描历史
        </button>
      </div>
    </div>
  </BaseModal>
</template>
