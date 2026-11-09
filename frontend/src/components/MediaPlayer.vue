<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mediaFailure } from '@/api/client'
import { mediaTime, playbackPosition, savePlayback } from '@/utils/playback'
import { attachMedia, type MediaPrefix, type MediaSession } from '@/utils/fragmentedMp4'
import { cacheVideo, cacheVideoPrefix, cachedVideo, clearVideoCache, deviceCacheAvailable } from '@/utils/deviceCache'
import { holdCoverQueue } from '@/utils/coverQueue'
import { withMediaTimeout } from '@/utils/mediaTransfer'

const props = defineProps<{ src: string; itemId: string; kind: 'video' | 'audio'; size: number; version: string; mime: string; poster?: string }>()
const cacheSupported = deviceCacheAvailable()
const cacheBusy = ref(false)
const cacheProgress = ref(0)
const local = ref(false)
const downloaded = ref(false)
const bufferSeconds = ref(0)
const playbackMode = ref('auto')
let localFile: Blob | undefined
let releaseLocal: (() => void) | undefined
let releaseCovers: (() => void) | undefined
let cacheController: AbortController | undefined
const cacheKey = () => `${props.src}|${props.version}`
async function cacheHere(): Promise<void> {
  cacheBusy.value = true
  cacheProgress.value = 0
  cacheController = new AbortController()
  try {
    await cacheVideo(props.src, cacheKey(), props.size, cacheController.signal,
      bytes => { cacheProgress.value = Math.floor(bytes / props.size * 100) },
      () => !paused.value || document.hidden)
    if (disposed) return
    downloaded.value = true
    notice.value = '已缓存到本机，下次打开时会优先使用。'
  } catch (e) {
    if (!disposed) notice.value = cacheController.signal.aborted ? '已取消缓存' : (e instanceof Error ? e.message : '本机缓存失败')
  } finally { cacheBusy.value = false }
}
async function clearCache(): Promise<void> {
  try {
    await clearVideoCache(localFile ? () => {
      save()
      localFile = undefined
      local.value = false
      restored = false
      start()
      releaseLocal?.()
      releaseLocal = undefined
    } : undefined)
    downloaded.value = false
    notice.value = '本机视频缓存已清除'
  } catch (e) { notice.value = e instanceof Error ? e.message : '清除失败' }
}
const player = ref<HTMLMediaElement | null>(null)
const container = ref<HTMLElement | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const paused = ref(true)
const duration = ref(0)
const current = ref(0)
const scrub = ref<number | null>(null)
const rate = ref(1)
const volume = ref(1)
const muted = ref(false)
const rotation = ref(0)
const quarterTurn = computed(() => rotation.value % 180 !== 0)
let restored = false
let lastSaved = 0
let disposed = false
let session: MediaSession | undefined
let setup: Promise<void> | undefined
let controller = new AbortController()
let failureCheck: AbortSignal | undefined
const streaming = ref(false)
const pipAvailable = computed(
  () =>
    props.kind === 'video' && typeof document !== 'undefined' && document.pictureInPictureEnabled,
)

