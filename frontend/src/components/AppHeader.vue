<script setup lang="ts">
import { computed, ref } from 'vue'

import ChannelSyncPanel from './ChannelSyncPanel.vue'
import TrafficPanel from './TrafficPanel.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const loggingOut = ref(false)

const storageLabel = computed(() => {
  const storage = auth.storage
  if (!storage) return '连接中…'
  if (!storage.connected) return 'Telegram 未连接'
  return storage.mode === 'bot' ? 'Bot 已连接' : '账号已连接'
})

const storageClass = computed(() => {
  const storage = auth.storage
  if (!storage) return ''
  return storage.connected ? 'online' : 'offline'
})

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
    <div class="top-actions">
      <TrafficPanel />
      <ChannelSyncPanel />
      <span class="pill" :class="storageClass">{{ storageLabel }}</span>
      <button class="ghost" type="button" :disabled="loggingOut" @click="logout">退出</button>
    </div>
  </header>
</template>
