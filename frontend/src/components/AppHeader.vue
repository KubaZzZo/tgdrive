<script setup lang="ts">
import { ref } from 'vue'

import ChannelSyncPanel from './ChannelSyncPanel.vue'
import TrafficPanel from './TrafficPanel.vue'
import TelegramAccountPanel from './TelegramAccountPanel.vue'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
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
    <div class="top-actions">
      <TrafficPanel />
      <ChannelSyncPanel />
      <TelegramAccountPanel />
      <button class="ghost" type="button" :disabled="loggingOut" @click="logout">退出</button>
    </div>
  </header>
</template>
