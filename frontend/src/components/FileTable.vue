<script setup lang="ts">
import type { DriveItem } from '@/api/types'
import { formatBytes, formatDate, iconFor } from '@/utils/format'

defineProps<{ items: DriveItem[] }>()
const emit = defineEmits<{
  open: [item: DriveItem]
  preview: [item: DriveItem]
  download: [item: DriveItem]
  share: [item: DriveItem]
  rename: [item: DriveItem]
  remove: [item: DriveItem]
}>()
</script>

<template>
  <div class="table-wrap">
    <table class="files-table">
      <thead>
        <tr>
          <th>名称</th>
          <th>大小</th>
          <th>修改时间</th>
          <th class="actions-col">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="item in items" :key="item.id">
          <td>
            <div class="name-cell">
              <span class="file-icon">{{ iconFor(item) }}</span>
              <button
                class="name-btn"
                type="button"
                :title="item.name"
                @click="item.kind === 'folder' ? emit('open', item) : emit('preview', item)"
              >
                {{ item.name }}
              </button>
            </div>
          </td>
          <td class="meta-size">{{ item.kind === 'folder' ? '—' : formatBytes(item.size) }}</td>
          <td class="meta-time">{{ formatDate(item.updated_at) }}</td>
          <td class="actions-col">
            <div class="row-actions">
              <template v-if="item.kind === 'file'">
                <button class="icon-btn" type="button" title="下载" @click="emit('download', item)">
                  下载
                </button>
                <button class="icon-btn" type="button" title="分享" @click="emit('share', item)">
                  分享
                </button>
              </template>
              <button class="icon-btn" type="button" title="重命名" @click="emit('rename', item)">
                改名
              </button>
              <button
                class="icon-btn danger"
                type="button"
                title="删除"
                @click="emit('remove', item)"
              >
                删除
              </button>
            </div>
          </td>
        </tr>
      </tbody>
    </table>
  </div>
</template>
