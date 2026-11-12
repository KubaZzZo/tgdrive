<script setup lang="ts">
import { computed, defineAsyncComponent, onBeforeUnmount, reactive, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { BatchItemResult, DriveItem, FileAction, FileCategory } from '@/api/types'
import type { DriveFilters } from '@/utils/driveLocation'
import { useDriveStore } from '@/stores/drive'
import { useToastStore } from '@/stores/toast'
import { useUploadStore } from '@/stores/uploads'
import { formatBytes, previewKind } from '@/utils/format'
import { groupFolderFiles } from '@/utils/folderUpload'
import AppIcon from './AppIcon.vue'
import BaseModal from './BaseModal.vue'
import ConfirmDialog from './ConfirmDialog.vue'
import FileTable from './FileTable.vue'
import PromptDialog from './PromptDialog.vue'

const AdvancedSearchDialog = defineAsyncComponent(() => import('./AdvancedSearchDialog.vue'))
const MoveDialog = defineAsyncComponent(() => import('./MoveDialog.vue'))
const PeopleIndex = defineAsyncComponent(() => import('./PeopleIndex.vue'))
const PreviewModal = defineAsyncComponent(() => import('./PreviewModal.vue'))
const ShareDialog = defineAsyncComponent(() => import('./ShareDialog.vue'))
const TrashDialog = defineAsyncComponent(() => import('./TrashDialog.vue'))
const VideoPeopleDialog = defineAsyncComponent(() => import('./VideoPeopleDialog.vue'))

const drive = useDriveStore()
const uploads = useUploadStore()
const toast = useToastStore()
const props = defineProps<{ routeRevision: number; peopleVideos?: boolean }>()
const inPeople = computed(() => drive.view === 'people' || props.peopleVideos)

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
  drive.filters = {}
  void drive.search()
}
function navigate(view: 'all' | 'recent' | FileCategory): void {
  clearTimeout(searchTimer)
  void drive.selectView(view)
}
function openPeople(): void {
  window.location.hash = 'people'
}
function breadcrumb(id: string | null): void {
  clearTimeout(searchTimer)
  void drive.openFolder(id)
}
function onAction(action: FileAction, item: DriveItem): void {
  if (action === 'download') download(item)
  if (action === 'share') shareTarget.value = item
  if (action === 'rename') renameTarget.value = item
  if (action === 'move') moveTargets.value = [item]
  if (action === 'remove') deleteTarget.value = item
  if (action === 'people') labelTarget.value = item
}
onBeforeUnmount(() => clearTimeout(searchTimer))
const overview = computed(() =>
  drive.categories.filter((c) => ['image', 'video', 'document', 'archive'].includes(c.id)),
)
const description = computed(() => {
  if (drive.folderId) return '保存在此文件夹中的文件，也会自动出现在对应分类。'
  if (drive.view === 'all') return '所有设备上传的文件，统一收纳、自动分类。'
  if (drive.view === 'recent') return '集中查看最近上传到网盘的文件。'
  if (drive.view === 'people') {
    return drive.person ? '已分配此标签的视频。' : '按人物标签浏览视频。'
  }
  return '从所有文件夹中汇集同类文件，保留原有目录。'
})
const dragging = ref(false)
const folderDialog = ref(false)
const renameTarget = ref<DriveItem | null>(null)
const deleteTarget = ref<DriveItem | null>(null)
const shareTarget = ref<DriveItem | null>(null)
const sharesOpen = ref(false)
const trashOpen = ref(false)
const moveTargets = ref<DriveItem[] | null>(null)
const advancedOpen = ref(false)
const creatingPaths = ref(false)
const selecting = ref(false)
const selected = reactive(new Set<string>())
const selectedItems = computed(() => drive.items.filter((item) => selected.has(item.id)))
const confirmingBatch = ref(false)
const submittingBatch = ref(false)
const batchStatus = ref<{ successes: number; failures: string[] } | null>(null)
const labelTarget = ref<DriveItem | null>(null)
const previewTarget = ref<DriveItem | null>(null)
const previewOpen = ref(false)
const downloadStatus = ref('')

