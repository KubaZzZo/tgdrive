<script setup lang="ts">
import { useAuthStore } from '@/stores/auth'
import type { DriveItem, FileAction } from '@/api/types'
import { telegramVideoUrl } from '@/utils/telegramMiniApp'
import AppIcon from '@/components/ui/AppIcon.vue'

defineProps<{ item: DriveItem }>()
const auth = useAuthStore()
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
        <button v-if="telegramVideoUrl(item)" type="button" @click="choose('telegram', $event)">在 Telegram 中播放</button>
        <button type="button" @click="choose('download', $event)">下载文件</button>
        <button type="button" @click="choose('share', $event)">创建分享链接</button>
        <button v-if="auth.features.people && item.category === 'video'" type="button" @click="choose('people', $event)">分配人物标签</button>
      </template>
      <button type="button" @click="choose('rename', $event)">重命名</button>
      <button type="button" @click="choose('move', $event)">移动到…</button>
      <button class="danger-text" type="button" @click="choose('remove', $event)">
        移入回收站
      </button>
    </div>
  </details>
</template>

<style scoped>
.file-menu {
  position: relative;
}
.file-menu summary {
  display: grid;
  place-items: center;
  width: 34px;
  height: 34px;
  border-radius: 6px;
  list-style: none;
  color: var(--muted);
}
.file-menu summary::-webkit-details-marker {
  display: none;
}
.file-menu summary:hover,
.file-menu[open] summary {
  background: var(--accent-soft);
  color: var(--accent);
}
.file-menu[open] {
  z-index: 15;
}
.file-menu-options {
  position: absolute;
  top: calc(100% + 3px);
  right: 0;
  width: 162px;
  padding: 5px;
  border: 1px solid var(--line);
  border-radius: 9px;
  box-shadow: var(--shadow);
  background: var(--panel);
}
.file-menu-options button {
  display: block;
  padding: 10px 12px;
  min-height: 42px;
  width: 100%;
  border: 0;
  border-radius: 5px;
  background: transparent;
  font-size: 12px;
  text-align: left;
}
.file-menu-options button:hover {
  background: var(--bg);
}
@media (max-width: 760px) {
  .file-menu summary {
    width: 40px;
    height: 44px;
  }
  .file-menu-options button {
    min-height: 44px;
  }
}
</style>
