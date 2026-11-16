<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'

import { api, ApiError } from '@/api/client'
import type { GoogleDriveItem } from '@/api/types'
import AppIcon from '@/components/ui/AppIcon.vue'
import BaseModal from '@/components/ui/BaseModal.vue'
import { formatBytes } from '@/utils/format'

const MediaPlayer = defineAsyncComponent(() => import('@/components/MediaPlayer.vue'))
const route = useRoute()
const folder = computed(() => typeof route.query.path === 'string' ? route.query.path : '')
const breadcrumbs = computed(() => folder.value ? folder.value.split('/') : [])
const items = ref<GoogleDriveItem[]>([])
const loading = ref(false)
const error = ref('')
const refresh = ref(0)
const selected = ref<GoogleDriveItem | null>(null)
const imageFailed = ref(false)
const selectedKind = computed(() => selected.value ? itemIcon(selected.value) : '')
const playerKind = computed(() => selectedKind.value === 'audio' ? 'audio' : 'video')
const isPlayer = computed(() => selectedKind.value === 'video' || selectedKind.value === 'audio')

function folderLink(path: string) {
  return { name: 'google-drive', query: path ? { path } : {} }
}

function itemPath(name: string): string {
  return folder.value ? `${folder.value}/${name}` : name
}

function itemIcon(item: GoogleDriveItem): string {
  if (item.folder) return 'folder'
  const kind = item.mime?.split('/')[0]
  return kind === 'image' || kind === 'video' || kind === 'audio' ? kind : 'document'
}

function openPreview(item: GoogleDriveItem): void {
  imageFailed.value = false
  selected.value = item
}

function downloadSelected(): void {
  if (selected.value) window.location.assign(api.googleDriveContentUrl(itemPath(selected.value.name), true))
}

watch([folder, refresh], async ([path], _, onCleanup) => {
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  selected.value = null
  items.value = []
  error.value = ''
  loading.value = true
  try {
    const result = await api.googleDriveItems(path, controller.signal)
    if (!controller.signal.aborted) items.value = result.items
  } catch (cause) {
    if (!controller.signal.aborted) {
      error.value = cause instanceof ApiError ? cause.message : '无法加载 Google Drive'
    }
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}, { immediate: true })
</script>

<template>
  <main class="google-drive">
    <h1>Google Drive</h1>
    <div class="folder-toolbar">
      <nav aria-label="当前文件夹">
        <RouterLink v-if="folder" :to="folderLink('')">根目录</RouterLink>
        <span v-else aria-current="page">根目录</span>
        <template v-for="(part, index) in breadcrumbs" :key="index">
          <span aria-hidden="true">/</span>
          <RouterLink v-if="index < breadcrumbs.length - 1" :to="folderLink(breadcrumbs.slice(0, index + 1).join('/'))">
            {{ part }}
          </RouterLink>
          <span v-else aria-current="page">{{ part }}</span>
        </template>
      </nav>
      <button class="ghost" type="button" :disabled="loading" @click="refresh++">刷新</button>
    </div>
    <div class="folder-list" :aria-busy="loading">
      <p v-if="loading" class="muted" role="status">正在加载文件…</p>
      <p v-else-if="error" class="form-error" role="alert">{{ error }}</p>
      <p v-else-if="!items.length" class="muted">此文件夹为空</p>
      <ul v-else>
        <li v-for="item in items" :key="item.id">
          <RouterLink v-if="item.folder" class="item-name" :to="folderLink(itemPath(item.name))">
            <AppIcon name="folder" /><span>{{ item.name }}</span>
          </RouterLink>
          <button v-else-if="itemIcon(item) !== 'document'" class="item-name" type="button" @click="openPreview(item)">
            <AppIcon :name="itemIcon(item)" /><span>{{ item.name }}</span>
          </button>
          <span v-else class="item-name"><AppIcon name="document" /><span>{{ item.name }}</span></span>
          <span v-if="!item.folder" class="item-size muted">{{ formatBytes(item.size) }}</span>
          <a v-if="!item.folder" class="ghost item-download" :href="api.googleDriveContentUrl(itemPath(item.name), true)"
            :aria-label="`下载 ${item.name}`" title="下载">
            <AppIcon name="download" :size="17" />
          </a>
        </li>
      </ul>
    </div>
    <BaseModal v-if="selected" open wide :media="isPlayer" :title="selected.name" @close="selected = null">
      <MediaPlayer v-if="isPlayer"
        :src="api.googleDriveContentUrl(itemPath(selected.name))"
        :item-id="`google-drive:${selected.id}`"
        :kind="playerKind" :size="selected.size"
        @close="selected = null" @download="downloadSelected" />
      <div v-else class="photo-preview">
        <p v-if="imageFailed" class="muted" role="alert">照片加载失败，可以下载后查看。</p>
        <img v-else :src="api.googleDriveContentUrl(itemPath(selected.name))" :alt="selected.name" @error="imageFailed = true" />
      </div>
      <template v-if="!isPlayer" #footer>
        <a class="primary" :href="api.googleDriveContentUrl(itemPath(selected.name), true)">下载</a>
      </template>
    </BaseModal>
  </main>
</template>

<style scoped>
.google-drive { width: min(100% - 32px, 950px); margin: 28px auto; }
h1 { margin: 0 0 16px; }
.folder-toolbar { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.folder-toolbar nav { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; min-width: 0; overflow-wrap: anywhere; }
.folder-list { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); overflow: hidden; }
.folder-list > p { margin: 0; padding: 20px; }
ul { list-style: none; margin: 0; padding: 0; }
li { display: flex; align-items: center; gap: 12px; padding: 8px 12px; min-height: 48px; }
li + li { border-top: 1px solid var(--line); }
.item-name { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; text-align: left; overflow-wrap: anywhere; padding: 6px 0; color: inherit; }
.item-name svg { flex: none; }
button.item-name { background: transparent; border: 0; }
a.item-name:hover, button.item-name:hover { text-decoration: underline; }
.item-size { white-space: nowrap; }
.item-download { display: grid; place-items: center; width: 36px; height: 36px; flex: none; }
.photo-preview { display: grid; place-items: center; min-height: 160px; }
.photo-preview img { max-width: 100%; max-height: 68dvh; object-fit: contain; }
</style>