const isEmpty = computed(() => !drive.loading && !drive.error && drive.items.length === 0)
const hasFilters = computed(() => Object.values(drive.filters).some((value) => value !== undefined))
const searching = computed(() => Boolean(drive.query.trim()) || hasFilters.value)

function applyFilters(filters: DriveFilters): void {
  drive.filters = filters
  advancedOpen.value = false
  submitSearch()
}

function toggleSelected(id: string): void {
  if (selected.has(id)) selected.delete(id)
  else selected.add(id)
}

function togglePage(): void {
  if (selectedItems.value.length === drive.items.length) selected.clear()
  else for (const item of drive.items) selected.add(item.id)
}

function handleBatch(results: BatchItemResult[], attempted: DriveItem[]): void {
  const failures: string[] = []
  let successes = 0
  for (const result of results) {
    if (result.ok) {
      successes++
      selected.delete(result.id)
    } else {
      failures.push(`${attempted.find((item) => item.id === result.id)?.name ?? result.id}：${result.error}`)
    }
  }
  batchStatus.value = { successes, failures }
  if (successes) void drive.load()
}

async function batchTrash(): Promise<void> {
  if (submittingBatch.value || !selectedItems.value.length) return
  const attempted = selectedItems.value
  submittingBatch.value = true
  try {
    const { results } = await api.batchItems(attempted.map((item) => item.id), 'trash')
    handleBatch(results, attempted)
    confirmingBatch.value = false
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : '批量删除失败')
  } finally {
    submittingBatch.value = false
  }
}

watch([() => drive.folderId, () => drive.view, () => drive.person, () => drive.page,
  () => drive.query, () => drive.filters, () => drive.searchScope, () => drive.sortOption], () => {
  selected.clear()
  selecting.value = false
})

async function openFolder(item: DriveItem): Promise<void> {
  clearTimeout(searchTimer)
  await drive.openFolder(item.id)
}

function openItem(item: DriveItem): void {
  previewTarget.value = item
  previewOpen.value = true
  downloadStatus.value = ''
}

function previewItem(item: DriveItem): void {
  if (previewKind(item) === 'none') {
    download(item)
    return
  }
  openItem(item)
}

