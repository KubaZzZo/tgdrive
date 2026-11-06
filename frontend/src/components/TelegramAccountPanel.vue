<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { api, ApiError } from '@/api/client'
import type { TelegramAccountStatus, TelegramChannel, TelegramLogin } from '@/api/types'
import { useAuthStore } from '@/stores/auth'
import { useSyncStore } from '@/stores/sync'
import { useUploadStore } from '@/stores/uploads'
import BaseModal from './BaseModal.vue'

const auth = useAuthStore()
const sync = useSyncStore()
const uploads = useUploadStore()
const open = ref(false)
const account = ref<TelegramAccountStatus | null>(null)
const authRequired = computed(() => sync.status?.state === 'auth_required')
const connected = computed(() => !authRequired.value && (account.value?.connected ?? auth.storage?.connected))
const job = ref<TelegramLogin | null>(null)
const channels = ref<TelegramChannel[]>([])
const chatId = ref('')
const password = ref('')
const error = ref('')
const busy = ref(false)
const loadingChannels = ref(false)
const passwordInput = ref<HTMLInputElement | null>(null)
const channelSelect = ref<HTMLSelectElement | null>(null)
const terminalStates = ['expired', 'canceled', 'error']
const loginActive = computed(() => !!job.value && !terminalStates.includes(job.value.state))
watch(() => job.value?.state, async (state) => {
  await nextTick()
  if (state === 'password') passwordInput.value?.focus()
  if (state === 'ready') channelSelect.value?.focus()
})
let timer: ReturnType<typeof setTimeout> | undefined
let generation = 0
let channelsRequested = false

async function show(): Promise<void> {
  open.value = true
  error.value = ''
  try { account.value = await api.telegramAccounts() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '读取账号失败' }
}
function message(cause: unknown): void { error.value = cause instanceof Error ? cause.message : '操作失败，请重试' }
function updateJob(result: TelegramLogin): void {
  job.value = result
  if (result.state === 'ready' && !channelsRequested) {
    channelsRequested = true
    void loadChannels()
  }
}
async function begin(): Promise<void> {
  busy.value = true
  error.value = ''
  const version = ++generation
  clearTimeout(timer)
  channelsRequested = false
  channels.value = []
  chatId.value = ''
  password.value = ''
  try {
    const result = await api.telegramLogin()
    if (version !== generation) { void api.telegramCancelLogin(result.id).catch(() => {}); return }
    updateJob(result)
    poll(version)
  } catch (cause) { message(cause) }
  finally { busy.value = false }
}
function poll(version: number): void {
  timer = setTimeout(async () => {
    if (!open.value || !job.value || version !== generation) return
    try {
      const result = await api.telegramLoginStatus(job.value.id)
      if (version !== generation) return
      updateJob(result)
    } catch (cause) {
      if (version !== generation) return
      if (cause instanceof ApiError && cause.status === 404 && job.value)
        job.value = { ...job.value, state: 'expired', qr_image: null }
      message(cause)
    }
    if (version === generation && loginActive.value) poll(version)
  }, 2000)
}
async function checkScan(): Promise<void> {
  if (busy.value || job.value?.state !== 'waiting') return
  const id = job.value.id
  const version = ++generation
  clearTimeout(timer)
  busy.value = true
  error.value = ''
  try {
    const result = await api.telegramCheckScan(id)
    if (version === generation) updateJob(result)
  } catch (cause) {
    if (version === generation) message(cause)
  } finally {
    if (version === generation) {
      busy.value = false
      if (loginActive.value) poll(version)
    }
  }
}
async function loadChannels(): Promise<void> {
  if (!job.value || loadingChannels.value) return
  const version = generation
  loadingChannels.value = true
  try {
    const result = await api.telegramChannels(job.value.id)
    if (version === generation) channels.value = result.channels
  } catch (cause) { if (version === generation) message(cause) }
  finally { loadingChannels.value = false }
}
async function submitPassword(): Promise<void> {
  if (busy.value || job.value?.state !== 'password' || !password.value) return
  const id = job.value.id
  const version = ++generation
  clearTimeout(timer)
  busy.value = true
  error.value = ''
  const secret = password.value
  password.value = ''
  try {
    const result = await api.telegramPassword(id, secret)
    if (version === generation) updateJob(result)
  } catch (cause) {
    if (version === generation) message(cause)
  } finally {
    if (version === generation) {
      busy.value = false
      if (loginActive.value) poll(version)
      await nextTick()
      passwordInput.value?.focus()
    }
  }
}
function canSwitch(): boolean {
  if (uploads.jobs.some(j => ['queued', 'uploading', 'processing', 'canceling'].includes(j.state))) {
    error.value = '请先暂停上传队列，并等待正在写入 Telegram 的任务完成。'
    return false
  }
  return true
}
async function activate(profileId?: string): Promise<void> {
  if (!canSwitch() || busy.value || (!profileId && !job.value)) return
  busy.value = true
  error.value = ''
  clearTimeout(timer)
  try {
    if (profileId) await api.telegramSwitch(profileId)
    else await api.telegramActivate(job.value!.id, chatId.value.trim())
    // Reload clears every view, preview, sync poller and queued file reference together.
    window.location.reload()
  } catch (cause) {
    message(cause)
    if (job.value) poll(generation)
  } finally { busy.value = false }
}
async function close(): Promise<void> {
  if (busy.value) return
  generation++
  clearTimeout(timer)
  password.value = ''
  const old = job.value
  job.value = null
  open.value = false
  if (old) { try { await api.telegramCancelLogin(old.id) } catch { /* Expires server-side in 10 minutes. */ } }
}
onBeforeUnmount(() => { generation++; clearTimeout(timer); password.value = '' })
</script>

