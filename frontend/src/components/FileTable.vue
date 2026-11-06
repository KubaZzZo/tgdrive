<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import type { DriveItem, FileAction } from '@/api/types'
import { formatBytes, formatDate } from '@/utils/format'
import { categoryLabel } from '@/utils/categories'
import AppIcon from './AppIcon.vue'
import FileActions from './FileActions.vue'
import VideoCover from './VideoCover.vue'

withDefaults(defineProps<{ items: DriveItem[]; layout?: 'list' | 'grid' }>(), {
  layout: 'list',
})
const emit = defineEmits<{
  open: [item: DriveItem]
  preview: [item: DriveItem]
  action: [action: FileAction, item: DriveItem]
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
        <strong>{{ item.name }}</strong>
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
              <span
                class="file-art small"
                :class="item.kind === 'folder' ? 'folder' : item.category"
                ><VideoCover v-if="item.category === 'video'" :key="item.updated_at" :item="item" /><AppIcon v-else
                  :name="item.kind === 'folder' ? 'folder' : (item.category ?? 'document')"
                  :size="22"
              /></span>
              <div class="file-name-block">
                <button class="name-btn" type="button" :title="item.name" @click="open(item)">
                  {{ item.name }}
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
