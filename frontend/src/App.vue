<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'

import AppHeader from '@/components/AppHeader.vue'
import FileBrowser from '@/components/FileBrowser.vue'
import ImageHost from '@/components/ImageHost.vue'
import LoginView from '@/components/LoginView.vue'
import StorageWarning from '@/components/StorageWarning.vue'
import ToastHost from '@/components/ToastHost.vue'
import UploadPanel from '@/components/UploadPanel.vue'
import { useSyncStore } from '@/stores/sync'
import { useAuthStore } from '@/stores/auth'
import { useDriveStore } from '@/stores/drive'
import { useToastStore } from '@/stores/toast'
import { useUploadStore } from '@/stores/uploads'

const sync = useSyncStore()
const auth = useAuthStore()
const drive = useDriveStore()
const uploads = useUploadStore()
const toast = useToastStore()
const entered = ref(false)
const hash = ref(window.location.hash)
const module = computed(() => {
  const [page] = hash.value.split('?', 2)
  if (page === '#people') return 'people'
  if (page === '#images') return 'images'
  return 'drive'
})
const peopleVideos = computed(() => module.value === 'people' && new URLSearchParams(hash.value.split('?', 2)[1]).get('videos') === '1')
const person = computed(() => {
  if (module.value !== 'people') return undefined
  const [, query] = hash.value.split('?', 2)
  const name = new URLSearchParams(query).get('name')?.trim()
  return name && Array.from(name).length <= 80 ? name : null
})

function updateRoute(): void {
  hash.value = window.location.hash
}

async function enterApp(): Promise<void> {
  if (entered.value) return
  entered.value = true
  await (peopleVideos.value
    ? drive.selectView('video')
    : person.value === undefined
    ? drive.selectView('all')
    : drive.selectView('people', person.value))
  sync.start()
  const pending = await uploads.restore()
  if (pending > 0) {
    toast.push(`有 ${pending} 个未完成的上传，重新拖入同名文件即可续传`, 'info', 6000)
  }
}

function onUnauthorized(): void {
  auth.markUnauthorized()
  toast.error('登录状态已失效，请重新登录')
}

watch(
  () => auth.authenticated,
  (authenticated) => {
    if (authenticated) {
      void enterApp()
    } else {
      sync.stop()
      entered.value = false
    }
  },
)

watch(
  () => uploads.completedCount,
  () => {
    if (auth.authenticated) void drive.load()
  },
)

onMounted(async () => {
  window.addEventListener('hashchange', updateRoute)
  window.addEventListener('tgdrive:unauthorized', onUnauthorized)
  await auth.bootstrap()
  if (auth.authenticated) await enterApp()
})

onBeforeUnmount(() => {
  sync.stop()
  window.removeEventListener('hashchange', updateRoute)
  window.removeEventListener('tgdrive:unauthorized', onUnauthorized)
})
</script>

<template>
  <LoginView v-if="auth.ready && !auth.authenticated" />
  <div v-else-if="auth.authenticated" class="app-shell">
    <AppHeader :module="module" />
    <StorageWarning :storage="auth.storage" />
    <main class="main" :class="{ 'has-transfers': uploads.hasJobs }">
      <FileBrowser v-if="module !== 'images'" :person="person" :people-videos="peopleVideos" />
      <ImageHost v-else />
    </main>
    <UploadPanel />
  </div>
  <div v-else class="boot-screen">
    <div class="brand-mark">TG</div>
    <p>正在连接 TG Drive…</p>
  </div>
  <ToastHost />
</template>
