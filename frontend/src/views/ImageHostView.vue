<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { api } from '@/api/client'
import type { DriveItem, HostedImage } from '@/api/types'
import { useUploadStore } from '@/stores/uploads'
import { useToastStore } from '@/stores/toast'
import { formatBytes } from '@/utils/format'
import { pastedImageFiles } from '@/utils/imagePaste'
import BaseModal from '@/components/ui/BaseModal.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'

const uploads = useUploadStore()
const toast = useToastStore()
const mode = ref<'published' | 'drive'>('published')
const query = ref('')
const offset = ref(0)
const images = ref<(DriveItem & Partial<HostedImage>)[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref('')
const busy = ref<string | null>(null)
const dragging = ref(false)
const picker = ref<HTMLInputElement | null>(null)
const selected = ref<{ name: string; url: string } | null>(null)
const revokeTarget = ref<DriveItem | null>(null)
const format = ref<'url' | 'markdown' | 'html'>('url')
let generation = 0
let timer: ReturnType<typeof setTimeout>

const linkText = computed(() => {
  if (!selected.value) return ''
  const url = new URL(selected.value.url, window.location.origin).href
  const name = selected.value.name
  if (format.value === 'markdown') return `![${name.replace(/[\\\[\]]/g, '\\$&')}](${url})`
  if (format.value === 'html') {
    const escaped = name.replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    return `<img src="${url}" alt="${escaped}" />`
  }
  return url
})

async function load(): Promise<void> {
  const current = ++generation
  loading.value = true
  error.value = ''
  try {
    if (mode.value === 'published') {
      const data = await api.listImages(query.value, offset.value)
      if (current !== generation) return
      images.value = data.images
      total.value = data.total
    } else {
      const data = await api.listItems(null, { scope: 'all', category: 'image', q: query.value, limit: 24, offset: offset.value, sort: 'created_at', direction: 'desc' })
      if (current !== generation) return
      images.value = data.items
      total.value = data.total
    }
  } catch (cause) {
    if (current === generation) error.value = cause instanceof Error ? cause.message : '图片加载失败'
  } finally {
    if (current === generation) loading.value = false
  }
}

function add(files: FileList | File[]): void {
  const added = uploads.enqueue(files, null, true)
  if (added) { mode.value = 'published'; offset.value = 0 }
}
function choose(event: Event): void {
  const input = event.target as HTMLInputElement
  if (input.files) add(input.files)
  input.value = ''
}
function drop(event: DragEvent): void {
  dragging.value = false
  if (event.dataTransfer?.files.length) add(event.dataTransfer.files)
}
function paste(event: ClipboardEvent): void {
  const files = pastedImageFiles(event, !!document.querySelector('[aria-modal="true"]'))
  if (files.length) { event.preventDefault(); add(files) }
}
async function publish(item: DriveItem): Promise<void> {
  if (busy.value) return
  busy.value = item.id
  try {
    const result = await api.publishImage(item.id)
    selected.value = { name: item.name, url: result.url }
    toast.success('已发布，持有链接的人可查看这张图片')
  } catch (cause) { toast.error(cause instanceof Error ? cause.message : '发布失败') }
  finally { busy.value = null }
}
async function revoke(): Promise<void> {
  const target = revokeTarget.value
  if (!target || busy.value) return
  busy.value = target.id
  try {
    await api.unpublishImage(target.id)
    revokeTarget.value = null
    if (images.value.length === 1 && offset.value > 0) offset.value -= 24
    await load()
    toast.success('外链已撤销，原文件仍保存在网盘')
  } catch (cause) { toast.error(cause instanceof Error ? cause.message : '撤销失败') }
  finally { busy.value = null }
}
async function copy(): Promise<void> {
  try { await navigator.clipboard.writeText(linkText.value); toast.success('链接已复制') }
  catch { toast.error('无法访问剪贴板，请选中下方内容手动复制') }
}
watch([mode, offset], () => { void load() })
watch(query, () => {
  clearTimeout(timer)
  timer = setTimeout(() => { offset.value = 0; void load() }, 250)
})
watch(() => uploads.completedCount, () => { void load() })
onMounted(() => { void load(); window.addEventListener('paste', paste) })
onBeforeUnmount(() => { generation++; clearTimeout(timer); window.removeEventListener('paste', paste) })
</script>

<template>
  <section class="image-host" @dragover.prevent="dragging = true" @drop.prevent="drop">
    <header class="image-heading">
      <div><p class="eyebrow">TELEGRAM IMAGE HOST</p><h1>图床</h1><p class="muted">上传图片，随处引用。原图存储在 Telegram。</p></div>
      <button class="primary" type="button" @click="picker?.click()">上传图片</button>
    </header>
    <input ref="picker" type="file" accept="image/jpeg,image/png,image/gif,image/webp" multiple hidden @change="choose" />
    <button class="image-drop" :class="{ dragging }" type="button" @dragleave="dragging = false" @click="picker?.click()">
      <span class="drop-mark" aria-hidden="true">＋</span>
      <strong>点击选择、拖入或粘贴图片</strong>
      <span>JPEG / PNG / GIF / WebP · 单张不超过 20 MiB · 支持批量上传</span>
    </button>
    <p class="publish-notice">在图床上传即发布，持有外链的人无需登录即可查看。网盘里的其他图片保持私有。</p>
    <div class="image-toolbar">
      <div class="image-tabs" aria-label="图片来源">
        <button type="button" :aria-pressed="mode === 'published'" @click="offset = 0; mode = 'published'">已发布</button>
        <button type="button" :aria-pressed="mode === 'drive'" @click="offset = 0; mode = 'drive'">从网盘添加</button>
      </div>
      <input v-model="query" type="search" aria-label="搜索图片名称" placeholder="搜索图片名称…" />
      <button class="ghost" type="button" :disabled="loading" @click="load">刷新</button>
    </div>
    <p v-if="mode === 'drive'" class="muted">选择已有图片并点击“发布外链”。仅支持 20 MiB 以内的 JPEG、PNG、GIF、WebP。</p>
    <div v-if="error" class="image-empty" role="alert"><p>{{ error }}</p><button class="ghost" type="button" @click="load">重试</button></div>
    <p v-else-if="loading" class="image-empty" role="status">正在加载图片…</p>
    <div v-else-if="!images.length" class="image-empty">
      <h2>{{ query ? '没有匹配的图片' : mode === 'published' ? '你的第一张外链图片，从这里开始' : '网盘中还没有图片' }}</h2>
      <p class="muted">{{ query ? '试试其他名称。' : '上传后可复制直链、Markdown 或 HTML 引用，随时可以撤销。' }}</p>
    </div>
    <div v-else class="image-grid">
      <article v-for="item in images" :key="item.id" class="image-card">
        <div class="image-preview"><img :src="api.thumbnailUrl(item.id, item.updated_at)" :alt="item.name" loading="lazy" decoding="async" /></div>
        <div class="image-info"><strong :title="item.name">{{ item.name }}</strong><span>{{ formatBytes(item.size) }}</span></div>
        <div class="image-actions">
          <template v-if="mode === 'published'">
            <button class="ghost" type="button" @click="selected = { name: item.name, url: item.url! }">获取链接</button>
            <button class="ghost revoke" type="button" :disabled="!!busy" @click="revokeTarget = item">撤销外链</button>
          </template>
          <button v-else class="ghost" type="button" :disabled="!!busy" @click="publish(item)">{{ busy === item.id ? '正在发布…' : '发布外链' }}</button>
        </div>
      </article>
    </div>
    <footer class="image-pagination">
      <span class="muted">共 {{ total }} 张图片</span>
      <button class="ghost" type="button" :disabled="offset === 0 || loading" @click="offset -= 24">上一页</button>
      <button class="ghost" type="button" :disabled="offset + 24 >= total || loading" @click="offset += 24">下一页</button>
    </footer>
    <BaseModal :open="!!selected" title="图片外链" @close="selected = null">
      <p class="muted">持有链接的人可查看图片。撤销外链后，链接会失效。</p>
      <label class="link-format">引用格式 <select v-model="format"><option value="url">直链 URL</option><option value="markdown">Markdown</option><option value="html">HTML</option></select></label>
      <textarea class="image-link" :value="linkText" rows="4" readonly aria-label="图片外链内容" @focus="($event.target as HTMLTextAreaElement).select()" />
      <template #footer><button class="primary" type="button" @click="copy">复制</button></template>
    </BaseModal>
    <ConfirmDialog :open="!!revokeTarget" title="撤销图片外链" message="撤销后，已有外链和引用将无法访问。原图片仍保存在网盘中。" confirm-text="撤销外链" danger @close="revokeTarget = null" @confirm="revoke" />
  </section>
</template>

<style scoped>
.image-host { width: 100%; max-width: 1320px; margin: 0 auto; padding: 12px; }
.image-heading { display: flex; align-items: center; justify-content: space-between; gap: 16px; margin-bottom: 24px; }
.image-heading h1 { font-size: 30px; margin: 4px 0; letter-spacing: -.03em; }
.image-heading p { margin: 4px 0; }
.eyebrow { font-size: 11px; font-weight: 700; letter-spacing: .12em; color: var(--accent); }
.muted { color: var(--muted); }
.image-drop { display: flex; width: 100%; align-items: center; justify-content: center; flex-direction: column; gap: 10px; padding: 32px 16px; border: 1px dashed #a3b9dc; border-radius: 14px; background: var(--accent-soft); color: var(--text); }
.image-drop:hover,.image-drop.dragging { border-color: var(--accent); background: var(--accent-soft); }
.drop-mark { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 12px; background: var(--panel); color: var(--accent); font-size: 26px; }
.image-drop > span:last-child { color: var(--muted); font-size: 12px; }
.publish-notice { font-size: 12px; color: var(--muted); margin: 12px 0 28px; }
.image-toolbar { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin-bottom: 20px; }
.image-toolbar input { margin-left: auto; width: min(300px,100%); min-width: 180px; border: 1px solid var(--line); padding: 9px 12px; border-radius: 8px; background: var(--panel); }
.image-tabs { display: flex; gap: 4px; }
.image-tabs button { background: transparent; border: 0; padding: 9px 14px; border-radius: 8px; color: var(--muted); }
.image-tabs button[aria-pressed=true] { background: var(--accent-soft); color: var(--accent); font-weight: 700; }
.image-empty { text-align: center; padding: 64px 12px; background: var(--panel); border: 1px solid var(--line); border-radius: 14px; }
.image-empty h2 { font-size: 18px; font-weight: 600; }
.image-grid { display: grid; grid-template-columns: repeat(auto-fill,minmax(225px,1fr)); gap: 20px; }
.image-card { overflow: hidden; border: 1px solid var(--line); border-radius: 12px; background: var(--panel); }
.image-preview { height: 190px; background: var(--panel-2); display: grid; place-items: center; overflow: hidden; }
.image-preview img { max-width: 100%; width: 100%; height: 190px; object-fit: contain; }
.image-info { padding: 14px 14px 6px; display: flex; flex-direction: column; gap: 4px; }
.image-info strong { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
.image-info span { color: var(--muted); font-size: 12px; }
.image-actions { display: flex; justify-content: space-between; padding: 8px 10px 12px; gap: 4px; }
.image-actions .ghost { font-size: 12px; padding: 7px; }
.revoke { color: var(--danger); }
.image-pagination { display: flex; justify-content: flex-end; align-items: center; gap: 12px; padding: 24px 0; }
.image-pagination > span { margin-right: auto; }
.link-format { display: flex; gap: 12px; align-items: center; margin: 16px 0; }
.link-format select { padding: 6px; border: 1px solid var(--line); border-radius: 6px; }
.image-link { width: 100%; padding: 12px; border: 1px solid var(--line); border-radius: 8px; resize: vertical; font: 13px/1.7 monospace; }
@media(max-width:600px) { .image-host { padding: 4px; } .image-toolbar input { order: 3; width: 100%; } .image-grid { grid-template-columns: repeat(2,minmax(0,1fr)); gap: 10px; } .image-preview,.image-preview img { height: 145px; } .image-actions { flex-wrap: wrap; } .image-heading h1 { font-size: 26px; } }
</style>
