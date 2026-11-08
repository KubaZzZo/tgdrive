<script setup lang="ts">
import { ref } from 'vue'

import ChannelSyncPanel from './ChannelSyncPanel.vue'
import TrafficPanel from './TrafficPanel.vue'
import TelegramAccountPanel from './TelegramAccountPanel.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
defineProps<{ module: 'drive' | 'images' }>()
const emit = defineEmits<{ navigate: [module: 'drive' | 'images'] }>()
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
      <button type="button" :aria-current="module === 'drive' ? 'page' : undefined" @click="emit('navigate', 'drive')">网盘</button>
      <button type="button" :aria-current="module === 'images' ? 'page' : undefined" @click="emit('navigate', 'images')">图床</button>
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
.module-nav button { border: 0; background: transparent; border-radius: 8px; padding: 8px 14px; color: var(--muted); white-space: nowrap; }
.module-nav button[aria-current] { background: #eff6ff; color: var(--accent); font-weight: 700; }
@media (max-width: 760px) {
  .topbar { flex-wrap: wrap; height: auto; padding: 10px 18px; gap: 8px; }
  .top-actions { flex-basis: 100%; justify-content: space-between; }
  .module-nav { margin-left: 12px; }
  .module-nav button { padding: 8px 14px; }
}
</style>
