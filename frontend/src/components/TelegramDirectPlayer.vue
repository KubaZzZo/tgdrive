<script setup lang="ts">
import { defineAsyncComponent, onBeforeUnmount, onMounted, ref, shallowRef } from 'vue'
import type { BaseTelegramClient } from '@mtcute/web'
import { api } from '@/api/client'
import type { DriveItem } from '@/api/types'
import BaseModal from '@/components/ui/BaseModal.vue'
import { clearTelegramDirectSession, readTelegramDirectSession, withTelegramDirectSessionLock } from '@/utils/telegramDirectSession'
import type { TelegramDirectSession } from '@/utils/telegramDirectSession'
import { openTelegramMedia, prepareTelegramMedia } from '@/utils/telegramMedia'
import { openTelegramVideo } from '@/utils/telegramMiniApp'

const MediaPlayer = defineAsyncComponent(() => import('@/components/MediaPlayer.vue'))
const props = defineProps<{ item: DriveItem }>()
const emit = defineEmits<{ close: []; serverFallback: [message?: string] }>()
const closeDeadline = defineModel<number>('closeDeadline', { default: 0 })

const stage = ref<'opening' | 'playing' | 'failed'>('opening')
const error = ref('')
const src = ref('')
const openSource = shallowRef<(element: HTMLMediaElement, signal: AbortSignal) => Promise<void>>()

const lifetime = new AbortController()
let telegram: typeof import('@/utils/telegramDirectClient') | undefined
let mp4: typeof import('@/utils/telegramMp4') | undefined
let client: BaseTelegramClient | undefined
let playback: AbortController | undefined

function releaseClient(): void {
  playback?.abort()
  playback = undefined
  const previous = client
  client = undefined
  if (previous) void previous.destroy().catch(() => {})
}

async function expireSession(saved: TelegramDirectSession): Promise<void> {
  releaseClient()
  await withTelegramDirectSessionLock(async () => clearTelegramDirectSession(saved))
  if (!lifetime.signal.aborted) emit('serverFallback', 'Telegram 直连授权已失效，已改用服务器播放。')
}

async function play(authorizedClient: BaseTelegramClient, saved: TelegramDirectSession): Promise<void> {
  if (!telegram) return
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
          await expireSession(saved)
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

async function start(): Promise<void> {
  stage.value = 'opening'
  error.value = ''
  const saved = readTelegramDirectSession()
  if (!saved) {
    emit('serverFallback', '当前设备尚未授权 Telegram 直连，已改用服务器播放。')
    return
  }
  try {
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
    const restoring = telegram.createTelegramDirectClient(saved.apiId, '')
    client = restoring
    try { await restoring.importSession(saved.session) }
    catch {
      if (!lifetime.signal.aborted) await expireSession(saved)
      return
    }
    lifetime.signal.throwIfAborted()
    await play(restoring, saved)
  } catch (cause) {
    if (lifetime.signal.aborted) return
    if (telegram?.telegramSessionExpired(cause)) {
      await expireSession(saved)
      return
    }
    releaseClient()
    stage.value = 'failed'
    error.value = telegram?.telegramDirectError(cause) ?? '无法加载 Telegram 直连组件，请重试。'
  }
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
      v-model:close-deadline="closeDeadline"
      :src="src" :open-source="openSource" :item-id="item.id" kind="video" :size="item.size"
      :poster="api.thumbnailUrl(item.id, item.updated_at)" :allow-download="false"
      @close="emit('close')">
      <template #error-actions>
        <button class="ghost" type="button" @click="emit('serverFallback')">经服务器播放</button>
        <button v-if="item.telegram_video_url" class="ghost" type="button" @click="openTelegramVideo(item.telegram_video_url)">在 Telegram 中播放</button>
      </template>
    </MediaPlayer>
    <p v-else-if="stage === 'opening'" class="muted" role="status">正在准备 Telegram 直连播放…</p>
    <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
    <template v-if="stage !== 'playing'" #footer>
      <button v-if="stage === 'failed'" class="ghost" type="button" @click="start">重试直连</button>
      <button class="ghost" type="button" @click="emit('serverFallback')">经服务器播放</button>
      <button v-if="item.telegram_video_url" class="ghost" type="button" @click="openTelegramVideo(item.telegram_video_url)">在 Telegram 中播放</button>
    </template>
  </BaseModal>
</template>
