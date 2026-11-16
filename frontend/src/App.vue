<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, watch } from 'vue'
import { RouterView, useRoute, useRouter } from 'vue-router'

import AppHeader from '@/components/AppHeader.vue'
import StorageWarning from '@/components/StorageWarning.vue'
import ToastHost from '@/components/ui/ToastHost.vue'
import { loginDestination } from '@/router'
import { useAuthStore } from '@/stores/auth'
import { useDriveStore } from '@/stores/drive'
import { useSyncStore } from '@/stores/sync'
import { useToastStore } from '@/stores/toast'
import { useTrafficStore } from '@/stores/traffic'
import { useUploadStore } from '@/stores/uploads'

const UploadPanel = defineAsyncComponent(() => import('@/components/UploadPanel.vue'))
const route = useRoute()
const router = useRouter()
const sync = useSyncStore()
const auth = useAuthStore()
const drive = useDriveStore()
const traffic = useTrafficStore()
const uploads = useUploadStore()
const toast = useToastStore()
let session = 0

watch(() => auth.authenticated, async (authenticated) => {
  const current = ++session
  if (!authenticated) {
    sync.stop()
    traffic.stop()
    drive.cancelLoad()
    if (route.name && route.name !== 'login') {
      await router.replace({ name: 'login', query: { redirect: route.fullPath } })
    }
    return
  }
  if (route.name === 'login') await router.replace(loginDestination(route.query.redirect))
  if (session !== current) return
  sync.start()
  if (auth.features.traffic) traffic.start()
})

watch(() => uploads.completedCount, () => {
  if (auth.authenticated && (route.name === 'drive' || route.name === 'people')) void drive.load()
})

function onUnauthorized(): void {
  if (!auth.authenticated) return
  auth.markUnauthorized()
  toast.error('登录状态已失效，请重新登录')
}

window.addEventListener('tgdrive:unauthorized', onUnauthorized)
onBeforeUnmount(() => {
  session++
  sync.stop()
  traffic.stop()
  drive.cancelLoad()
  window.removeEventListener('tgdrive:unauthorized', onUnauthorized)
})
</script>

<template>
  <RouterView v-if="route.name === 'login'" />
  <div v-else-if="auth.authenticated && route.name" class="app-shell">
    <AppHeader />
    <StorageWarning :storage="auth.storage" />
    <main class="main" :class="{ 'has-transfers': uploads.hasJobs }">
      <RouterView />
    </main>
    <UploadPanel v-if="uploads.hasJobs" />
  </div>
  <div v-else class="boot-screen">
    <div class="brand-mark">TG</div>
    <p>正在连接 TG Drive…</p>
  </div>
  <ToastHost />
</template>

<style scoped>
.main {
  margin: 0 auto;
  max-width: 1680px;
  padding: 0 28px 60px 0;
}
.main.has-transfers {
  padding-bottom: 340px;
}
.boot-screen {
  min-height: 100dvh;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 18px;
  color: var(--muted);
}
@media (min-width: 1440px) {
  .main {
    padding-right: 44px;
  }
}
@media (max-width: 760px) {
  .main {
    padding: 0 16px 36px;
  }
  .main.has-transfers {
    padding-bottom: 340px;
  }
}
</style>
