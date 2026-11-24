<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { DriveItem, FileAction } from '@/api/types'
import { formatBytes, formatDate } from '@/utils/format'
import { categoryLabel } from '@/utils/categories'
import AppIcon from '@/components/ui/AppIcon.vue'
import FileActions from '@/components/FileActions.vue'
import VideoCover from '@/components/VideoCover.vue'

withDefaults(defineProps<{ items: DriveItem[]; layout?: 'list' | 'grid'; selected?: Set<string>; selecting?: boolean }>(), {
  layout: 'list',
})
const emit = defineEmits<{
  open: [item: DriveItem]
  preview: [item: DriveItem]
  action: [action: FileAction, item: DriveItem]
  select: [id: string]
}>()
const root = ref<HTMLElement | null>(null)
function closeMenus(event: PointerEvent): void {
  root.value?.querySelectorAll('details[open]').forEach((menu) => {
    if (!menu.contains(event.target as Node)) menu.removeAttribute('open')
  })
}
onMounted(() => document.addEventListener('pointerdown', closeMenus))
onBeforeUnmount(() => document.removeEventListener('pointerdown', closeMenus))
function open(item: DriveItem): void {
  item.kind === 'folder' ? emit('open', item) : emit('preview', item)
}
</script>
<template>
  <div v-if="layout === 'grid'" ref="root" class="file-grid">
    <article v-for="item in items" :key="item.id" class="file-card">
      <div class="file-card-top">
        <input v-if="selecting" type="checkbox" class="file-selection" :aria-label="`选择 ${item.name}`" :checked="selected?.has(item.id)" @change="emit('select', item.id)" />
        <span class="type-label">{{
          item.kind === 'folder' ? '文件夹' : categoryLabel(item.category)
        }}</span>
        <FileActions :item="item" @action="emit('action', $event, item)" />
      </div>
      <button class="file-card-open" type="button" :title="item.name" @click="open(item)">
        <span class="file-art" :class="item.kind === 'folder' ? 'folder' : item.category"
          ><VideoCover v-if="item.category === 'video'" :key="item.updated_at" :item="item" /><AppIcon v-else
            :name="item.kind === 'folder' ? 'folder' : (item.category ?? 'document')"
            :size="40"
        /></span>
        <span class="file-card-name">
          <strong class="file-title">{{ item.name }}</strong>
        </span>
      </button>
      <p>
        {{ item.kind === 'folder' ? '文件夹' : formatBytes(item.size) }} ·
        {{ formatDate(item.created_at).split(' ')[0] }}
      </p>
    </article>
  </div>
  <div v-else ref="root" class="table-wrap">
    <table class="files-table">
      <thead>
        <tr>
          <th>文件名称</th>
          <th>类型</th>
          <th>大小</th>
          <th>上传时间</th>
          <th><span class="sr-only">操作</span></th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td>
            <div class="name-cell">
              <input v-if="selecting" type="checkbox" class="file-selection" :aria-label="`选择 ${item.name}`" :checked="selected?.has(item.id)" @change="emit('select', item.id)" />
              <span
                class="file-art small"
                :class="item.kind === 'folder' ? 'folder' : item.category"
                ><VideoCover v-if="item.category === 'video'" :key="item.updated_at" :item="item" /><AppIcon v-else
                  :name="item.kind === 'folder' ? 'folder' : (item.category ?? 'document')"
                  :size="22"
              /></span>
              <div class="file-name-block">
                <button class="name-btn" type="button" :title="item.name" @click="open(item)">
                  <span class="file-title">{{ item.name }}</span>
                </button>
                <span class="mobile-meta">{{
                  item.kind === 'folder'
                    ? '文件夹'
                    : `${categoryLabel(item.category)} · ${formatBytes(item.size)}`
                }}</span>
              </div>
            </div>
          </td>
          <td class="meta-type">
            {{ item.kind === 'folder' ? '文件夹' : categoryLabel(item.category)
            }}<span v-if="item.source === 'channel'" class="source-badge">频道</span>
          </td>
          <td class="meta-size">
            {{ item.kind === 'folder' ? '—' : formatBytes(item.size) }}
          </td>
          <td class="meta-time">{{ formatDate(item.created_at) }}</td>
          <td class="actions-col">
            <FileActions :item="item" @action="emit('action', $event, item)" />
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>

