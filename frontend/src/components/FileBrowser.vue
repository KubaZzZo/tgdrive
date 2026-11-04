<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'

import { api } from '@/api/client'
import type { DriveItem, FileAction, FileCategory } from '@/api/types'
import { useDriveStore } from '@/stores/drive'
import { useToastStore } from '@/stores/toast'
import { useUploadStore } from '@/stores/uploads'
import { formatBytes, previewKind } from '@/utils/format'
import AppIcon from './AppIcon.vue'
import BaseModal from './BaseModal.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import FileTable from './FileTable.vue'
import PreviewModal from './PreviewModal.vue'
import PromptDialog from './PromptDialog.vue'

const drive = useDriveStore()
const uploads = useUploadStore()
const toast = useToastStore()

const classificationHelp = ref(false)
let searchTimer: ReturnType<typeof setTimeout>
function scheduleSearch(): void {
  clearTimeout(searchTimer)
  searchTimer = setTimeout(() => {
    void drive.search()
  }, 300)
}
function submitSearch(): void {
  clearTimeout(searchTimer)
  void drive.search()
}
function clearSearch(): void {
  clearTimeout(searchTimer)
  drive.query = ''
  void drive.search()
}
function navigate(view: 'all' | 'recent' | FileCategory): void {
  clearTimeout(searchTimer)
  void drive.selectView(view)
}
function breadcrumb(id: string | null): void {
  clearTimeout(searchTimer)
  void drive.openFolder(id)
}
function onAction(action: FileAction, item: DriveItem): void {
  if (action === 'download') download(item)
  if (action === 'share') void share(item)
  if (action === 'rename') renameTarget.value = item
  if (action === 'remove') deleteTarget.value = item
}
onBeforeUnmount(() => clearTimeout(searchTimer))
const overview = computed(() =>
  drive.categories.filter((c) => ['image', 'video', 'document', 'archive'].includes(c.id)),
)
const description = computed(() =>
  drive.folderId
    ? '保存在此文件夹中的文件，也会自动出现在对应分类。'
    : drive.view === 'all'
      ? '所有设备上传的文件，统一收纳、自动分类。'
      : drive.view === 'recent'
        ? '集中查看最近上传到网盘的文件。'
        : '从所有文件夹中汇集同类文件，保留原有目录。',
)
const dragging = ref(false)
const folderDialog = ref(false)
const renameTarget = ref<DriveItem | null>(null)
const deleteTarget = ref<DriveItem | null>(null)
const previewTarget = ref<DriveItem | null>(null)
const previewOpen = ref(false)

const isEmpty = computed(() => !drive.loading && !drive.error && drive.items.length === 0)

