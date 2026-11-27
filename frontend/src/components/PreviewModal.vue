<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { DriveItem } from '@/api/types'
import { formatBytes, previewKind } from '@/utils/format'
import BaseModal from '@/components/ui/BaseModal.vue'
import type { PlaybackMode, PlaybackState } from '@/utils/playbackPreferences'

const MediaPlayer = defineAsyncComponent(() => import('@/components/MediaPlayer.vue'))

const props = defineProps<{
  open: boolean; openedAt: number; item: DriveItem | null; downloadPending: boolean; downloadStatus: string
  progressKey: string; initialState?: PlaybackState; directAvailable: boolean
  preferredMode: PlaybackMode | null; defaultMode: PlaybackMode
}>()
const emit = defineEmits<{
  close: []; download: [item: DriveItem]; switchMode: [mode: PlaybackMode, state: PlaybackState]
  defaultMode: [mode: PlaybackMode]; clearPreference: []
}>()
const closeDeadline = defineModel<number>('closeDeadline', { default: 0 })

const failed = ref(false)
watch(
  () => props.item?.id,
  () => {
    failed.value = false
  },
)
const kind = computed(() => (props.item ? previewKind(props.item) : 'none'))
const src = computed(() => (props.item ? api.downloadUrl(props.item.id) : ''))
</script>

<template>
  <BaseModal v-if="item" :open="open" :title="item.name" wide :media="kind === 'video' || kind === 'audio'" @close="emit('close')">
    <MediaPlayer
      v-if="open && (kind === 'video' || kind === 'audio')"
      :key="item.id"
      v-model:close-deadline="closeDeadline"
      :src="src"
      :item-id="item.id"
      :opened-at="openedAt"
      :kind="kind"
      :size="item.size"
      :download-pending="downloadPending"
      :download-status="downloadStatus"
      :poster="kind === 'video' ? api.thumbnailUrl(item.id, item.updated_at) : undefined"
      :mode="kind === 'video' ? 'server' : undefined"
      :progress-key="progressKey" :initial-state="initialState" :direct-available="directAvailable"
      :preferred-mode="preferredMode" :default-mode="defaultMode"
      @switch-mode="(mode, state) => emit('switchMode', mode, state)"
      @default-mode="emit('defaultMode', $event)" @clear-preference="emit('clearPreference')"
      @close="emit('close')"
      @download="emit('download', item)"
    />
    <div v-else-if="open" class="preview-stage">
      <div v-if="failed" class="preview-fallback">浏览器暂不支持预览此格式，请下载后查看。</div>
      <img v-else-if="kind === 'image'" :src="src" :alt="item.name" @error="failed = true" />
      <iframe v-else-if="kind === 'pdf'" :src="src" :title="item.name" />
    </div>
    <template v-if="kind !== 'video' && kind !== 'audio'" #footer>
      <span class="muted">{{ formatBytes(item.size) }}</span>
      <span v-if="downloadStatus" class="muted" role="status">{{ downloadStatus }}</span>
      <button class="primary" type="button" :disabled="downloadPending" @click="emit('download', item)">
        {{ downloadPending ? '正在检查…' : '下载' }}
      </button>
    </template>
  </BaseModal>
</template>

<style scoped>
.preview-stage {
  display: grid;
  place-items: center;
  min-height: 180px;
  background: #101828;
  border-radius: 10px;
  overflow: hidden;
}
.preview-stage :deep(video),
.preview-stage img {
  display: block;
  max-width: 100%;
  max-height: 66dvh;
  margin: 0 auto;
}
.preview-stage :deep(audio) {
  width: 95%;
  margin: 25px 0;
}
.preview-stage iframe {
  width: 100%;
  height: 65dvh;
  border: 0;
  background: var(--panel);
}
.preview-fallback {
  padding: 32px;
  color: #cbd5e1;
  font-size: 13px;
  text-align: center;
}
</style>
