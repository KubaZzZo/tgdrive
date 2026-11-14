<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { TrashItem } from '@/api/types'
import { useToastStore } from '@/stores/toast'
import { formatDate } from '@/utils/format'
import BaseModal from '@/components/ui/BaseModal.vue'

const emit = defineEmits<{ close: []; restored: [] }>()
const items = ref<TrashItem[]>([])
const busy = ref(false)
const error = ref('')
const pending = ref<TrashItem | 'empty' | null>(null)
const total = ref(0)
const offset = ref(0)
const retention = ref<number | null>(null)
const pageSize = 30
const toast = useToastStore()

async function load(): Promise<void> {
  const result = await api.listTrash(offset.value)
  if (!result.items.length && offset.value > 0) {
    offset.value = Math.max(0, Math.floor((result.total - 1) / pageSize) * pageSize)
    await load()
    return
  }
  items.value = result.items
  total.value = result.total
  retention.value = result.retention_days
}

async function run(operation: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '回收站操作失败'
  } finally {
    busy.value = false
  }
}

async function restore(item: TrashItem): Promise<void> {
  const restored = await api.restoreItem(item.id)
  toast.success(item.trash_parent_id && !restored.parent_id ? '原目录不可用，已恢复到根目录' : '已恢复')
  emit('restored')
  items.value = items.value.filter((entry) => entry.id !== item.id)
  total.value--
  await load()
}

async function purge(): Promise<void> {
  const target = pending.value
  if (!target) return
  if (target === 'empty') {
    await api.emptyTrash()
    toast.success('回收站已清空')
    items.value = []
    total.value = offset.value = 0
  } else {
    const result = await api.purgeItem(target.id)
    if (result.warning) toast.push(result.warning)
    else toast.success('已永久删除')
    items.value = items.value.filter((entry) => entry.id !== target.id)
    total.value--
  }
  pending.value = null
  await load()
}

function changePage(next: number): void {
  void run(async () => {
    const previous = offset.value
    offset.value = next
    try { await load() } catch (cause) { offset.value = previous; throw cause }
  })
}

onMounted(() => void run(load))
</script>

<template>
  <BaseModal :open="true" title="回收站" wide @close="!busy && emit('close')">
    <p v-if="retention" class="muted">项目保留 {{ retention }} 天。原目录不可用时，项目会恢复到根目录。</p>
    <div v-if="error" class="inline-error" role="alert">{{ error }}</div>
    <p v-if="busy && !items.length" role="status">正在加载…</p>
    <p v-else-if="!error && !items.length" class="muted">回收站为空</p>
    <div v-if="pending" class="operation-actions">
      <strong>{{ pending === 'empty' ? '清空整个回收站？' : `永久删除「${pending.name}」？` }} 此操作无法撤销。</strong>
      <button class="ghost" :disabled="busy" @click="pending = null">取消</button>
      <button class="primary danger" :disabled="busy" @click="run(purge)">确认永久删除</button>
    </div>
    <button v-else-if="total" class="ghost danger-text" :disabled="busy" @click="pending = 'empty'">清空回收站</button>
    <ul v-if="items.length" class="operation-list">
      <li v-for="item in items" :key="item.id">
        <strong class="operation-name">{{ item.name }}</strong>
        <small class="muted">{{ item.kind === 'folder' ? '文件夹' : '文件' }} · {{ formatDate(item.trashed_at) }} 移入</small>
        <div class="operation-actions">
          <button class="ghost" :disabled="busy || Boolean(pending)" @click="run(() => restore(item))">恢复</button>
          <button class="ghost danger-text" :disabled="busy || Boolean(pending)" @click="pending = item">永久删除</button>
        </div>
      </li>
    </ul>
    <div v-if="total > pageSize" class="operation-actions">
      <button class="ghost" :disabled="busy || Boolean(pending) || offset === 0" @click="changePage(offset - pageSize)">上一页</button>
      <span>{{ Math.floor(offset / pageSize) + 1 }} / {{ Math.ceil(total / pageSize) }}</span>
      <button class="ghost" :disabled="busy || Boolean(pending) || offset + pageSize >= total" @click="changePage(offset + pageSize)">下一页</button>
    </div>
  </BaseModal>
</template>