async function openFolder(item: DriveItem): Promise<void> {
  clearTimeout(searchTimer)
  await drive.openFolder(item.id)
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
  <div class="library-layout">
    <aside class="library-sidebar" aria-label="文件导航">
      <p class="nav-heading">文件空间</p>
      <nav class="primary-nav">
        <button
          :class="{ active: drive.view === 'all' }"
          :aria-current="drive.view === 'all' ? 'page' : undefined"
          @click="navigate('all')"
        >
          <AppIcon name="all" /><span>我的文件</span
          ><span class="nav-count">{{ drive.stats.files }}</span>
        </button>
        <button
          :class="{ active: drive.view === 'recent' }"
          :aria-current="drive.view === 'recent' ? 'page' : undefined"
          @click="navigate('recent')"
        >
          <AppIcon name="recent" /><span>最近上传</span>
        </button>
      </nav>
      <div class="nav-section-label"><span>文件分类</span><span class="auto-tag">自动</span></div>
      <nav class="category-nav" aria-label="文件分类">
        <button
          v-for="category in drive.categories"
          :key="category.id"
          :class="{ active: drive.view === category.id }"
          :aria-current="drive.view === category.id ? 'page' : undefined"
          @click="navigate(category.id)"
        >
          <AppIcon :name="category.id" /><span>{{ category.label }}</span
          ><span class="nav-count">{{ category.count }}</span>
        </button>
      </nav>
      <div class="sidebar-storage">
        <AppIcon name="cloud" :size="22" /><span>已存储文件</span>
        <strong>{{ formatBytes(drive.stats.bytes) }}</strong>
        <p>{{ drive.stats.files }} 个文件 · {{ drive.stats.folders }} 个文件夹</p>
        <div v-if="drive.stats.bytes" class="storage-distribution" aria-hidden="true">
          <span
            v-for="category in drive.categories"
            :key="category.id"
            :class="category.id"
            :style="{ flexGrow: category.bytes }"
          />
        </div>
        <button class="text-btn" @click="classificationHelp = true">
          了解自动分类 <AppIcon name="chevron" :size="13" />
        </button>
      </div>
    </aside>

    <section class="library-content" :aria-label="drive.title">
      <header class="library-heading">
        <div>
          <p class="eyebrow">TG DRIVE / 个人网盘</p>
          <h1>{{ drive.title }}</h1>
          <p class="page-description">{{ description }}</p>
        </div>
        <div class="toolbar-actions">
          <button class="ghost" type="button" @click="folderDialog = true">
            <AppIcon name="plus" :size="18" />新建文件夹
          </button>
          <label class="primary file-btn"
            ><AppIcon name="upload" :size="18" />上传文件<input
              aria-label="上传文件"
              type="file"
              multiple
              class="sr-only"
              @change="onFileInput"
          /></label>
        </div>
      </header>

      <div v-if="drive.view === 'all' && !drive.folderId && !drive.query" class="category-overview">
        <button
          v-for="category in overview"
          :key="category.id"
          class="category-card"
          @click="navigate(category.id)"
        >
          <span class="file-art" :class="category.id"
            ><AppIcon :name="category.id" :size="23"
          /></span>
          <span class="category-card-label"
            >{{ category.label }}<strong>{{ category.count }}<small> 个文件</small></strong></span
          >
          <span class="category-card-size">{{ formatBytes(category.bytes) }}</span>
        </button>
      </div>

      <div class="library-controls">
        <div class="search-field">
          <AppIcon name="search" :size="19" /><label class="sr-only" for="file-search"
            >搜索文件</label
          ><input
            id="file-search"
            v-model="drive.query"
            type="search"
            :placeholder="drive.folderId ? '搜索当前文件夹' : '搜索文件名称…'"
            maxlength="255"
            @input="scheduleSearch"
            @keydown.enter="submitSearch"
          /><button v-if="drive.query" class="icon-btn" aria-label="清除搜索" @click="clearSearch">
            <AppIcon name="close" :size="16" />
          </button>
        </div>
        <div class="display-controls">
          <label class="sr-only" for="file-sort">文件排序</label
          ><select id="file-sort" v-model="drive.sortOption" @change="drive.search()">
            <option value="newest">最新上传</option>
            <option value="oldest">最早上传</option>
            <option value="name">名称 A–Z</option>
            <option value="size">文件从大到小</option>
          </select>
          <div class="view-switch" role="group" aria-label="显示方式">
            <button
              :aria-pressed="drive.layout === 'list'"
              aria-label="列表视图"
              @click="drive.layout = 'list'"
            >
              <AppIcon name="list" :size="18" /></button
            ><button
              :aria-pressed="drive.layout === 'grid'"
              aria-label="网格视图"
              @click="drive.layout = 'grid'"
            >
              <AppIcon name="grid" :size="18" />
            </button>
          </div>
        </div>
      </div>

      <div class="location-row">
        <nav v-if="drive.view === 'all' && !drive.query" class="crumbs" aria-label="目录路径">
          <button class="crumb" @click="breadcrumb(null)">我的文件</button
          ><template v-for="item in drive.path" :key="item.id"
            ><AppIcon name="chevron" :size="12" /><button
              class="crumb"
              @click="breadcrumb(item.id)"
            >
              {{ item.name }}
            </button></template
          >
        </nav>
        <span v-else class="results-label">{{
          drive.query ? `“${drive.query}”的搜索结果` : `全部${drive.title}文件`
        }}</span>
        <span class="item-count" aria-live="polite">{{ drive.total }} 个项目</span>
      </div>

      <div v-if="drive.error" class="inline-error" role="alert">
        <span>{{ drive.error }}</span
        ><button class="text-btn" @click="drive.load()">重新加载</button>
      </div>
      <div
        v-if="drive.loading"
        class="table-wrap skeleton-wrap"
        aria-label="正在加载文件"
        aria-busy="true"
      >
        <div v-for="i in 6" :key="i" class="skeleton-row" />
      </div>
      <FileTable
        v-else-if="drive.items.length && !drive.error"
        :items="drive.items"
        :layout="drive.layout"
        @open="openFolder"
        @preview="previewItem"
        @action="onAction"
      />
      <div v-if="isEmpty" class="empty">
        <span class="empty-art"
          ><AppIcon
            :name="drive.query ? 'search' : drive.view === 'all' ? 'folder' : drive.view"
            :size="42"
        /></span>
        <h2>{{ drive.query ? '没有找到匹配的文件' : '这里还没有文件' }}</h2>
        <p>
          {{
            drive.query
              ? '试试其他关键词，或清除搜索查看所有文件。'
              : '上传后会自动归入对应分类，在电脑和手机上都能找到。'
          }}
        </p>
        <button v-if="drive.query" class="ghost" @click="clearSearch">清除搜索</button>
      </div>
      <footer v-if="drive.total > drive.pageSize" class="pagination">
        <span
          >{{ (drive.page - 1) * drive.pageSize + 1 }}–{{
            Math.min(drive.page * drive.pageSize, drive.total)
          }}
          / {{ drive.total }} 个项目</span
        >
        <div>
          <button
            class="ghost"
            :disabled="drive.page <= 1 || drive.loading"
            @click="drive.setPage(drive.page - 1)"
          >
            上一页</button
          ><span>{{ drive.page }} / {{ drive.pageCount }}</span
          ><button
            class="ghost"
            :disabled="drive.page >= drive.pageCount || drive.loading"
            @click="drive.setPage(drive.page + 1)"
          >
            下一页
          </button>
        </div>
      </footer>
      <div
        class="drop-zone"
        :class="{ dragover: dragging }"
        @dragenter.prevent="dragging = true"
        @dragover.prevent="dragging = true"
        @dragleave.prevent="onDragLeave"
        @drop.prevent="onDrop"
      >
        <AppIcon name="upload" :size="24" />
        <div>
          <strong>拖入文件，剩下的交给自动分类</strong>
          <p>
            {{ drive.folderId ? '文件将保存到当前文件夹' : '文件将保存到我的文件' }}
            · 支持分片续传
          </p>
        </div>
        <span class="drop-caption">电脑拖放 / 手机选择文件</span>
      </div>
    </section>

    <PromptDialog
      :open="folderDialog"
      title="新建文件夹"
      label="文件夹名称"
      placeholder="例如：旅行照片"
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
    <BaseModal
      :open="classificationHelp"
      title="文件会如何自动分类？"
      @close="classificationHelp = false"
      ><div class="classification-help">
        <p>
          在电脑或手机网页上传后，文件会按格式自动归入以下分类。自建文件夹会保留，分类中可以找到所有目录里的同类文件。
        </p>
        <dl>
          <div v-for="category in drive.categories" :key="category.id">
            <AppIcon :name="category.id" />
            <dt>{{ category.label }}</dt>
            <dd>
              {{
                {
                  image: '照片、截图、HEIC / HEIF',
                  video: 'MP4、MOV、MKV 等视频',
                  audio: 'MP3、FLAC、录音文件',
                  document: 'PDF、Office、电子书、文本',
                  archive: 'ZIP、RAR、7Z 等压缩文件',
                  code: '源代码、脚本、配置文件',
                  application: 'APK、EXE、DMG 等安装文件',
                  other: '暂未识别格式的文件',
                }[category.id]
              }}
            </dd>
          </div>
        </dl>
        <p class="muted">
          更改文件扩展名后会重新归类。分类仅用于整理，预览能力取决于浏览器对格式的支持。
        </p>
      </div></BaseModal
    >
  </div>
</template>
