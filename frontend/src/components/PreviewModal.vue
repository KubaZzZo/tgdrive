<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { DriveItem } from '@/api/types'
import { formatBytes, previewKind } from '@/utils/format'
import BaseModal from './BaseModal.vue'
import MediaPlayer from './MediaPlayer.vue'

const props = defineProps<{ open: boolean; item: DriveItem | null }>()
const emit = defineEmits<{ close: []; download: [item: DriveItem] }>()

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
  <BaseModal v-if="item" :open="open" :title="item.name" wide @close="emit('close')">
    <MediaPlayer
      v-if="open && (kind === 'video' || kind === 'audio')"
      :key="item.id"
      :src="src"
      :item-id="item.id"
      :kind="kind"
    />
    <div v-else-if="open" class="preview-stage">
      <div v-if="failed" class="preview-fallback">浏览器暂不支持预览此格式，请下载后查看。</div>
      <img v-else-if="kind === 'image'" :src="src" :alt="item.name" @error="failed = true" />
      <iframe v-else-if="kind === 'pdf'" :src="src" :title="item.name" />
    </div>
    <template #footer>
      <span class="muted">{{ formatBytes(item.size) }}</span>
      <button class="primary" type="button" @click="emit('download', item)">下载</button>
    </template>
  </BaseModal>
</template>
