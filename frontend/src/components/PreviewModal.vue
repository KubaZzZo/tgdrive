<script setup lang="ts">
import { computed } from 'vue'

import { api } from '@/api/client'
import type { DriveItem } from '@/api/types'
import { formatBytes, previewKind } from '@/utils/format'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ open: boolean; item: DriveItem | null }>()
const emit = defineEmits<{ close: []; download: [item: DriveItem] }>()

const kind = computed(() => (props.item ? previewKind(props.item) : 'none'))
const src = computed(() => (props.item ? api.downloadUrl(props.item.id) : ''))
</script>

<template>
  <BaseModal
    v-if="item"
    :open="open"
    :title="item.name"
    wide
    @close="emit('close')"
  >
    <div class="preview-stage">
      <video v-if="kind === 'video'" :src="src" controls autoplay playsinline />
      <img v-else-if="kind === 'image'" :src="src" :alt="item.name" />
      <audio v-else-if="kind === 'audio'" :src="src" controls autoplay />
    </div>
    <template #footer>
      <span class="muted">{{ formatBytes(item.size) }}</span>
      <button class="primary" type="button" @click="emit('download', item)">下载</button>
    </template>
  </BaseModal>
</template>
