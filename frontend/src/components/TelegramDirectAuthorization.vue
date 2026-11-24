<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import type { BaseTelegramClient, SentCodeDeliveryType } from '@mtcute/web'
import { encode } from 'uqr'
import { api } from '@/api/client'
import BaseModal from '@/components/ui/BaseModal.vue'
import { useAuthStore } from '@/stores/auth'
import { useToastStore } from '@/stores/toast'
import {
  hasSavedTelegramDirectSession, readTelegramDirectRevocation, readTelegramDirectSession,
  revokeTelegramDirectSession, saveTelegramDirectSession, withTelegramDirectSessionLock,
} from '@/utils/telegramDirectSession'
import { untilAborted } from '@/utils/telegramMedia'

const emit = defineEmits<{ close: [] }>()
const auth = useAuthStore()
const toast = useToastStore()
const useConfiguredApi = computed(() => auth.authenticated && auth.meta?.telegram_direct_configured === true)
const supported = isSecureContext && typeof AbortSignal.any === 'function'
const stage = ref<'credentials' | 'code' | 'qr' | 'password' | 'authorized'>(readTelegramDirectSession() ? 'authorized' : 'credentials')
const busy = ref(false)
const error = ref('')
const savedForDevice = ref(hasSavedTelegramDirectSession())
const apiIdInput = ref('')
const apiHash = ref('')
const phone = ref('')
const code = ref('')
const password = ref('')
const codeDelivery = ref<SentCodeDeliveryType>()
const codeHint = computed(() => {
  if (codeDelivery.value === 'app') {
    return '请在此账号已登录的 Telegram 客户端中，打开官方「Telegram」对话查收验证码。'
  }
  if (codeDelivery.value?.startsWith('sms')) return 'Telegram 已通过短信发送验证码。收不到短信时，可改用扫码授权。'
  return '输入 Telegram 发给此账号的验证码。'
})
const qrImage = ref('')
const qrScanned = ref(false)

interface AuthorizationClient {
  apiId: number
  client: BaseTelegramClient
  sdk: typeof import('@/utils/telegramDirectClient')
  revocation: string
  qrAuthorized: boolean
}

const lifetime = new AbortController()
let telegram: AuthorizationClient['sdk'] | undefined
let connection: AuthorizationClient | undefined
let qrAttempt: AbortController | undefined
let phoneCodeHash = ''

function releaseClient(): void {
  const previous = connection
  connection = undefined
  if (previous) void previous.client.destroy().catch(() => {})
}

function showCredentials(): void {
  qrAttempt?.abort()
  qrImage.value = ''
  releaseClient()
  phoneCodeHash = ''
  code.value = ''
  password.value = ''
  error.value = ''
  stage.value = 'credentials'
}

