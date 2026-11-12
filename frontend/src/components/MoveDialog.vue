<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { BatchItemResult, DriveItem, PathItem } from '@/api/types'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ items: DriveItem[]; initialFolder: string | null }>()
const emit = defineEmits<{ close: []; result: [results: BatchItemResult[], items: DriveItem[]] }>()
const folder = ref<string | null>(null)
const folders = ref<DriveItem[]>([])
const path = ref<PathItem[]>([])
const offset = ref(0)
const total = ref(0)
const pageSize = 50
const loading = ref(false)
const browseFailed = ref(false)
const moving = ref(false)
const error = ref('')
const activeItems = ref(props.items)
const selectedFolders = computed(() => new Set(activeItems.value.filter((item) => item.kind === 'folder').map((item) => item.id)))
const blocked = computed(() => path.value.some((item) => selectedFolders.value.has(item.id)))
const alreadyThere = computed(() => activeItems.value.every((item) => item.parent_id === folder.value))
let request: AbortController | null = null

async function browse(id: string | null, nextOffset = 0): Promise<void> {
  request?.abort()
  const controller = new AbortController()
  request = controller
  loading.value = true
  browseFailed.value = false
  error.value = ''
  try {
    const result = await api.listItems(id, {
      kind: 'folder', scope: 'folder', sort: 'name', limit: pageSize, offset: nextOffset,
    }, controller.signal)
    if (controller.signal.aborted) return
    folder.value = id
    offset.value = nextOffset
    folders.value = result.items
    total.value = result.total
    path.value = result.path
  } catch (cause) {
    if (!controller.signal.aborted) {
      browseFailed.value = true
      error.value = cause instanceof Error ? cause.message : '无法加载目录'
    }
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}

async function move(): Promise<void> {
  if (loading.value || browseFailed.value || moving.value || blocked.value || alreadyThere.value) return
  moving.value = true
  error.value = ''
  const attempted = activeItems.value
  try {
    const { results } = await api.batchItems(attempted.map((item) => item.id), 'move', folder.value)
    emit('result', results, attempted)
    const failed = results.filter((result) => !result.ok)
    if (!failed.length) {
      emit('close')
      return
    }
    activeItems.value = attempted.filter((item) => failed.some((result) => result.id === item.id))
    error.value = failed.map((result) => `${attempted.find((item) => item.id === result.id)?.name}：${result.error}`).join('\n')
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '移动失败'
  } finally {
    moving.value = false
  }
}

onMounted(() => void browse(props.initialFolder))
onBeforeUnmount(() => request?.abort())
</script>

<template>
  <BaseModal :open="true" :title="activeItems.length === 1 ? '移动项目' : `移动 ${activeItems.length} 个项目`" @close="!moving && emit('close')">
    <p v-if="activeItems.length === 1" class="operation-name">{{ activeItems[0]?.name }}</p>
    <nav class="crumbs" aria-label="目标目录">
      <button class="crumb" type="button" :disabled="moving" @click="browse(null)">根目录</button>
      <button v-for="entry in path" :key="entry.id" class="crumb" type="button" :disabled="moving" @click="browse(entry.id)">› {{ entry.name }}</button>
    </nav>
    <p v-if="error" class="inline-error operation-errors" role="alert">{{ error }}</p>
    <p v-if="blocked" class="muted">不能移动到自身或其子目录。</p>
    <p v-else-if="alreadyThere && !loading" class="muted">项目已在此目录。请选择其他目标目录。</p>
    <p v-if="loading" role="status">正在加载目录…</p>
    <ul v-else-if="folders.length" class="destination-list">
      <li v-for="entry in folders" :key="entry.id">
        <button class="ghost" type="button" :disabled="moving || blocked || selectedFolders.has(entry.id)" @click="browse(entry.id)">
          <span>{{ entry.name }}</span><small>{{ selectedFolders.has(entry.id) ? '不能移动到自身' : '›' }}</small>
        </button>
      </li>
    </ul>
    <p v-else-if="!error" class="muted">没有子文件夹</p>
    <div v-if="total > pageSize" class="operation-actions">
      <button class="ghost" :disabled="loading || moving || !offset" @click="browse(folder, offset - pageSize)">上一页</button>
      <button class="ghost" :disabled="loading || moving || offset + pageSize >= total" @click="browse(folder, offset + pageSize)">下一页</button>
    </div>
    <template #footer>
      <button class="ghost" type="button" :disabled="moving" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="loading || browseFailed || moving || blocked || alreadyThere" @click="move">
        {{ folder ? '移动到此目录' : '移动到根目录' }}
      </button>
    </template>
  </BaseModal>
</template>
