<script setup lang="ts">
import type { DriveItem, FileAction } from '@/api/types'
import AppIcon from './AppIcon.vue'

defineProps<{ item: DriveItem }>()
const emit = defineEmits<{ action: [action: FileAction] }>()
function choose(action: FileAction, event: Event): void {
  ;(event.target as HTMLElement).closest('details')?.removeAttribute('open')
  emit('action', action)
}
</script>
<template>
  <details
    class="file-menu"
    name="file-actions"
    @keydown.esc="($event.currentTarget as HTMLDetailsElement).open = false"
  >
    <summary :aria-label="`${item.name}的更多操作`">
      <AppIcon name="more" />
    </summary>
    <div class="file-menu-options">
      <template v-if="item.kind === 'file'">
        <button type="button" @click="choose('download', $event)">下载文件</button>
        <button type="button" @click="choose('share', $event)">创建分享链接</button>
      </template>
      <button type="button" @click="choose('rename', $event)">重命名</button>
      <button class="danger-text" type="button" @click="choose('remove', $event)">
        删除{{ item.kind === 'folder' ? '文件夹' : '文件' }}
      </button>
    </div>
  </details>
</template>
