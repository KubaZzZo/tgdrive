<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from 'vue'

import { RouterLink } from 'vue-router'

import AppIcon from '@/components/ui/AppIcon.vue'
import { useAuthStore } from '@/stores/auth'
import { useTrafficStore } from '@/stores/traffic'
import { useSyncStore } from '@/stores/sync'
import { setTheme, theme, type Theme } from '@/stores/theme'

const TelegramAccountPanel = defineAsyncComponent(() => import('@/components/TelegramAccountPanel.vue'))
const auth = useAuthStore()
const sync = useSyncStore()
const traffic = useTrafficStore()
const syncLabel = computed(() => {
  const status = sync.status
  if (status?.bot_state === 'listening') return 'Bot 备用同步'
  if (status?.bot_state === 'connecting') return '备用 Bot 连接中'
  if (status?.bot_state === 'error') return '备用 Bot 异常'
  if (status?.state === 'auth_required') return '同步需登录'
  if (status?.state === 'rate_limited') return '同步限流等待'
  if (sync.busy) return '正在同步'
  return status?.state === 'error' ? '同步异常' : '频道同步'
})
const telegramOpen = ref(false)
const telegramConnected = computed(() => sync.status?.state !== 'auth_required' && auth.storage?.connected)
const loggingOut = ref(false)

async function logout(): Promise<void> {
  if (loggingOut.value) return
  loggingOut.value = true
  try {
    await auth.logout()
  } finally {
    loggingOut.value = false
  }
}
</script>

<template>
  <header class="topbar">
    <div class="brand">
      <span class="brand-mark small">TG</span>
      <span>TG Drive</span>
    </div>
    <nav class="module-nav" aria-label="功能模块">
      <RouterLink :to="{ name: 'drive' }">网盘</RouterLink>
      <RouterLink v-if="auth.features.google_drive" :to="{ name: 'google-drive' }">Google Drive</RouterLink>
      <RouterLink :to="{ name: 'video-import' }">链接导入</RouterLink>
      <RouterLink v-if="auth.features.people" :to="{ name: 'people' }">人物视频</RouterLink>
      <RouterLink v-if="auth.features.images" :to="{ name: 'images' }">图床</RouterLink>
    </nav>
    <div class="top-actions">
      <RouterLink v-if="auth.features.traffic" :to="{ name: 'traffic' }" class="ghost traffic-button" :class="{ 'traffic-alert': traffic.warning }">
        {{ traffic.label }}
      </RouterLink>
      <RouterLink :to="{ name: 'sync' }" class="ghost sync-button">
        <AppIcon name="refresh" :size="16" :class="{ spinning: sync.busy && sync.status?.state !== 'rate_limited' }" />{{ syncLabel }}
      </RouterLink>
      <RouterLink :to="{ name: 'clients' }" class="ghost">客户端与备份</RouterLink>
      <select class="theme-picker" aria-label="显示主题" :value="theme"
        @change="setTheme(($event.target as HTMLSelectElement).value as Theme)">
        <option value="system">跟随系统</option>
        <option value="light">亮色</option>
        <option value="dark">暗色</option>
      </select>
      <button class="ghost telegram-button" :class="{ 'telegram-offline': !telegramConnected }" @click="telegramOpen = true">
        <span class="telegram-status-dot" :class="{ online: telegramConnected }" />
        <span class="telegram-label-desktop">Telegram 账号</span><span class="telegram-label-mobile">账号</span>
      </button>
      <TelegramAccountPanel v-if="telegramOpen" @close="telegramOpen = false" />
      <button class="ghost" type="button" :disabled="loggingOut" @click="logout">退出</button>
    </div>
  </header>
</template>

<style scoped>
.topbar {
  position: sticky;
  top: 0;
  z-index: 40;
  height: 76px;
  padding: 0 32px;
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--panel);
  border-bottom: 1px solid var(--line);
}
.brand {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 19px;
  font-weight: 750;
  letter-spacing: -0.5px;
}
.top-actions {
  display: flex;
  align-items: center;
  gap: 18px;
}
.top-actions .ghost {
  text-decoration: none;
  font-size: 12px;
  min-height: 34px;
  padding: 6px 12px;
}
.theme-picker {
  min-height: 34px;
  padding: 4px 6px;
  border: 1px solid var(--line);
  border-radius: 9px;
  background: var(--panel);
  color: var(--text);
  font-size: 12px;
}
.sync-button {
  gap: 7px;
}
.spinning {
  animation: sync-spin 1.5s linear infinite;
}
@keyframes sync-spin {
  to {
    transform: rotate(360deg);
  }
}
.top-actions .traffic-alert { color: var(--warning); background: var(--warning-soft); }
@media (max-width: 600px) {
  .topbar .brand > span:last-child { display: none; }
  .top-actions .traffic-button { font-size: 11px; padding-inline: 7px; }
}
.telegram-status-dot { width: 7px; height: 7px; border-radius: 50%; background: #ba7840; display: inline-block; }
.telegram-status-dot.online { background: #368d64; }
.telegram-label-mobile { display: none; }
@media (max-width: 600px) {
  .telegram-label-desktop { display: none; }
  .telegram-label-mobile { display: inline; }
}

.module-nav { display: flex; gap: 4px; margin-right: auto; margin-left: 24px; }
.module-nav a { border-radius: 8px; padding: 8px 14px; color: var(--muted); white-space: nowrap; text-decoration: none; }
.module-nav a[aria-current] { background: var(--accent-soft); color: var(--accent); font-weight: 700; }
@media (max-width: 760px) {
  .topbar { flex-wrap: wrap; height: auto; padding: 10px 18px; gap: 8px; }
  .brand { font-size: 17px; gap: 9px; }
  .top-actions { flex-basis: 100%; flex-wrap: wrap; gap: 8px 6px; justify-content: space-between; }
  .module-nav { margin-left: 0; }
  .module-nav a { padding: 8px 10px; }
}
</style>