function save(): void {
  const media = player.value
  if (media) savePlayback(props.itemId, media.currentTime, media.duration)
}
function metadata(): void {
  const media = player.value
  if (!media) return
  media.playbackRate = rate.value
  duration.value = Number.isFinite(media.duration) ? media.duration : 0
  if (!restored && duration.value > 0 && !streaming.value) {
    restored = true
    const position = playbackPosition(props.itemId, media.duration)
    if (position) {
      media.currentTime = position
      notice.value = `已恢复至 ${mediaTime(position)}`
    }
  }
}
function update(): void {
  current.value = player.value?.currentTime ?? 0
  if (Date.now() - lastSaved > 5000) {
    save()
    lastSaved = Date.now()
  }
}
async function toggle(): Promise<void> {
  await setup
  const media = player.value
  if (!media) return
  if (media.paused) {
    try {
      await media.play()
      error.value = ''
    } catch {
      notice.value = '请点击播放器中的播放按钮，或等待缓冲完成。'
    }
  } else media.pause()
}
function seek(value: number): void {
  const media = player.value
  if (!media || !Number.isFinite(media.duration)) return
  const target = Math.max(0, Math.min(media.duration - 0.05, value))
  if (Math.abs(media.currentTime - target) > 0.05) media.currentTime = target
  current.value = media.currentTime
}
function commitSeek(event: Event): void {
  seek(Number((event.target as HTMLInputElement).value))
  scrub.value = null
}
function restart(): void {
  seek(0)
  notice.value = ''
}
function seeking(): void {
  const media = player.value
  if (!media) return
  current.value = media.currentTime
  loading.value = media.readyState < HTMLMediaElement.HAVE_FUTURE_DATA
}
function changeRate(): void {
  if (player.value) player.value.playbackRate = rate.value
}
function changeVolume(): void {
  if (player.value) {
    player.value.volume = volume.value
    player.value.muted = volume.value === 0
  }
}
function mute(): void {
  if (player.value) player.value.muted = !player.value.muted
}
function volumeChanged(): void {
  if (player.value) {
    volume.value = player.value.volume
    muted.value = player.value.muted
  }
}
async function fullscreen(): Promise<void> {
  const video = player.value as HTMLVideoElement & { webkitEnterFullscreen?: () => void }
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else if (container.value?.requestFullscreen) await container.value.requestFullscreen()
    else if (video?.webkitEnterFullscreen) video.webkitEnterFullscreen()
    else notice.value = '请使用播放器自带的全屏按钮。'
  } catch {
    notice.value = '当前环境无法进入全屏，请使用播放器自带的全屏按钮。'
  }
}
async function pictureInPicture(): Promise<void> {
  try {
    if (document.pictureInPictureElement) await document.exitPictureInPicture()
    else await (player.value as HTMLVideoElement).requestPictureInPicture()
  } catch {
    notice.value = '暂时无法进入画中画，请先开始播放。'
  }
}
function failed(): void {
  if (disposed) return
  const code = player.value?.error?.code
  if (!code) return
  reportFailure(
    code === 2
      ? '媒体传输中断，请重试加载。'
      : '浏览器无法解码此文件，可以下载后使用本地播放器观看。',
  )
}
function reportFailure(fallback: string): void {
  const signal = controller.signal
  if (signal.aborted || disposed) return
  loading.value = false
  if (localFile) { error.value = fallback; return }
  if (failureCheck === signal) return
  failureCheck = signal
  error.value = '正在检查媒体来源…'
  void mediaFailure(props.src, fallback, signal).then(message => {
    if (!signal.aborted && !disposed) error.value = message
  })
}
function start(useCache = false): void {
  controller.abort()
  session?.dispose()
  controller = new AbortController()
  error.value = ''
  const signal = controller.signal
  const media = player.value
  if (!media) return
  media.pause()
  media.removeAttribute('src')
  media.load()
  streaming.value = false
  setup = (async () => {
    let prefix: MediaPrefix | undefined
    const native = props.kind === 'audio' || playbackMode.value === 'native'
    if (useCache && cacheSupported && props.kind === 'video') {
      const hit = await cachedVideo(cacheKey(), props.size, !native)
      if (hit) {
        let usingFile = false
        try {
          signal.throwIfAborted()
          const response = await withMediaTimeout(signal, 1200, cacheSignal => fetch(props.src, {
            method: 'HEAD', headers: { Range: 'bytes=0-0' }, cache: 'no-store', signal: cacheSignal,
          }))
          if (!signal.aborted && response.status === 206 && response.headers.get('ETag') === hit.etag &&
            response.headers.get('Content-Range') === 'bytes 0-0/' + props.size) {
            if (hit.file.size === props.size) {
              localFile = hit.file.slice(0, hit.file.size, props.mime)
              releaseLocal = hit.release
              usingFile = true
              local.value = true
              downloaded.value = true
            } else prefix = { bytes: new Uint8Array(await hit.file.arrayBuffer()), etag: hit.etag }
          }
        } catch { /* The network source remains available if cache validation fails. */ }
        finally { if (!usingFile) hit.release() }
      }
    }
    if (signal.aborted) return
    if (native) {
      if (localFile) {
        const objectUrl = URL.createObjectURL(localFile)
        media.src = objectUrl
        session = { streaming: false, dispose: () => URL.revokeObjectURL(objectUrl) }
      } else media.src = props.src
      return
    }
    const value = await attachMedia(media, props.src, signal, () => {
      if (!signal.aborted) reportFailure('视频流加载失败，请重试；若仍失败，可切换播放方式。')
    }, {
      file: localFile, bufferSeconds: () => bufferSeconds.value || undefined, version: props.version,
      size: props.size, prefix,
      onPrefix: cacheSupported ? data => { void cacheVideoPrefix(cacheKey(), data.bytes, data.etag) } : undefined,
      onSeekUnavailable: () => { notice.value = '未能在缓冲预算内定位，已返回原进度。可重试或切换播放方式。' },
    })
    if (signal.aborted) value.dispose()
    else { session = value; streaming.value = value.streaming }
  })().catch(() => {
    if (!signal.aborted) reportFailure('视频加载失败，请重试。')
  })
}
function retry(): void {
  error.value = ''
  loading.value = true
  restored = false
  start()
}
function onKey(event: KeyboardEvent): void {
  if ((event.target as HTMLElement).matches('input, select, button, a')) return
  if ([' ', 'ArrowLeft', 'ArrowRight', 'm', 'M', 'f', 'F'].includes(event.key))
    event.preventDefault()
  if (event.key === ' ') void toggle()
  if (event.key === 'ArrowLeft') seek(current.value - 10)
  if (event.key === 'ArrowRight') seek(current.value + 10)
  if (event.key.toLowerCase() === 'm') mute()
  if (event.key.toLowerCase() === 'f' && props.kind === 'video') void fullscreen()
}
function pause(): void {
  paused.value = true
  save()
}
onMounted(() => {
  releaseCovers = holdCoverQueue()
  start(true)
  window.addEventListener('pagehide', save)
  document.addEventListener('visibilitychange', save)
})
onBeforeUnmount(() => {
  save()
  disposed = true
  cacheController?.abort()
  controller.abort()
  session?.dispose()
  window.removeEventListener('pagehide', save)
  document.removeEventListener('visibilitychange', save)
  if (player.value) {
    player.value.pause()
    player.value.removeAttribute('src')
    player.value.load()
  }
  releaseLocal?.()
  releaseCovers?.()
})
</script>
<template>
  <div
    ref="container"
    class="media-player"
    :class="{ 'audio-player': kind === 'audio' }"
    tabindex="0"
    aria-label="媒体播放器；空格播放暂停，方向键快进快退"
    @keydown="onKey"
  >
    <div class="media-screen" :class="{ 'media-quarter-turn': quarterTurn }">
      <component
        :is="kind"
        ref="player"
        :controls="rotation === 0"
        :style="rotation ? { rotate: `${rotation}deg` } : undefined"
        playsinline
        preload="metadata"
        :poster="kind === 'video' ? poster : undefined"
        :aria-label="kind === 'video' ? '视频播放' : '音频播放'"
        @loadedmetadata="metadata"
        @durationchange="metadata"
        @timeupdate="update"
        @seeking="seeking"
        @seeked="loading = false"
        @play="paused = false"
        @pause="pause"
        @ended="pause"
        @waiting="loading = true"
        @canplay="loading = false"
        @playing="loading = false"
        @error="failed"
        @volumechange="volumeChanged"
      />
      <span v-if="loading && !error" class="media-loading" role="status">正在缓冲…</span>
      <div v-if="error" class="media-error" role="alert">
        <p>{{ error }}</p>
        <button class="ghost" @click="retry">重新加载</button>
      </div>
    </div>
    <div class="media-controls">
      <div class="media-timeline">
        <span>{{ mediaTime(current) }}</span
        ><label class="sr-only" :for="`seek-${itemId}`">播放进度</label
        ><input
          :id="`seek-${itemId}`"
          type="range"
          min="0"
          :max="duration || 1"
          step="0.1"
          :value="scrub ?? current"
          :disabled="!duration"
          @input="scrub = Number(($event.target as HTMLInputElement).value)"
          @change="commitSeek"
          @blur="scrub = null"
        /><span>{{ mediaTime(duration) }}</span>
      </div>
      <div class="media-buttons">
        <button @click="toggle">{{ paused ? '播放' : '暂停' }}</button
        ><button :disabled="!duration" @click="seek(current - 10)">后退 10 秒</button
        ><button :disabled="!duration" @click="seek(current + 10)">前进 10 秒</button
        ><label class="speed-control"
          >倍速<select v-model="rate" aria-label="播放速度" @change="changeRate">
            <option v-for="speed in [0.5, 0.75, 1, 1.25, 1.5, 2]" :key="speed" :value="speed">
              {{ speed }}×
            </option>
          </select></label
        ><button :aria-label="muted ? '取消静音' : '静音'" @click="mute">
          {{ muted ? '取消静音' : '静音' }}</button
        ><input
          class="media-volume"
          aria-label="音量"
          type="range"
          min="0"
          max="1"
          step=".05"
          v-model.number="volume"
          @input="changeVolume"
        /><button v-if="pipAvailable" @click="pictureInPicture">画中画</button
        ><button v-if="kind === 'video'" @click="fullscreen">全屏</button>
        <button v-if="kind === 'video'" @click="rotation = (rotation + 90) % 360">
          旋转视频<span v-if="rotation">（{{ rotation }}°）</span>
        </button>
        <label v-if="kind === 'video'" class="speed-control">播放方式<select v-model="playbackMode" aria-label="播放方式" @change="retry"><option value="auto">兼容模式（推荐）</option><option value="native">浏览器原生</option></select></label>
        <label v-if="streaming" class="speed-control">预缓冲<select v-model.number="bufferSeconds" aria-label="预缓冲时长"><option :value="0">自动</option><option :value="60">最多 60 秒</option><option :value="120">最多 120 秒</option></select></label>
        <button v-if="kind === 'video' && cacheSupported && !cacheBusy && !downloaded" @click="cacheHere">缓存到本机</button>
        <button v-if="cacheBusy" @click="cacheController?.abort()">取消缓存 {{ cacheProgress }}%</button>
        <button v-if="cacheSupported && !cacheBusy" @click="clearCache">清除本机缓存</button>
      </div>
      <p v-if="notice" class="media-notice" role="status">
        {{ notice }}
        <button @click="restart">从头播放</button>
      </p>
      <p class="media-hint">{{ local ? '正在播放本机缓存' : streaming ? '流式播放 · 跳转后从目标附近缓冲' : '自动记住播放位置' }} · 空格播放/暂停 · ← → 快进快退</p>
      <p v-if="kind === 'video' && cacheSupported" class="media-hint">本机会优先保留最近使用的视频缓存；首次仍需网络传输，浏览器可能回收缓存。</p>
    </div>
  </div>
</template>