<style scoped>
.file-selection {
  flex: 0 0 auto;
  width: 17px;
  height: 17px;
  margin: 0;
  accent-color: var(--accent);
}
.table-wrap {
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 11px;
}
.files-table {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
}
.files-table th {
  text-align: left;
  color: var(--muted);
  font-size: 11px;
  font-weight: 400;
  padding: 14px 18px;
  background: var(--panel-2);
  border-bottom: 1px solid var(--line);
}
.files-table th:first-child {
  width: 44%;
  border-radius: 11px 0 0 0;
}
.files-table th:nth-child(2) {
  width: 12%;
}
.files-table th:nth-child(3) {
  width: 12%;
}
.files-table th:nth-child(4) {
  width: 25%;
}
.files-table td {
  padding: 12px 18px;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
}
.files-table tr:last-child td {
  border-bottom: 0;
}
.files-table tbody tr:hover {
  background: var(--panel-2);
}
.name-cell {
  display: flex;
  align-items: center;
  gap: 12px;
  min-width: 0;
}
.file-name-block {
  min-width: 0;
}
.name-btn {
  display: block;
  max-width: 100%;
  padding: 9px 0;
  border: 0;
  background: transparent;
  color: var(--text);
  text-align: left;
  font-size: 12px;
}
.file-title {
  display: block;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.name-btn:hover {
  color: var(--accent);
}
.meta-type,
.meta-size,
.meta-time {
  color: var(--muted);
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}
.mobile-meta {
  display: none;
}
.actions-col {
  width: 7%;
}
.file-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 16px;
}
.file-card {
  min-width: 0;
  border: 1px solid var(--line);
  border-radius: 11px;
  background: var(--panel);
  padding: 12px 15px 16px;
}
.file-card:hover {
  border-color: #c5d6f7;
}
.file-card-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.type-label {
  font-size: 10px;
  color: var(--muted);
}
.file-card-open {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 20px;
  width: 100%;
  padding: 12px 0 0;
  border: 0;
  background: transparent;
}
.file-card-open .file-art {
  width: 78px;
  height: 78px;
  border-radius: 18px;
}
.file-card-name {
  max-width: 100%;
}
.file-card-name strong {
  font-weight: 500;
  font-size: 12px;
}
.file-card > p {
  margin: 7px 0 0;
  text-align: center;
  font-size: 10px;
  color: var(--muted);
}
@media (max-width: 1100px) {
  .files-table th:nth-child(4),
  .files-table td:nth-child(4) {
    display: none;
  }
  .files-table th:first-child {
    width: 58%;
  }
  .files-table th:nth-child(3) {
    width: 20%;
  }
}
@media (max-width: 760px) {
  .files-table th,
  .files-table td {
    padding: 12px;
  }
  .files-table th:nth-child(2),
  .files-table td:nth-child(2),
  .files-table th:nth-child(3),
  .files-table td:nth-child(3) {
    display: none;
  }
  .files-table th:first-child {
    width: auto;
  }
  .files-table th:last-child {
    width: 60px;
  }
  .name-cell {
    gap: 10px;
  }
  .name-btn {
    font-size: 13px;
    padding: 0 0 5px;
    min-height: 28px;
  }
  .mobile-meta {
    display: block;
    font-size: 10px;
    color: var(--muted);
  }
  .file-grid {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 10px;
  }
  .file-card {
    padding: 10px 12px 15px;
  }
  .file-card-open {
    gap: 17px;
  }
  .file-card-open .file-art {
    width: 62px;
    height: 62px;
    border-radius: 14px;
  }
  .file-card-open :deep(.app-icon) {
    width: 32px;
    height: 32px;
  }
}
.source-badge {
  display: inline-block;
  margin-left: 6px;
  color: var(--muted);
  background: var(--accent-soft);
  padding: 1px 5px;
  border-radius: 4px;
  font-size: 10px;
}
</style>