const pendingDownloads = reactive(new Set<string>())
async function download(item: DriveItem): Promise<void> {
  if (pendingDownloads.has(item.id)) return
  pendingDownloads.add(item.id)
  const inPreview = () => previewOpen.value && previewTarget.value?.id === item.id
  const report = (message: string, failed = false) => {
    if (inPreview()) downloadStatus.value = message
    else if (failed) toast.error(message)
    else toast.push(message)
  }
  if (inPreview()) downloadStatus.value = '正在检查下载…'
  const url = api.downloadUrl(item.id, true)
  try {
    await api.checkDownload(item.id)
    const link = document.createElement('a')
    link.href = url
    link.download = item.name
    document.body.append(link)
    link.click()
    link.remove()
    report('已提交下载请求，请在浏览器下载列表查看进度')
  } catch (error) {
    report(error instanceof Error ? error.message : '下载失败，请重试', true)
  } finally { pendingDownloads.delete(item.id) }
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

async function onFolderInput(event: Event): Promise<void> {
  const input = event.target as HTMLInputElement
  const files = Array.from(input.files ?? [])
  input.value = ''
  if (!files.length || creatingPaths.value) return
  const parent = drive.folderId
  creatingPaths.value = true
  try {
    const groups = groupFolderFiles(files)
    const ids: string[] = []
    for (let start = 0; start < groups.length; start += 200) {
      const paths = groups.slice(start, start + 200).map((group) => group.path)
      const result = await api.createFolderPaths(parent, paths)
      if (result.ids.length !== paths.length || result.ids.some((id) => !id)) {
        throw new Error('部分上传目录未创建，请重试')
      }
      ids.push(...result.ids)
    }
    uploads.enqueueFiles(groups.flatMap((group, index) =>
      group.files.map((file) => ({ file, parentId: ids[index]! })),
    ))
    await drive.load()
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : '无法准备上传目录')
  } finally {
    creatingPaths.value = false
  }
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

watch(() => props.routeRevision, () => {
  clearTimeout(searchTimer)
  closePreview()
  renameTarget.value = null
  deleteTarget.value = null
  labelTarget.value = null
  shareTarget.value = null
  sharesOpen.value = false
  trashOpen.value = false
  advancedOpen.value = false
  moveTargets.value = null
  confirmingBatch.value = false
  folderDialog.value = false
  classificationHelp.value = false
})
</script>
<template>
  <div class="library-layout" :class="{ 'people-layout': inPeople }">
    <aside v-if="!inPeople" class="library-sidebar" aria-label="文件导航">
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
        <button type="button" @click="openPeople">
          <AppIcon name="people" /><span>人物视频</span>
        </button>
        <button type="button" @click="sharesOpen = true">
          <AppIcon name="link" /><span>我的分享</span>
        </button>
        <button type="button" @click="trashOpen = true">
          <AppIcon name="trash" /><span>回收站</span>
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
          <p v-if="drive.person && drive.person !== drive.title" class="page-description">
            原名 {{ drive.person }}
          </p>
          <p v-else class="page-description">{{ description }}</p>
        </div>
        <div v-if="!inPeople" class="toolbar-actions">
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
          <label class="ghost file-btn" :class="{ 'is-busy': creatingPaths }">
            <AppIcon name="folder" :size="18" />{{ creatingPaths ? '正在准备目录…' : '上传文件夹' }}
            <input aria-label="上传文件夹" type="file" multiple webkitdirectory class="sr-only" :disabled="creatingPaths" @change="onFolderInput" />
          </label>
        </div>
      </header>

      <PeopleIndex v-if="drive.view === 'people' && !drive.person" />
      <template v-else>
      <div v-if="drive.view === 'all' && !drive.folderId && !searching" class="category-overview">
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
            :placeholder="drive.person ? '搜索此人物的视频…' : '搜索文件名称…'"
            maxlength="255"
            @input="scheduleSearch"
            @keydown.enter="submitSearch"
          /><button v-if="drive.query" class="icon-btn" aria-label="清除搜索" @click="clearSearch">
            <AppIcon name="close" :size="16" />
          </button>
        </div>
        <div class="display-controls">
          <label v-if="!inPeople" class="search-scope"><span class="sr-only">搜索范围</span>
            <select aria-label="搜索范围" :value="drive.searchScope || (drive.folderId ? 'folder' : 'all')"
              @change="drive.searchScope = ($event.target as HTMLSelectElement).value === 'folder' ? 'folder' : 'all'; submitSearch()">
              <option value="folder">当前目录</option>
              <option value="all">全部文件</option>
            </select>
          </label>
          <button class="ghost" type="button" @click="advancedOpen = true">
            {{ hasFilters ? '高级搜索 · 已筛选' : '高级搜索' }}
          </button>
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
              @click="drive.setLayout('list')"
            >
              <AppIcon name="list" :size="18" /></button
            ><button
              :aria-pressed="drive.layout === 'grid'"
              aria-label="网格视图"
              @click="drive.setLayout('grid')"
            >
              <AppIcon name="grid" :size="18" />
            </button>
          </div>
        </div>
      </div>

      <div class="location-row">
        <nav v-if="drive.person || peopleVideos" class="crumbs" aria-label="人物目录">
          <a class="crumb" href="#people">人物标签</a>
          <AppIcon name="chevron" :size="12" />
          <span>{{ drive.title }}</span>
        </nav>
        <nav v-else-if="drive.view === 'all' && (!searching || drive.folderId)" class="crumbs" aria-label="目录路径">
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
          drive.query ? `“${drive.query}”的搜索结果` : hasFilters ? '筛选结果' : `全部${drive.title}文件`
        }}</span>
        <span class="item-count" aria-live="polite">
          {{ drive.total }} {{ drive.person ? '部视频' : '个项目' }}
        </span>
      </div>

      <div v-if="drive.items.length && !drive.loading && !drive.error" class="operation-actions selection-bar">
        <button class="ghost" type="button" :aria-pressed="selecting"
          @click="selecting = !selecting; if (!selecting) selected.clear()">{{ selecting ? '退出多选' : '多选' }}</button>
        <template v-if="selecting">
          <button class="ghost" type="button" @click="togglePage">{{ selectedItems.length === drive.items.length ? '取消全选' : '选择本页' }}</button>
          <span aria-live="polite">已选 {{ selectedItems.length }} 项</span>
          <button class="ghost" type="button" :disabled="!selectedItems.length || submittingBatch" @click="moveTargets = selectedItems">批量移动</button>
          <button class="ghost danger-text" type="button" :disabled="!selectedItems.length || submittingBatch" @click="confirmingBatch = true">批量删除</button>
        </template>
      </div>
      <section v-if="batchStatus" class="batch-result" aria-live="polite">
        <div class="operation-actions">
          <strong>操作完成 · {{ batchStatus.successes }} 项成功，{{ batchStatus.failures.length }} 项失败</strong>
          <button type="button" class="text-btn" @click="batchStatus = null">关闭结果</button>
        </div>
        <ul v-if="batchStatus.failures.length"><li v-for="(message, index) in batchStatus.failures" :key="index">{{ message }}</li></ul>
      </section>
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
        :selecting="selecting"
        :selected="selected"
        @select="toggleSelected"
        @open="openFolder"
        @preview="previewItem"
        @action="onAction"
      />
      <div v-if="isEmpty" class="empty">
        <span class="empty-art"
          ><AppIcon
          :name="searching ? 'search' : drive.person ? 'video' : drive.view === 'all' ? 'folder' : drive.view"
            :size="42"
        /></span>
        <h2>{{ searching ? '没有找到匹配的文件' : drive.person ? '暂无相关视频' : '这里还没有文件' }}</h2>
        <p>
          {{
            searching
              ? '试试其他关键词或筛选条件。'
              : drive.person
                ? '返回人物目录，查看其他人物的视频。'
              : '上传后会自动归入对应分类，在电脑和手机上都能找到。'
          }}
        </p>
        <button v-if="searching" class="ghost" @click="clearSearch">清除搜索</button>
      </div>
      <footer v-if="drive.total > drive.pageSize" class="pagination">
        <span
          >{{ (drive.page - 1) * drive.pageSize + 1 }}–{{
            Math.min(drive.page * drive.pageSize, drive.total)
          }}
          / {{ drive.total }} {{ drive.person ? '部视频' : '个项目' }}</span
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
        v-if="!inPeople"
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
      </template>
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
      title="移入回收站"
      :message="`将「${deleteTarget?.name ?? ''}」及其内容移入回收站？`"
      confirm-text="移入回收站"
      danger
      @close="deleteTarget = null"
      @confirm="confirmDelete"
    />
    <ConfirmDialog
      :open="confirmingBatch"
      title="批量移入回收站"
      :message="`将选中的 ${selectedItems.length} 个项目及其内容移入回收站？`"
      :confirm-text="submittingBatch ? '正在处理…' : '移入回收站'"
      danger
      @close="!submittingBatch && (confirmingBatch = false)"
      @confirm="batchTrash"
    />
    <MoveDialog v-if="moveTargets" :items="moveTargets" :initial-folder="drive.folderId"
      @result="handleBatch" @close="moveTargets = null" />
    <ShareDialog v-if="shareTarget || sharesOpen" :item="shareTarget ?? undefined"
      @close="shareTarget = null; sharesOpen = false" />
    <TrashDialog v-if="trashOpen" @close="trashOpen = false" @restored="drive.load()" />
    <AdvancedSearchDialog v-if="advancedOpen" :filters="drive.filters"
      @close="advancedOpen = false" @apply="applyFilters" />
    <PreviewModal
      v-if="previewOpen && previewTarget"
      :open="previewOpen"
      :item="previewTarget"
      :download-pending="!!previewTarget && pendingDownloads.has(previewTarget.id)"
      :download-status="downloadStatus"
      @close="closePreview"
      @download="download"
    />
    <VideoPeopleDialog
      v-if="labelTarget"
      :item="labelTarget"
      @close="labelTarget = null"
      @saved="labelTarget = null; drive.load()"
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

<style scoped>
.people-layout {
  grid-template-columns: minmax(0, 1fr);
  overflow-wrap: anywhere;
}
@media (min-width: 761px) {
  .people-layout { padding-left: 28px; }
}
@media (min-width: 1440px) {
  .people-layout { padding-left: 44px; }
}
</style>
