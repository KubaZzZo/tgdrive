<script setup lang="ts">
import { computed, ref } from 'vue'

import { api } from '@/api/client'
import type { DriveItem } from '@/api/types'
import { useDriveStore } from '@/stores/drive'
import { useToastStore } from '@/stores/toast'
import { useUploadStore } from '@/stores/uploads'
import { formatBytes, previewKind } from '@/utils/format'
import ConfirmDialog from './ConfirmDialog.vue'
import FileTable from './FileTable.vue'
import PreviewModal from './PreviewModal.vue'
import PromptDialog from './PromptDialog.vue'

const drive = useDriveStore()
const uploads = useUploadStore()
const toast = useToastStore()

const dragging = ref(false)
const folderDialog = ref(false)
const renameTarget = ref<DriveItem | null>(null)
const deleteTarget = ref<DriveItem | null>(null)
const previewTarget = ref<DriveItem | null>(null)
const previewOpen = ref(false)

const isEmpty = computed(() => !drive.loading && !drive.error && drive.items.length === 0)

async function openFolder(item: DriveItem): Promise<void> {
  await drive.load(item.id)
}

function openItem(item: DriveItem): void {
  previewTarget.value = item
  previewOpen.value = true
}

function previewItem(item: DriveItem): void {
  if (previewKind(item) === 'none') {
    download(item)
    return
  }
  openItem(item)
}

function download(item: DriveItem): void {
  window.location.assign(api.downloadUrl(item.id, true))
}

async function share(item: DriveItem): Promise<void> {
  try {
    const url = await drive.share(item)
    try {
      await navigator.clipboard.writeText(url)
      toast.success('分享链接已复制')
    } catch {
      window.prompt('分享链接', url)
    }
  } catch {
    // The drive store already reports the error.
  }
}

async function confirmCreateFolder(name: string): Promise<void> {
  try {
    await drive.createFolder(name)
    folderDialog.value = false
  } catch {
    // Keep the dialog open so the user can correct the name.
  }
}

async function confirmRename(name: string): Promise<void> {
  const target = renameTarget.value
  if (!target) return
  try {
    await drive.rename(target, name)
    renameTarget.value = null
  } catch {
    // Keep the dialog open.
  }
}

async function confirmDelete(): Promise<void> {
  const target = deleteTarget.value
  if (!target) return
  try {
    await drive.remove(target)
    deleteTarget.value = null
  } catch {
    // The drive store already reports the error.
  }
}

function addFiles(files: FileList | File[]): void {
  uploads.enqueue(files, drive.folderId)
}

function onFileInput(event: Event): void {
  const input = event.target as HTMLInputElement
  if (input.files?.length) addFiles(input.files)
  input.value = ''
}

function onDrop(event: DragEvent): void {
  dragging.value = false
  if (event.dataTransfer?.files.length) addFiles(event.dataTransfer.files)
}

function onDragLeave(event: DragEvent): void {
  const current = event.currentTarget as HTMLElement
  const next = event.relatedTarget as Node | null
  if (!next || !current.contains(next)) dragging.value = false
}

function closePreview(): void {
  previewOpen.value = false
  previewTarget.value = null
}
</script>

<template>
  <section class="file-browser">
    <div class="toolbar">
      <nav class="crumbs" aria-label="目录路径">
        <button class="crumb" :class="{ current: drive.path.length === 0 }" @click="drive.load(null)">
          我的文件
        </button>
        <template v-for="(item, index) in drive.path" :key="item.id">
          <span class="crumb-sep">/</span>
          <button
            class="crumb"
            :class="{ current: index === drive.path.length - 1 }"
            @click="drive.load(item.id)"
          >
            {{ item.name }}
          </button>
        </template>
      </nav>

      <div class="toolbar-actions">
        <button class="ghost" type="button" @click="folderDialog = true">＋ 文件夹</button>
        <label class="primary file-btn">
          ↑ 上传
          <input type="file" multiple hidden @change="onFileInput" />
        </label>
      </div>
    </div>

    <div
      class="drop-zone"
      :class="{ dragover: dragging }"
      @dragenter.prevent="dragging = true"
      @dragover.prevent="dragging = true"
      @dragleave.prevent="onDragLeave"
      @drop.prevent="onDrop"
    >
      <div class="drop-inner">
        <span class="drop-icon">⇧</span>
        <span>拖拽文件到这里上传，支持刷新后断点续传</span>
      </div>
    </div>

    <div class="stats-row">
      <span class="stat-chip">{{ drive.stats.files }} 个文件</span>
      <span class="stat-chip">{{ drive.stats.folders }} 个文件夹</span>
      <span class="stat-chip">已用 {{ formatBytes(drive.stats.bytes) }}</span>
    </div>

    <div v-if="drive.error" class="inline-error" role="alert">
      <span>{{ drive.error }}</span>
      <button class="text-btn" type="button" @click="drive.load()">重试</button>
    </div>

    <div v-if="drive.loading && drive.items.length === 0" class="table-wrap skeleton-wrap">
      <div v-for="index in 5" :key="index" class="skeleton-row" />
    </div>

    <FileTable
      v-else-if="drive.items.length"
      :items="drive.items"
      @open="openFolder"
      @preview="previewItem"
      @download="download"
      @share="share"
      @rename="renameTarget = $event"
      @remove="deleteTarget = $event"
    />

    <div v-if="isEmpty" class="empty">
      <div class="empty-icon">☁</div>
      <p>这里还没有文件，拖一个上来试试</p>
    </div>

    <PromptDialog
      :open="folderDialog"
      title="新建文件夹"
      label="文件夹名称"
      placeholder="例如：电影"
      confirm-text="创建"
      @close="folderDialog = false"
      @confirm="confirmCreateFolder"
    />
    <PromptDialog
      :open="Boolean(renameTarget)"
      title="重命名"
      label="新名称"
      :initial="renameTarget?.name ?? ''"
      confirm-text="保存"
      @close="renameTarget = null"
      @confirm="confirmRename"
    />
    <ConfirmDialog
      :open="Boolean(deleteTarget)"
      title="删除项目"
      :message="`确定删除「${deleteTarget?.name ?? ''}」吗？文件夹内的内容也会一起删除。`"
      confirm-text="删除"
      danger
      @close="deleteTarget = null"
      @confirm="confirmDelete"
    />
    <PreviewModal
      :open="previewOpen"
      :item="previewTarget"
      @close="closePreview"
      @download="download"
    />
  </section>
</template>