function authorizationIsCurrent(attempt: AuthorizationClient): boolean {
  if (attempt.revocation === readTelegramDirectRevocation()) return true
  showCredentials()
  error.value = '设备直连已撤销，请重新授权。'
  return false
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
  qrImage.value = ''
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

async function signedIn(authorized: AuthorizationClient, signal = lifetime.signal): Promise<void> {
  signal.throwIfAborted()
  const session = await authorized.client.exportSession()
  signal.throwIfAborted()
  saveTelegramDirectSession({ apiId: authorized.apiId, session })
  showAuthorized()
}

async function prepareClient(signal: AbortSignal, method: 'code' | 'qr'): Promise<AuthorizationClient | undefined> {
  const configured = useConfiguredApi.value
  const credentials = configured
    ? await api.telegramDirectCredentials(signal).catch(() => null)
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
  const sdk = telegram ??= await import('@/utils/telegramDirectClient')
  signal.throwIfAborted()
  const connecting: AuthorizationClient = {
    apiId: id,
    sdk,
    revocation: readTelegramDirectRevocation(),
    qrAuthorized: false,
    client: sdk.createTelegramDirectClient(id, hash, method === 'qr' ? {
      onQrAuthorized: () => { connecting.qrAuthorized = true },
    } : undefined),
  }
  return connecting
}

function sendCode(): void {
  authorize(async () => {
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    const connecting = await prepareClient(abortSignal, 'code')
    if (!connecting) return
    releaseClient()
    connection = connecting
    let result
    try {
      result = await untilAborted(abortSignal, () => (
        connecting.sdk.sendCode(connecting.client, { phone: phone.value.trim(), abortSignal })
      ))
    } catch (cause) {
      releaseClient()
      throw cause
    }
    lifetime.signal.throwIfAborted()
    if ('phoneCodeHash' in result) {
      if (result.type === 'email_required' || result.type === 'firebase') {
        releaseClient()
        error.value = result.type === 'email_required'
          ? 'Telegram 要求先设置登录邮箱，请在 Telegram 中完成设置后重试，或改用扫码授权。'
          : '此号码的短信验证需要 Telegram 官方客户端，请改用扫码授权。'
        return
      }
      codeDelivery.value = result.type
      phoneCodeHash = result.phoneCodeHash
      stage.value = 'code'
    } else {
      await signedIn(connecting)
    }
  })
}

function renderLoginQr(url: string): string {
  const { data, size } = encode(url, { border: 4 })
  const scale = 6
  const canvas = document.createElement('canvas')
  canvas.width = canvas.height = size * scale
  const context = canvas.getContext('2d')
  if (!context) throw new Error('无法绘制 Telegram 登录二维码')
  context.fillStyle = 'white'
  context.fillRect(0, 0, canvas.width, canvas.height)
  context.fillStyle = 'black'
  data.forEach((row, y) => row.forEach((dark, x) => {
    if (dark) context.fillRect(x * scale, y * scale, scale, scale)
  }))
  return canvas.toDataURL()
}

function startQr(): void {
  authorize(async () => {
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    const scanning = await prepareClient(abortSignal, 'qr')
    if (!scanning) return
    releaseClient()
    phoneCodeHash = ''
    code.value = ''
    qrImage.value = ''
    qrScanned.value = false
    const scan = qrAttempt = new AbortController()
    stage.value = 'qr'
    void waitForQr(scanning, scan)
  })
}

async function waitForQr(scanning: AuthorizationClient, scan: AbortController): Promise<void> {
  const { client, sdk } = scanning
  const signal = AbortSignal.any([lifetime.signal, scan.signal])
  let scanned = false
  let saved = false
  try {
    await untilAborted(signal, () => sdk.signInQr(client, {
      abortSignal: signal,
      onUrlUpdated(url) {
        signal.throwIfAborted()
        qrScanned.value = false
        qrImage.value = renderLoginQr(url)
      },
      onQrScanned() {
        if (signal.aborted) return
        scanned = true
        qrImage.value = ''
        qrScanned.value = true
      },
    }))
    // Keep the scan wait outside the device session lock.
    await withTelegramDirectSessionLock(async () => {
      await auth.refreshMeta()
      signal.throwIfAborted()
      if (!auth.authenticated) {
        showCredentials()
        return
      }
      if (!authorizationIsCurrent(scanning)) return
      if (readTelegramDirectSession()) showAuthorized()
      else {
        await signedIn(scanning, signal)
        saved = true
      }
    }, signal)
  } catch (cause) {
    if (signal.aborted) return
    if (!scanning.qrAuthorized && sdk.telegramPasswordRequired(cause)) {
      if (!authorizationIsCurrent(scanning)) return
      connection = scanning
      qrImage.value = ''
      stage.value = 'password'
      return
    }
    showCredentials()
    error.value = sdk.telegramDirectError(cause)
  } finally {
    if (qrAttempt === scan) qrAttempt = undefined
    if (connection !== scanning) {
      if (!saved && (scanned || scanning.qrAuthorized)) {
        try { await sdk.logOutTelegramDirectClient(client, AbortSignal.timeout(8000)) }
        catch (cause) {
          if (scanning.qrAuthorized && !sdk.telegramSessionExpired(cause)) {
            toast.error('扫码会话撤销失败，可在 Telegram 的「设备」中结束对应的 TG Drive Browser 会话。')
          }
        }
      }
      void client.destroy().catch(() => {})
    }
  }
}

function verifyCode(): void {
  authorize(async () => {
    const signingIn = connection
    if (!signingIn || !authorizationIsCurrent(signingIn)) return
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    try {
      await signingIn.sdk.signIn(signingIn.client, {
        phone: phone.value.trim(), phoneCodeHash, phoneCode: code.value.trim(), abortSignal,
      })
    } catch (cause) {
      if (!signingIn.sdk.telegramPasswordRequired(cause)) throw cause
      stage.value = 'password'
      return
    } finally { code.value = '' }
    await signedIn(signingIn)
  })
}

function verifyPassword(): void {
  authorize(async () => {
    const signingIn = connection
    if (!signingIn || !authorizationIsCurrent(signingIn)) return
    const secret = password.value
    password.value = ''
    const abortSignal = AbortSignal.any([lifetime.signal, AbortSignal.timeout(30_000)])
    await signingIn.sdk.checkPassword(signingIn.client, { password: secret, abortSignal })
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
      <div class="operation-actions">
        <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在连接 Telegram…' : '获取验证码' }}</button>
        <button class="ghost" type="button" :disabled="busy" @click="startQr">Telegram 扫码授权</button>
      </div>
    </form>
    <form v-else-if="stage === 'code'" class="telegram-auth-form" @submit.prevent="verifyCode">
      <p class="muted">{{ codeHint }}</p>
      <label>Telegram 验证码 <input v-model="code" required autocomplete="one-time-code" :disabled="busy" /></label>
      <div class="operation-actions">
        <button class="primary" type="submit" :disabled="busy">{{ busy ? '正在验证…' : '授权此设备' }}</button>
        <button class="ghost" type="button" :disabled="busy" @click="showCredentials">重新获取</button>
        <button class="ghost" type="button" :disabled="busy" @click="startQr">Telegram 扫码授权</button>
      </div>
    </form>
    <div v-else-if="stage === 'qr'" class="telegram-qr">
      <template v-if="qrImage">
        <img :src="qrImage" alt="Telegram 登录二维码" />
        <p class="muted">在另一台已登录同一账号的设备上，打开 Telegram 的「设置 → 设备 → 连接桌面设备」，扫描此二维码并确认。</p>
      </template>
      <p v-else role="status">{{ qrScanned ? '正在确认 Telegram 授权…' : '正在生成 Telegram 登录二维码…' }}</p>
      <button class="ghost" type="button" :disabled="busy" @click="showCredentials">返回授权方式</button>
    </div>
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
.telegram-qr { display: grid; justify-items: center; gap: 12px; text-align: center; }
.telegram-qr img { width: 224px; max-width: 100%; background: white; border: 1px solid var(--line); border-radius: 12px; }
.telegram-qr p { margin: 0; }
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
