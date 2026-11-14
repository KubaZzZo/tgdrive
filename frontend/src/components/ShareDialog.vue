<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { DriveItem, ManagedShare, ShareRecord } from '@/api/types'
import { useToastStore } from '@/stores/toast'
import { formatDate } from '@/utils/format'
import BaseModal from '@/components/ui/BaseModal.vue'

const props = defineProps<{ item?: DriveItem }>()
const emit = defineEmits<{ close: [] }>()
const toast = useToastStore()
const options = [
  { hours: 1, label: '1 小时' },
  { hours: 24, label: '1 天' },
  { hours: 168, label: '7 天' },
  { hours: 720, label: '30 天' },
  { hours: 0, label: '永久有效' },
]
const hours = ref(24)
const created = ref<ShareRecord | null>(null)
const shares = ref<ManagedShare[]>([])
const editing = ref<string | null>(null)
const revoking = ref<string | null>(null)
const busy = ref(false)
const error = ref('')
const offset = ref(0)
const total = ref(0)
const pageSize = 30

async function load(): Promise<void> {
  const data = await api.listShares(offset.value)
  if (!data.shares.length && offset.value > 0) {
    offset.value = Math.max(0, Math.floor((data.total - 1) / pageSize) * pageSize)
    await load()
    return
  }
  shares.value = data.shares
  total.value = data.total
}

async function run(operation: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '分享操作失败'
  } finally {
    busy.value = false
  }
}

async function create(): Promise<void> {
  if (props.item) created.value = await api.createShare(props.item.id, hours.value)
}

async function update(share: ManagedShare): Promise<void> {
  const result = await api.updateShare(share.token, hours.value)
  share.expires_at = result.expires_at
  editing.value = null
  toast.success('有效期已更新')
}

async function revoke(token: string): Promise<void> {
  await api.revokeShare(token)
  revoking.value = null
  shares.value = shares.value.filter((share) => share.token !== token)
  total.value--
  toast.success('分享链接已撤销')
  await load()
}

async function copy(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url)
    toast.success('分享链接已复制')
  } catch {
    error.value = '无法自动复制，请手动复制链接'
  }
}

function changePage(next: number): void {
  void run(async () => {
    const previous = offset.value
    offset.value = next
    editing.value = revoking.value = null
    try { await load() } catch (cause) { offset.value = previous; throw cause }
  })
}

onMounted(() => { if (!props.item) void run(load) })
</script>

<template>
  <BaseModal :open="true" :title="item ? '分享设置' : '我的分享'" :wide="!item" @close="!busy && emit('close')">
    <div v-if="error" class="inline-error" role="alert">{{ error }}</div>
    <template v-if="item">
      <p class="operation-name">{{ item.name }}</p>
      <form v-if="!created" class="operation-form" @submit.prevent="run(create)">
        <label>链接有效期
          <select v-model.number="hours" :disabled="busy">
            <option v-for="option in options" :key="option.hours" :value="option.hours">{{ option.label }}</option>
          </select>
        </label>
        <button class="primary" :disabled="busy">创建链接</button>
      </form>
      <template v-else>
        <label class="operation-link">分享链接<input readonly :value="created.url" @focus="($event.target as HTMLInputElement).select()" /></label>
        <label class="operation-link">外部播放器直链<input readonly :value="created.stream_url" @focus="($event.target as HTMLInputElement).select()" /></label>
        <p class="muted">{{ created.expires_at ? `有效期至 ${formatDate(created.expires_at)}` : '永久有效' }}</p>
        <button class="primary" type="button" @click="copy(created.url)">复制链接</button>
      </template>
    </template>
    <template v-else>
      <p v-if="busy && !shares.length" role="status">正在加载…</p>
      <p v-else-if="!error && !shares.length" class="muted">暂无分享链接</p>
      <ul v-if="shares.length" class="operation-list">
        <li v-for="share in shares" :key="share.token">
          <strong class="operation-name">{{ share.name }}</strong>
          <small class="muted">
            {{ share.suspended ? '回收站中 · 链接暂停' : share.expires_at && Date.parse(share.expires_at) <= Date.now() ? '已过期' : '可访问' }}
            · {{ share.expires_at ? formatDate(share.expires_at) : '永久有效' }}
          </small>
          <input class="operation-link" readonly :value="share.url" :aria-label="`${share.name}的分享链接`" @focus="($event.target as HTMLInputElement).select()" />
          <div v-if="revoking === share.token" class="operation-actions">
            <span>撤销后，此链接将立即失效。</span>
            <button class="ghost" :disabled="busy" @click="revoking = null">取消</button>
            <button class="primary danger" :disabled="busy" @click="run(() => revoke(share.token))">确认撤销</button>
          </div>
          <form v-else-if="editing === share.token" class="operation-form" @submit.prevent="run(() => update(share))">
            <label>从现在起
              <select v-model.number="hours" :disabled="busy">
                <option v-for="option in options" :key="option.hours" :value="option.hours">{{ option.label }}</option>
              </select>
            </label>
            <button class="ghost" type="button" :disabled="busy" @click="editing = null">取消</button>
            <button class="primary" :disabled="busy">保存</button>
          </form>
          <div v-else class="operation-actions">
            <button class="ghost" :disabled="busy" @click="copy(share.url)">复制</button>
            <button class="ghost" :disabled="busy || !!share.suspended" @click="copy(share.stream_url)">复制播放直链</button>
            <button class="ghost" :disabled="busy" @click="editing = share.token; revoking = null; hours = 24">修改有效期</button>
            <button class="ghost danger-text" :disabled="busy" @click="revoking = share.token; editing = null">撤销</button>
          </div>
        </li>
      </ul>
      <div v-if="total > pageSize" class="operation-actions">
        <button class="ghost" :disabled="busy || offset === 0" @click="changePage(offset - pageSize)">上一页</button>
        <span>{{ Math.floor(offset / pageSize) + 1 }} / {{ Math.ceil(total / pageSize) }}</span>
        <button class="ghost" :disabled="busy || offset + pageSize >= total" @click="changePage(offset + pageSize)">下一页</button>
      </div>
    </template>
  </BaseModal>
</template>
