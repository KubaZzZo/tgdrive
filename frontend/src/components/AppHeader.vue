<script setup lang="ts">
import { ref } from 'vue'

import ChannelSyncPanel from './ChannelSyncPanel.vue'
import TrafficPanel from './TrafficPanel.vue'
import TelegramAccountPanel from './TelegramAccountPanel.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
defineProps<{ module: 'drive' | 'people' | 'images' }>()
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
      <a href="#" :aria-current="module === 'drive' ? 'page' : undefined">网盘</a>
      <a href="#people" :aria-current="module === 'people' ? 'page' : undefined">人物视频</a>
      <a href="#images" :aria-current="module === 'images' ? 'page' : undefined">图床</a>
    </nav>
    <div class="top-actions">
      <TrafficPanel />
      <ChannelSyncPanel />
      <TelegramAccountPanel />
      <button class="ghost" type="button" :disabled="loggingOut" @click="logout">退出</button>
    </div>
  </header>
</template>

<style scoped>
.module-nav { display: flex; gap: 4px; margin-right: auto; margin-left: 24px; }
.module-nav a { border-radius: 8px; padding: 8px 14px; color: var(--muted); white-space: nowrap; text-decoration: none; }
.module-nav a[aria-current] { background: #eff6ff; color: var(--accent); font-weight: 700; }
@media (max-width: 760px) {
  .topbar { flex-wrap: wrap; height: auto; padding: 10px 18px; gap: 8px; }
  .top-actions { flex-basis: 100%; flex-wrap: wrap; column-gap: 6px; justify-content: space-between; }
  .module-nav { margin-left: 0; }
  .module-nav a { padding: 8px 10px; }
}
</style>