<template>
  <button class="ghost telegram-button" :class="{ 'telegram-offline': !connected }" @click="show">
    <span class="telegram-status-dot" :class="{ online: connected }" />
    <span class="telegram-label-desktop">Telegram 账号</span><span class="telegram-label-mobile">账号</span>
  </button>
  <BaseModal :open="open" title="Telegram 账号与存储频道" wide @close="close">
    <div class="telegram-panel">
      <p v-if="account"><strong>{{ account.account?.first_name || account.account?.username || '当前账号' }}</strong> · {{ authRequired ? '需要重新登录' : connected ? '已连接' : '未连接' }}<br /><span class="muted">存储频道：{{ account.chat_id || '尚未配置' }}</span></p>
      <p class="muted">每个频道独立保存目录、上传任务和同步记录；切回原频道可继续访问原文件及分享链接。VPS 流量用量始终全局累计。</p>
      <p v-if="error || job?.error" class="form-error" role="alert">{{ error || job?.error }}</p>
      <div v-if="!loginActive && account?.profiles.length" class="telegram-profiles">
        <h3>已保存的连接</h3>
        <div v-for="profile in account.profiles" :key="profile.id" class="telegram-profile">
          <div><strong>{{ profile.first_name || profile.username || 'Telegram 账号' }}</strong><span v-if="profile.username"> @{{ profile.username }}</span><p>{{ profile.title }} <small class="muted">{{ profile.chat_id }}</small></p></div>
          <button class="ghost" :disabled="busy || (account.active_id === profile.id && (connected || authRequired))" @click="activate(profile.id)">{{ account.active_id === profile.id && (connected || authRequired) ? '当前连接' : '切换' }}</button>
        </div>
      </div>
      <button v-if="!loginActive" class="primary" :disabled="busy || !account?.can_login" @click="begin">{{ busy ? '连接中…' : authRequired ? '重新扫码登录' : account?.active_id && !connected ? '重新扫码连接' : '扫码添加账号与频道' }}</button>
      <p v-if="account && !account.can_login" class="form-error">请先配置 TG_API_ID 和 TG_API_HASH 后重启应用。</p>
      <template v-if="job">
        <p v-if="job.state === 'connecting'" role="status">正在连接 Telegram…</p>
        <p v-if="job.state === 'checking'" role="status">正在检查扫码与验证状态…</p>
        <div v-if="job.state === 'waiting' && job.qr_image" class="telegram-qr">
          <img :src="job.qr_image" alt="Telegram 登录二维码" />
          <p>在手机 Telegram 的「设置 → 设备 → 连接桌面设备」中扫码，并在手机上确认。需要两步验证时，这里会显示密码输入框。</p>
          <button class="ghost" type="button" :disabled="busy" @click="checkScan">已扫码，检查验证状态</button>
        </div>
        <form v-if="job.state === 'password'" class="telegram-password" @submit.prevent="submitPassword">
          <p role="status">扫码已确认，请输入你在 Telegram 中设置的两步验证密码。</p>
          <label for="telegram-password">Telegram 两步验证密码</label>
          <input id="telegram-password" ref="passwordInput" v-model="password" type="password" autocomplete="off" maxlength="512" :disabled="busy" required />
          <button class="primary" type="submit" :disabled="busy || !password">{{ busy ? '验证中…' : '验证并继续' }}</button>
          <p class="muted">密码仅用于本次验证。</p>
        </form>
        <div v-if="job.state === 'ready'" class="telegram-channel-picker">
          <p>已登录：<strong>{{ job.account?.first_name || job.account?.username }}</strong>。请选择此账号的存储频道。</p>
          <label for="telegram-channel-select">已加入的频道（最多列出 200 个会话内的频道）</label><select id="telegram-channel-select" ref="channelSelect" v-model="chatId"><option value="">选择频道，或在下方输入</option><option v-for="channel in channels" :key="channel.id" :value="channel.id" :disabled="!channel.writable">{{ channel.title }}{{ channel.writable ? '' : '（缺少管理员权限）' }}</option></select>
          <button class="text-btn" :disabled="loadingChannels" @click="loadChannels">{{ loadingChannels ? '读取中…' : '刷新频道列表' }}</button>
          <label for="telegram-channel-id">频道 ID 或 @用户名</label><input id="telegram-channel-id" v-model="chatId" placeholder="例如 -1001234567890 或 @channel" maxlength="128" />
          <p class="muted">新账号必须拥有频道发送和删除消息权限。确认后全站切换；未完成的上传保留在原频道，现有下载可继续完成。</p>
          <button class="primary" :disabled="busy || !chatId.trim()" @click="activate()">{{ busy ? '验证并切换中…' : '确认切换账号和频道' }}</button>
        </div>
      </template>
      <button v-if="job" class="ghost" :disabled="busy" @click="close">取消本次登录</button>
    </div>
  </BaseModal>
</template>
