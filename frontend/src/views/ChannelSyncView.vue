<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import TelegramDiagnostics from '@/components/TelegramDiagnostics.vue'
import { useSyncStore } from '@/stores/sync'
import { formatDate } from '@/utils/format'
const sync = useSyncStore()
const secondsLeft = ref(0)
const authRequired = computed(() => sync.status?.state === 'auth_required')
const botState = computed(() => sync.status?.bot_state)
const rateLimited = computed(() => sync.status?.state === 'rate_limited')
const progress = computed(() => {
  if (sync.status?.state === 'idle' && sync.status.running) return null
  return sync.status?.progress ?? null
})
const unavailable = computed(
  () =>
    !sync.status?.enabled ||
    Boolean(botState.value) ||
    authRequired.value ||
    ['unavailable', 'stopped', 'disabled'].includes(sync.status?.state ?? 'unavailable'),
)
const summary = computed(() => {
  if (botState.value === 'listening') return '备用 Bot 正在监听新消息'
  if (botState.value === 'connecting') return '正在连接备用 Bot'
  if (botState.value === 'error') return '备用 Bot 暂不可用'
  if (rateLimited.value) return '等待 Telegram 限流解除'
  if (sync.busy) return progress.value === null ? '正在准备扫描' : '正在检查频道'
  if (unavailable.value) return '同步尚未就绪'
  if (sync.status?.state === 'error') return '扫描中断'
  return sync.status?.last_success ? '本轮同步完成' : '等待同步'
})
watch(() => sync.status, (status, _previous, onCleanup) => {
  const seconds = status?.retry_after ?? 0
  secondsLeft.value = seconds
  if (seconds <= 0) return
  const deadline = performance.now() + seconds * 1000
  const timer = setInterval(() => {
    secondsLeft.value = Math.max(0, Math.ceil((deadline - performance.now()) / 1000))
  }, 1000)
  onCleanup(() => clearInterval(timer))
}, { immediate: true })
onMounted(() => { void sync.refresh() })
</script>
<template>
  <section class="sync-details panel-view" aria-labelledby="sync-title">
    <h1 id="sync-title">频道同步</h1>
      <p>
        直接用手机或电脑 Telegram 将照片、视频、音频和文件发送到存储频道，网盘会自动收录并分类。
      </p>
      <div class="sync-summary">
        <strong>{{ sync.status?.files ?? 0 }}</strong
        ><span>个频道文件已收录</span
        ><span class="sync-state">{{ summary }}</span>
        <div v-if="!botState && (sync.busy || progress !== null)" class="sync-progress">
          <progress :value="progress ?? undefined" max="100" aria-label="频道扫描进度" />
          <span>{{ progress === null ? '扫描范围待确认' : progress === 100 ? '100%' : `约 ${progress}%` }}</span>
          <span>已检查 {{ sync.status?.scanned ?? 0 }} 条消息</span>
        </div>
      </div>
      <p v-if="rateLimited" role="status">
        {{ secondsLeft > 0 ? `约 ${secondsLeft} 秒后自动继续。` : '正在等待恢复请求。' }}
      </p>
      <p v-if="sync.status?.last_success" class="muted">
        最近完成：{{ formatDate(sync.status.last_success) }}
      </p>
      <p v-if="sync.error || sync.status?.bot_error || (!botState && sync.status?.error)" class="form-error" role="alert">
        {{ sync.error || sync.status?.bot_error || sync.status?.error }}
      </p>
      <p v-if="botState" class="muted">
        备用 Bot 自动收录新频道消息。历史扫描、网盘上传和未取得 Bot 下载权限的文件需要个人会话。
      </p>
      <p v-else-if="unavailable && !authRequired" class="muted">请确认 Telegram 已连接，且频道同步功能已启用。</p>
      <p v-if="!botState" class="muted">
        新消息实时收录，断线后自动补同步。重新扫描历史会补齐遗漏，并更新频道中已删除或更换的文件；网盘自己上传的分段不会重复导入。
      </p>
      <div class="sync-actions">
        <button class="primary" :disabled="unavailable || sync.busy" @click="sync.run(false)">
          立即同步</button
        ><button class="ghost" :disabled="unavailable || sync.busy" @click="sync.run(true)">
          重新扫描历史
        </button>
      </div>
      <TelegramDiagnostics />
  </section>
</template>

<style scoped>
.panel-view {
  margin: 24px auto 0;
  max-width: 900px;
  padding: clamp(16px, 4vw, 32px);
  border: 1px solid var(--line);
  border-radius: 16px;
  background: var(--panel);
}
.panel-view h1 { margin: 0; }

.sync-details p {
  font-size: 13px;
  line-height: 1.9;
  color: var(--muted);
}
.sync-summary {
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  padding: 18px;
  margin: 20px 0;
  background: var(--accent-soft);
  border-radius: 10px;
  font-size: 12px;
  color: var(--muted);
}
.sync-summary strong {
  color: var(--accent);
  font-size: 28px;
}
.sync-state {
  margin-left: auto;
  color: var(--accent);
}
.sync-progress {
  flex: 1 0 100%;
  display: flex;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 6px;
}
.sync-progress progress {
  width: 100%;
  height: 12px;
  accent-color: var(--accent);
}
.sync-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 22px;
}
</style>
