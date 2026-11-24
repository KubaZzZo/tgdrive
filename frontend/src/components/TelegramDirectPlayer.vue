<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { BaseTelegramClient } from '@mtcute/web'
import { api } from '@/api/client'
import type { DriveItem } from '@/api/types'
import BaseModal from '@/components/ui/BaseModal.vue'
import { clearTelegramDirectSession, readTelegramDirectSession, revokeTelegramDirectSession, saveTelegramDirectSession } from '@/utils/telegramDirectSession'
import { openTelegramMedia, prepareTelegramMedia, untilAborted } from '@/utils/telegramMedia'
import { openTelegramVideo } from '@/utils/telegramMiniApp'

const MediaPlayer = defineAsyncComponent(() => import('@/components/MediaPlayer.vue'))
const props = defineProps<{ item: DriveItem }>()
const emit = defineEmits<{ close: []; serverFallback: [] }>()

const stage = ref<'opening' | 'credentials' | 'code' | 'password' | 'playing' | 'failed'>('opening')
const busy = ref(false)
const error = ref('')
const src = ref('')
const openSource = shallowRef<(element: HTMLMediaElement, signal: AbortSignal) => Promise<void>>()
const apiIdInput = ref('')
const apiHash = ref('')
const phone = ref('')
const code = ref('')
const password = ref('')

const lifetime = new AbortController()
let telegram: typeof import('@/utils/telegramDirectClient') | undefined
let mp4: typeof import('@/utils/telegramMp4') | undefined
let client: BaseTelegramClient | undefined
let playback: AbortController | undefined
let phoneCodeHash = ''
let apiId = 0

function releaseClient(): void {
  playback?.abort()
  playback = undefined
  const previous = client
  client = undefined
  if (previous) void previous.destroy().catch(() => {})
}

function showCredentials(): void {
  releaseClient()
  phoneCodeHash = ''
  code.value = ''
  password.value = ''
  error.value = ''
  stage.value = 'credentials'
}

function expireSession(): void {
  clearTelegramDirectSession()
  showCredentials()
  error.value = 'Telegram 授权已过期，请重新授权。'
}

async function attempt(action: () => Promise<void>): Promise<void> {
  if (busy.value || lifetime.signal.aborted) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (cause) {
    if (lifetime.signal.aborted) return
    if (telegram?.telegramSessionExpired(cause)) {
      expireSession()
      return
    }
    if (stage.value === 'opening') {
      releaseClient()
      stage.value = 'failed'
    }
    error.value = telegram?.telegramDirectError(cause) ?? '无法加载 Telegram 直连组件，请重试。'
  } finally {
    busy.value = false
  }
}

async function play(authorizedClient: BaseTelegramClient): Promise<void> {
  if (!telegram) return
  stage.value = 'opening'
  const telegramClient = telegram
  const deadline = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
  const media = await telegramClient.telegramDirectMedia(authorizedClient, props.item, deadline)
  lifetime.signal.throwIfAborted()
  const connection = new AbortController()
  playback = connection
  const source = {
    ...media,
    async read(offset: number, end: number, signal: AbortSignal) {
      try {
        return await media.read(offset, end, signal)
      } catch (cause) {
        if (!signal.aborted && !connection.signal.aborted && telegramClient.telegramSessionExpired(cause)) {
          expireSession()
        }
        throw cause
      }
    },
  }
  if (mp4) {
    const playMp4 = mp4.playTelegramMp4
    openSource.value = (element, signal) => playMp4(source, element, AbortSignal.any([signal, connection.signal]))
  } else {
    src.value = openTelegramMedia(source, connection.signal)
  }
  stage.value = 'playing'
}

function start(): void {
  void attempt(async () => {
    stage.value = 'opening'
    if (!isSecureContext || typeof AbortSignal.any !== 'function') {
      stage.value = 'failed'
      error.value = '当前 WebView 无法启动 Telegram 直连播放。'
      return
    }
    try {
      mp4 = undefined
      await prepareTelegramMedia(lifetime.signal)
    } catch {
      lifetime.signal.throwIfAborted()
      const mime = props.item.mime ?? ''
      if (/^video\/(mp4|quicktime|x-m4v)(;|$)/i.test(mime) || /\.(mp4|m4v|mov)$/i.test(props.item.name)) {
        const module = await import('@/utils/telegramMp4')
        if (module.supportsTelegramMp4()) mp4 = module
      }
      if (!mp4) {
        stage.value = 'failed'
        error.value = '当前 WebView 无法直连播放这段视频。'
        return
      }
    }
    telegram ??= await import('@/utils/telegramDirectClient')
    lifetime.signal.throwIfAborted()
    const saved = readTelegramDirectSession()
    if (!saved) { showCredentials(); return }
    const restoring = telegram.createTelegramDirectClient(saved.apiId, '')
    client = restoring
    try { await restoring.importSession(saved.session) }
    catch {
      if (!lifetime.signal.aborted) {
        clearTelegramDirectSession()
        showCredentials()
      }
      return
    }
    await play(restoring)
  })
}

async function signedIn(authorizedClient: BaseTelegramClient): Promise<void> {
  lifetime.signal.throwIfAborted()
  stage.value = 'opening'
  const session = await authorizedClient.exportSession()
  lifetime.signal.throwIfAborted()
  saveTelegramDirectSession({ apiId, session })
  apiHash.value = ''
  phoneCodeHash = ''
  await play(authorizedClient)
}

