<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import type { BaseTelegramClient } from '@mtcute/web'
import { api } from '@/api/client'
import BaseModal from '@/components/ui/BaseModal.vue'
import { useAuthStore } from '@/stores/auth'
import {
  hasSavedTelegramDirectSession, readTelegramDirectSession, revokeTelegramDirectSession,
  saveTelegramDirectSession, withTelegramDirectSessionLock,
} from '@/utils/telegramDirectSession'
import { untilAborted } from '@/utils/telegramMedia'

const emit = defineEmits<{ close: [] }>()
const auth = useAuthStore()
const useConfiguredApi = computed(() => auth.authenticated && auth.meta?.telegram_direct_configured === true)
const supported = isSecureContext && typeof AbortSignal.any === 'function'
const stage = ref<'credentials' | 'code' | 'password' | 'authorized'>(readTelegramDirectSession() ? 'authorized' : 'credentials')
const busy = ref(false)
const error = ref('')
const savedForDevice = ref(hasSavedTelegramDirectSession())
const apiIdInput = ref('')
const apiHash = ref('')
const phone = ref('')
const code = ref('')
const password = ref('')

const lifetime = new AbortController()
let telegram: typeof import('@/utils/telegramDirectClient') | undefined
let client: BaseTelegramClient | undefined
let phoneCodeHash = ''
let apiId = 0

function releaseClient(): void {
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

async function attempt(action: () => Promise<void>): Promise<void> {
  if (busy.value || lifetime.signal.aborted) return
  busy.value = true
  error.value = ''
  try {
    await action()
  } catch (cause) {
    if (lifetime.signal.aborted) return
    if (telegram?.telegramSessionExpired(cause)) showCredentials()
    error.value = telegram?.telegramDirectError(cause) ?? '无法启动 Telegram 直连授权，请重试。'
  } finally {
    busy.value = false
  }
}

function showAuthorized(): void {
  savedForDevice.value = hasSavedTelegramDirectSession()
  apiHash.value = ''
  phoneCodeHash = ''
  password.value = ''
  code.value = ''
  stage.value = 'authorized'
  releaseClient()
}

function authorize(action: () => Promise<void>): void {
  void attempt(() => withTelegramDirectSessionLock(async () => {
    await auth.refreshMeta()
    lifetime.signal.throwIfAborted()
    if (!auth.authenticated) return
    if (readTelegramDirectSession()) showAuthorized()
    else await action()
  }, lifetime.signal))
}

async function signedIn(authorizedClient: BaseTelegramClient): Promise<void> {
  lifetime.signal.throwIfAborted()
  const session = await authorizedClient.exportSession()
  lifetime.signal.throwIfAborted()
  saveTelegramDirectSession({ apiId, session })
  showAuthorized()
}

function sendCode(): void {
  authorize(async () => {
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    const configured = useConfiguredApi.value
    const credentials = configured
      ? await api.telegramDirectCredentials(abortSignal).catch(() => null)
      : { api_id: Number(apiIdInput.value), api_hash: apiHash.value.trim() }
    lifetime.signal.throwIfAborted()
    if (!credentials) {
      error.value = '无法读取后台的 Telegram API 配置，请重试。'
      return
    }
    const { api_id: id, api_hash: hash } = credentials
    if (!Number.isInteger(id) || id < 1 || id > 0x7fffffff || !/^[0-9a-f]{32}$/i.test(hash)) {
      error.value = configured ? '请检查后台的 Telegram API 配置。' : '请输入有效的 Telegram API ID 和 API Hash。'
      return
    }
    const telegramClient = telegram ??= await import('@/utils/telegramDirectClient')
    abortSignal.throwIfAborted()
    apiId = id
    releaseClient()
    const connecting = telegramClient.createTelegramDirectClient(apiId, hash)
    client = connecting
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
  authorize(async () => {
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
  authorize(async () => {
    const signingIn = client
    if (!signingIn || !telegram) return
    const secret = password.value
    password.value = ''
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    await telegram.checkPassword(signingIn, { password: secret, abortSignal })
    await signedIn(signingIn)
  })
}

function revoke(): void {
  void attempt(async () => {
    try {
      await withTelegramDirectSessionLock(revokeTelegramDirectSession, lifetime.signal)
      lifetime.signal.throwIfAborted()
      showCredentials()
    } catch {
      if (!lifetime.signal.aborted) error.value = '暂时无法撤销 Telegram 直连授权，请检查网络后重试。'
    }
  })
}

onBeforeUnmount(() => {
  lifetime.abort()
  releaseClient()
})
</script>

<template>
  <BaseModal open title="Telegram 直连授权" @close="!busy && emit('close')">
    <template v-if="stage === 'authorized'">
      <p role="status">当前设备已授权 Telegram 直连播放。</p>
      <p v-if="!savedForDevice" class="muted">此授权仅在当前页面可用，关闭后仍可经服务器播放。</p>
      <button class="ghost" type="button" :disabled="busy" @click="revoke">撤销直连授权</button>
    </template>
    <p v-else-if="!supported" class="muted">当前环境无法设置 Telegram 直连授权。</p>
    <form v-else-if="stage === 'credentials'" class="telegram-auth-form" @submit.prevent="sendCode">
      <p class="muted">使用已加入存储频道的 Telegram 账号。本次授权会保存在当前浏览器或小程序中。</p>
      <template v-if="!useConfiguredApi">
        <p class="muted">API ID 和 API Hash 可在 my.telegram.org 查看。</p>
        <label>API ID <input v-model="apiIdInput" required inputmode="numeric" pattern="[0-9]+" :disabled="busy" /></label>
        <label>API Hash <input v-model="apiHash" required type="password" autocomplete="off" :disabled="busy" /></label>
      </template>
      <label>Telegram 手机号 <input v-model="phone" required type="tel" placeholder="+86…" :disabled="busy" /></label>
      <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在连接 Telegram…' : '获取验证码' }}</button>
    </form>
    <form v-else-if="stage === 'code'" class="telegram-auth-form" @submit.prevent="verifyCode">
      <p class="muted">输入 Telegram 发给此账号的验证码。</p>
      <label>Telegram 验证码 <input v-model="code" required autocomplete="one-time-code" :disabled="busy" /></label>
      <div class="operation-actions">
        <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在验证…' : '授权此设备' }}</button>
        <button class="ghost" type="button" :disabled="busy" @click="showCredentials">重新获取</button>
      </div>
    </form>
    <form v-else class="telegram-auth-form" @submit.prevent="verifyPassword">
      <label>Telegram 两步验证密码 <input v-model="password" required type="password" autocomplete="off" :disabled="busy" /></label>
      <div class="operation-actions">
        <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在验证…' : '授权此设备' }}</button>
        <button class="ghost" type="button" :disabled="busy" @click="showCredentials">重新授权</button>
      </div>
    </form>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
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
