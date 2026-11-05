<script setup lang="ts">
import { onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { DriveItem, ShareRecord } from '@/api/types'
import { useToastStore } from '@/stores/toast'
import { formatDate } from '@/utils/format'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ item: DriveItem }>()
const emit = defineEmits<{ close: [] }>()
const toast = useToastStore()
const links = ref<ShareRecord[]>([])
const expiresHours = ref(24)
const ready = ref(false)
const busy = ref(false)
const error = ref('')
const confirming = ref<string | null>(null)

async function load(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    links.value = (await api.listShares(props.item.id)).shares
    ready.value = true
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '无法读取分享链接'
  } finally {
    busy.value = false
  }
}

async function create(): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    const link = await api.createShare(props.item.id, expiresHours.value)
    links.value.unshift(link)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '创建分享链接失败'
  } finally {
    busy.value = false
  }
}

async function revoke(token: string): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    await api.revokeShare(props.item.id, token)
    links.value = links.value.filter(link => link.token !== token)
    confirming.value = null
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '撤销分享链接失败'
  } finally {
    busy.value = false
  }
}

async function copy(url: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(url)
    toast.success('分享链接已复制')
  } catch {
    window.prompt('分享链接', url)
  }
}

onMounted(load)
</script>

<template>
  <BaseModal open :title="`管理分享链接 · ${item.name}`" @close="emit('close')">
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-if="!ready && busy" role="status">正在加载分享链接…</p>
    <button v-else-if="!ready" class="ghost" type="button" @click="load">重试</button>
    <template v-else>
      <form class="share-actions" @submit.prevent="create">
        <label>
          有效期
          <select v-model.number="expiresHours" :disabled="busy">
            <option :value="1">1 小时</option>
            <option :value="24">24 小时</option>
            <option :value="168">7 天</option>
            <option :value="0">永久有效</option>
          </select>
        </label>
        <button class="primary" type="submit" :disabled="busy">创建链接</button>
      </form>
      <p v-if="links.length === 0" class="muted">此文件没有有效分享链接。</p>
      <ul v-else class="share-links" aria-label="有效分享链接">
        <li v-for="link in links" :key="link.token">
          <input :value="link.url" aria-label="分享链接" readonly @focus="($event.target as HTMLInputElement).select()" />
          <div class="share-actions">
            <small class="muted">{{ link.expires_at ? `${formatDate(link.expires_at)} 到期` : '永久有效' }}</small>
            <template v-if="confirming === link.token">
              <button class="ghost" type="button" :disabled="busy" @click="confirming = null">取消</button>
              <button class="ghost danger-text" type="button" :disabled="busy" @click="revoke(link.token)">确认撤销</button>
            </template>
            <template v-else>
              <button class="ghost" type="button" @click="copy(link.url)">复制链接</button>
              <button class="ghost danger-text" type="button" :disabled="busy" @click="confirming = link.token">撤销</button>
            </template>
          </div>
        </li>
      </ul>
    </template>
  </BaseModal>
</template>

<style scoped>
.share-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}
.share-actions small {
  flex: 1 0 auto;
}
.share-links {
  display: grid;
  gap: 12px;
  padding: 0;
  margin-bottom: 0;
  list-style: none;
}
.share-links li {
  display: grid;
  gap: 6px;
  min-width: 0;
}
.share-links input {
  width: 100%;
  min-width: 0;
}
</style>
