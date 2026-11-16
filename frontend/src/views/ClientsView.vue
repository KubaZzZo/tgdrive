<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { api } from '@/api/client'
import type { ClientCredential, ClientScope } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useToastStore } from '@/stores/toast'

const auth = useAuthStore()
const toast = useToastStore()
const credentials = ref<ClientCredential[]>([])
const channelId = ref('')
const scope = ref<ClientScope>('webdav')
const label = ref('')
const busy = ref(false)
const error = ref('')
const reveal = ref<(ClientCredential & { secret: string }) | null>(null)
const labels = { webdav: '只读 WebDAV', picgo: 'PicGo 图床' }
const mountUrl = `${window.location.origin}/webdav/`
const picgoUrl = `${window.location.origin}/api/picgo/upload`
const backupUrl = computed(() =>
  `/api/clients/index-backup?workspace_id=${encodeURIComponent(channelId.value)}`,
)

async function manage(operation: () => Promise<void>): Promise<void> {
  busy.value = true
  error.value = ''
  try {
    await operation()
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '请求失败，请重试'
  } finally {
    busy.value = false
  }
}

onMounted(() => void manage(async () => {
  const data = await api.clientCredentials()
  credentials.value = data.credentials
  channelId.value = data.channel_id
}))

async function create(): Promise<void> {
  if (!label.value.trim() || busy.value) return
  await manage(async () => {
    const { secret, ...credential } = await api.addClientCredential(scope.value, label.value.trim())
    credentials.value.unshift(credential)
    reveal.value = { ...credential, secret }
    label.value = ''
  })
}

async function revoke(credential: ClientCredential): Promise<void> {
  if (busy.value || !window.confirm(`撤销「${credential.label}」？`)) return
  await manage(async () => {
    await api.revokeClientCredential(credential.id)
    credentials.value = credentials.value.filter((item) => item.id !== credential.id)
    if (reveal.value?.id === credential.id) reveal.value = null
  })
}

async function copySecret(): Promise<void> {
  if (!reveal.value) return
  const value = reveal.value.scope === 'picgo' ? `Bearer ${reveal.value.secret}` : reveal.value.secret
  try {
    await navigator.clipboard.writeText(value)
    toast.success('已复制')
  } catch {
    toast.error('复制失败，请手动复制')
  }
}
</script>

<template>
  <main class="clients-page">
    <h1>客户端与备份</h1>
    <p v-if="channelId" class="muted">当前频道 {{ channelId }}</p>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>

    <section class="client-card">
      <h2>应用凭据</h2>
      <p class="muted">凭据绑定当前频道。切换频道后，切回原频道即可继续使用。</p>
      <form class="client-form" @submit.prevent="create">
        <label>
          客户端
          <select v-model="scope">
            <option value="webdav">只读 WebDAV</option>
            <option v-if="auth.features.images" value="picgo">PicGo 图床</option>
          </select>
        </label>
        <label>
          凭据名称
          <input v-model="label" maxlength="80" placeholder="例如笔记本" required />
        </label>
        <button class="primary" :disabled="busy || !channelId || !label.trim()">生成凭据</button>
      </form>

      <div v-if="reveal" class="secret-panel" aria-live="polite">
        <strong>{{ labels[reveal.scope] }}凭据只显示一次</strong>
        <template v-if="reveal.scope === 'picgo'">
          <p>上传地址 <code>{{ picgoUrl }}</code></p>
          <p>表单字段 <code>file</code> · 返回路径 <code>data.downloadLink</code></p>
          <p>请求头 <code>Authorization: Bearer {{ reveal.secret }}</code></p>
        </template>
        <template v-else>
          <p>挂载地址 <code>{{ mountUrl }}</code></p>
          <p>用户名 <code>tgdrive</code> · 密码 <code>{{ reveal.secret }}</code></p>
        </template>
        <button type="button" class="ghost" @click="copySecret">复制凭据</button>
        <button type="button" class="ghost" @click="reveal = null">关闭</button>
      </div>

      <ul v-if="credentials.length" class="credential-list">
        <li v-for="credential in credentials" :key="credential.id">
          <span><strong>{{ credential.label }}</strong> · {{ labels[credential.scope] }}</span>
          <button type="button" class="ghost" :disabled="busy" @click="revoke(credential)">撤销</button>
        </li>
      </ul>
      <p v-else-if="!busy && channelId" class="muted">当前频道没有应用凭据</p>
    </section>

    <section class="client-card">
      <h2>频道索引备份</h2>
      <p class="muted">导出目录、文件分段索引、分享链接、图床和人物标签。请妥善保存 ZIP，里面包含现有的分享令牌。</p>
      <a v-if="channelId" :href="backupUrl" class="ghost backup-link">下载当前频道索引</a>
    </section>
  </main>
</template>

<style scoped>
.clients-page { width: min(100% - 32px, 850px); margin: 28px auto; }
.clients-page h1 { margin: 0; }
.client-card { background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 20px; margin-top: 18px; }
.client-card h2 { margin: 0; font-size: 18px; }
.client-form { display: flex; flex-wrap: wrap; align-items: end; gap: 12px; }
.client-form label { display: grid; gap: 6px; }
.client-form input, .client-form select { width: 100%; min-height: 38px; }
.secret-panel { margin-top: 16px; padding: 12px; border-radius: 8px; background: var(--panel-2); overflow-wrap: anywhere; }
.credential-list { list-style: none; padding: 0; margin-bottom: 0; }
.credential-list li { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 8px 0; border-top: 1px solid var(--line); overflow-wrap: anywhere; }
.backup-link { text-decoration: none; }
</style>