function sendCode(): void {
  void attempt(async () => {
    const telegramClient = telegram
    if (!telegramClient) return
    const hash = apiHash.value.trim()
    apiId = Number(apiIdInput.value)
    if (!Number.isInteger(apiId) || apiId < 1 || apiId > 0x7fffffff || !/^[0-9a-f]{32}$/i.test(hash)) {
      error.value = '请输入有效的 Telegram API ID 和 API Hash。'
      return
    }
    releaseClient()
    const connecting = telegramClient.createTelegramDirectClient(apiId, hash)
    client = connecting
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    let result
    try {
      result = await untilAborted(abortSignal, () => (
        telegramClient.sendCode(connecting, { phone: phone.value.trim(), abortSignal })
      ))
    } catch (cause) {
      releaseClient()
      throw cause
    }
    lifetime.signal.throwIfAborted()
    if ('phoneCodeHash' in result) {
      if (result.type === 'email_required') {
        releaseClient()
        error.value = 'Telegram 要求先设置登录邮箱，请在 Telegram 中完成设置后重试。'
        return
      }
      phoneCodeHash = result.phoneCodeHash
      stage.value = 'code'
    } else {
      await signedIn(connecting)
    }
  })
}

function verifyCode(): void {
  void attempt(async () => {
    const signingIn = client
    if (!signingIn || !telegram) return
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    try {
      await telegram.signIn(signingIn, {
        phone: phone.value.trim(), phoneCodeHash, phoneCode: code.value.trim(), abortSignal,
      })
    } catch (cause) {
      if (!telegram.telegramPasswordRequired(cause)) throw cause
      stage.value = 'password'
      return
    } finally { code.value = '' }
    await signedIn(signingIn)
  })
}

function verifyPassword(): void {
  void attempt(async () => {
    const signingIn = client
    if (!signingIn || !telegram) return
    const secret = password.value
    password.value = ''
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    await telegram.checkPassword(signingIn, { password: secret, abortSignal })
    await signedIn(signingIn)
  })
}

function changeAccount(): void {
  void attempt(async () => {
    try {
      await revokeTelegramDirectSession()
      lifetime.signal.throwIfAborted()
      showCredentials()
    } catch {
      if (!lifetime.signal.aborted) {
        error.value = '暂时无法撤销原 Telegram 授权，请检查网络后再更换账号。'
      }
    }
  })
}

onMounted(start)
onBeforeUnmount(() => {
  lifetime.abort()
  releaseClient()
})
</script>

<template>
  <BaseModal :open="true" :title="item.name" :wide="stage === 'playing'" :media="stage === 'playing'" @close="emit('close')">
    <MediaPlayer v-if="stage === 'playing'"
      :src="src" :open-source="openSource" :item-id="item.id" kind="video" :size="item.size"
      :poster="api.thumbnailUrl(item.id, item.updated_at)" :allow-download="false"
      @close="emit('close')">
      <template #error-actions>
        <button class="ghost" type="button" @click="emit('serverFallback')">经服务器播放</button>
        <button v-if="item.telegram_video_url" class="ghost" type="button" @click="openTelegramVideo(item.telegram_video_url)">在 Telegram 中播放</button>
      </template>
    </MediaPlayer>
    <p v-else-if="stage === 'opening'" class="muted" role="status">正在准备 Telegram 直连播放…</p>
    <form v-else-if="stage === 'credentials'" class="telegram-auth-form" @submit.prevent="sendCode">
      <p class="muted">使用已加入原频道的 Telegram 账号授权。API ID 和 API Hash 可在 my.telegram.org 查看。</p>
      <label>API ID <input v-model="apiIdInput" required inputmode="numeric" pattern="[0-9]+" :disabled="busy" /></label>
      <label>API Hash <input v-model="apiHash" required type="password" autocomplete="off" :disabled="busy" /></label>
      <label>Telegram 手机号 <input v-model="phone" required type="tel" placeholder="+86…" :disabled="busy" /></label>
      <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在连接 Telegram…' : '获取验证码' }}</button>
    </form>
    <form v-else-if="stage === 'code'" class="telegram-auth-form" @submit.prevent="verifyCode">
      <p class="muted">输入 Telegram 发给此账号的验证码。</p>
      <label>Telegram 验证码 <input v-model="code" required autocomplete="one-time-code" :disabled="busy" /></label>
      <div class="operation-actions">
        <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在验证…' : '验证并播放' }}</button>
        <button class="ghost" type="button" :disabled="busy" @click="showCredentials">重新获取</button>
      </div>
    </form>
    <form v-else-if="stage === 'password'" class="telegram-auth-form" @submit.prevent="verifyPassword">
      <label>Telegram 两步验证密码 <input v-model="password" required type="password" autocomplete="off" :disabled="busy" /></label>
      <div class="operation-actions">
        <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在验证…' : '验证并播放' }}</button>
        <button class="ghost" type="button" :disabled="busy" @click="showCredentials">重新授权</button>
      </div>
    </form>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <template v-if="stage !== 'playing'" #footer>
      <button v-if="stage === 'failed'" class="ghost" type="button" :disabled="busy" @click="start">重试直连</button>
      <button v-if="stage === 'failed' && readTelegramDirectSession()" class="ghost" type="button" :disabled="busy" @click="changeAccount">更换直连账号</button>
      <button class="ghost" type="button" @click="emit('serverFallback')">经服务器播放</button>
      <button v-if="item.telegram_video_url" class="ghost" type="button" @click="openTelegramVideo(item.telegram_video_url)">在 Telegram 中播放</button>
    </template>
  </BaseModal>
</template>

<style scoped>
.telegram-auth-form, .telegram-auth-form label { display: grid; gap: 8px; }
.telegram-auth-form { gap: 12px; justify-items: start; }
.telegram-auth-form label { width: 100%; }
.telegram-auth-form p { margin-block: 0 4px; }
.telegram-auth-form input {
  width: 100%;
  min-height: 42px;
  padding: 8px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel);
  color: inherit;
  font: inherit;
}
</style>
