<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mediaFailure } from '@/api/client'
import { mediaTime, playbackPosition, savePlayback } from '@/utils/playback'
import { attachMedia, type MediaPrefix, type MediaSession } from '@/utils/fragmentedMp4'
import {
  cacheVideo, cacheVideoPrefix, cacheVideoSegment, cachedVideo, cachedVideoSegments,
  clearVideoCache, deviceCacheAvailable, readCachedVideoSegment,
} from '@/utils/deviceCache'
import { holdCoverQueue } from '@/utils/coverQueue'
import { formatBytes } from '@/utils/format'
import { withMediaTimeout } from '@/utils/mediaTransfer'
import AppIcon from './AppIcon.vue'

const props = defineProps<{
  src: string; itemId: string; kind: 'video' | 'audio'; size: number; version: string; mime: string
  poster?: string; downloadPending?: boolean; downloadStatus?: string
}>()
const emit = defineEmits<{ download: [] }>()
type SafariVideo = HTMLVideoElement & {
  webkitDisplayingFullscreen?: boolean
  webkitSupportsFullscreen?: boolean
  webkitEnterFullscreen?: () => void
  webkitExitFullscreen?: () => void
  webkitPresentationMode?: 'inline' | 'picture-in-picture' | 'fullscreen'
  webkitSupportsPresentationMode?: (mode: 'picture-in-picture') => boolean
  webkitSetPresentationMode?: (mode: 'inline' | 'picture-in-picture') => void
}
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
const videoWidth = ref(0)
const videoHeight = ref(0)
const landscape = computed(() => videoWidth.value > 0 && videoHeight.value > 0 &&
  (quarterTurn.value ? videoHeight.value > videoWidth.value : videoWidth.value > videoHeight.value))
const expanded = ref(false)
const nativeFullscreen = ref(false)
const fullscreenActive = computed(() => expanded.value || nativeFullscreen.value)
const pipAvailable = ref(false)
const pipActive = ref(false)
const settingsOpen = ref(false)
const settingsButton = ref<HTMLButtonElement | null>(null)
const settingsId = `media-settings-${props.itemId}`
let orientationLocked = false
const videoElement = () => props.kind === 'video' ? player.value as SafariVideo | null : null
let restored = false
let resumePending = false
let lastSaved = 0
let disposed = false
let session: MediaSession | undefined
let setup: Promise<void> | undefined
let controller = new AbortController()
let failureCheck: AbortSignal | undefined
const streaming = ref(false)

function save(): void {
  const media = player.value
  if (!media || media.seeking || (resumePending && media.currentTime < 2)) return
  resumePending = false
  savePlayback(props.itemId, media.currentTime, media.duration)
}
function metadata(): void {
  const media = player.value
  if (!media) return
  syncPresentation()
  media.playbackRate = rate.value
  duration.value = Number.isFinite(media.duration) ? media.duration : 0
  if (!restored && duration.value > 0) {
    restored = true
    const position = playbackPosition(props.itemId, media.duration)
    resumePending = position > 0
    if (position) {
      media.currentTime = position
      notice.value = `已恢复至 ${mediaTime(position)}`
    }
  }
}
function syncPresentation(): void {
  const video = videoElement()
  if (!video) return
  videoWidth.value = video.videoWidth
  videoHeight.value = video.videoHeight
  pipAvailable.value = Boolean(
    (document.pictureInPictureEnabled && video.requestPictureInPicture) ||
    (video.webkitSetPresentationMode && video.webkitSupportsPresentationMode?.('picture-in-picture')),
  )
  pipActive.value = document.pictureInPictureElement === video || video.webkitPresentationMode === 'picture-in-picture'
  nativeFullscreen.value = document.fullscreenElement === container.value || Boolean(video.webkitDisplayingFullscreen)
  if (!nativeFullscreen.value && orientationLocked) {
    screen.orientation?.unlock?.()
    orientationLocked = false
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
  resumePending = false
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
  const video = videoElement()
  const element = container.value
  if (!video || !element || pipActive.value) return
  settingsOpen.value = false
  try {
    if (expanded.value) expanded.value = false
    else if (document.fullscreenElement === element) await document.exitFullscreen()
    else if (video.webkitDisplayingFullscreen) video.webkitExitFullscreen?.()
    else if (document.fullscreenEnabled && element.requestFullscreen) {
      try { await element.requestFullscreen() }
      catch { expanded.value = !disposed }
      const orientation = screen.orientation as (ScreenOrientation & { lock?: (mode: 'landscape') => Promise<void> }) | undefined
      if (document.fullscreenElement === element && landscape.value && matchMedia('(pointer: coarse), (max-width: 760px)').matches) {
        try {
          if (orientation?.lock) {
            await orientation.lock('landscape')
            orientationLocked = document.fullscreenElement === element
          }
        } catch { /* The player layout also supports portrait screens. */ }
      }
    } else if (rotation.value === 0 && video.webkitEnterFullscreen && video.webkitSupportsFullscreen) {
      try { video.webkitEnterFullscreen() }
      catch { expanded.value = !disposed }
    } else expanded.value = true
  } catch {
    notice.value = '暂时无法退出全屏，请使用系统返回键。'
  } finally {
    syncPresentation()
    if (expanded.value && !element.contains(document.activeElement)) element.focus({ preventScroll: true })
  }
}
async function pictureInPicture(): Promise<void> {
  const video = videoElement()
  if (!video || fullscreenActive.value || !video.videoWidth) return
  try {
    if (document.pictureInPictureElement === video) await document.exitPictureInPicture()
    else if (video.webkitPresentationMode === 'picture-in-picture') video.webkitSetPresentationMode?.('inline')
    else if (document.pictureInPictureEnabled && video.requestPictureInPicture) await video.requestPictureInPicture()
    else video.webkitSetPresentationMode?.('picture-in-picture')
  } catch {
    notice.value = '暂时无法进入画中画，请先开始播放。'
  } finally {
    syncPresentation()
  }
}
function failed(): void {
  if (disposed) return
  const code = player.value?.error?.code
  if (!code) return
  if (fallbackFromRemux()) return
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
  session = undefined
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
    const native = props.kind === 'audio' || playbackMode.value === 'native' ||
      /^video\/(?:webm|ogg|x-matroska|x-msvideo|mpeg)(?:;|$)/i.test(props.mime)
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
    const key = cacheKey()
    const value = await attachMedia(media, props.src, signal, () => {
      if (!signal.aborted && !fallbackFromRemux())
        reportFailure('视频流加载失败，请重试；若仍失败，可切换播放方式。')
    }, {
      file: localFile, bufferSeconds: () => bufferSeconds.value || undefined, version: props.version,
      size: props.size, prefix, remux: true,
      onPrefix: cacheSupported ? data => { void cacheVideoPrefix(key, data.bytes, data.etag) } : undefined,
      segments: cacheSupported ? {
        list: etag => cachedVideoSegments(key, etag),
        read: (etag, start, end) => readCachedVideoSegment(key, etag, start, end),
        write: (etag, segment, bytes) => cacheVideoSegment(key, segment, bytes, etag),
      } : undefined,
      onSeekUnavailable: () => { notice.value = '未能在缓冲预算内定位，已返回原进度。可重试或切换播放方式。' },
    })
    if (signal.aborted) value.dispose()
    else { session = value; streaming.value = value.streaming }
  })().catch(() => {
    if (!signal.aborted) reportFailure('视频加载失败，请重试。')
  })
}
function fallbackFromRemux(): boolean {
  if (!session?.remuxed || playbackMode.value === 'native') return false
  const wasPlaying = !paused.value
  playbackMode.value = 'native'
  retry()
  notice.value = '兼容播放失败，已切换到浏览器原生播放。'
  if (wasPlaying) void toggle()
  return true
}
function retry(): void {
  save()
  error.value = ''
  loading.value = true
  restored = false
  start()
}
function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape' && (settingsOpen.value || fullscreenActive.value)) {
    event.preventDefault()
    event.stopPropagation()
    if (settingsOpen.value) {
      settingsOpen.value = false
      settingsButton.value?.focus()
    } else void fullscreen()
    return
  }
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
  syncPresentation()
  start(true)
  window.addEventListener('pagehide', save)
  document.addEventListener('visibilitychange', save)
  document.addEventListener('fullscreenchange', syncPresentation)
})
onBeforeUnmount(() => {
  save()
  disposed = true
  cacheController?.abort()
  controller.abort()
  session?.dispose()
  window.removeEventListener('pagehide', save)
  document.removeEventListener('visibilitychange', save)
  document.removeEventListener('fullscreenchange', syncPresentation)
  if (orientationLocked) screen.orientation?.unlock?.()
  if (document.fullscreenElement === container.value) void document.exitFullscreen().catch(() => {})
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
    :class="{
      'audio-player': kind === 'audio',
      'media-expanded': expanded,
      'media-fullscreen': fullscreenActive,
      'media-landscape': landscape,
    }"
    tabindex="0"
    aria-label="媒体播放器；空格播放暂停，方向键快进快退"
    @keydown="onKey"
  >
    <div class="media-layout">
      <div class="media-screen" :class="{ 'media-quarter-turn': quarterTurn }">
        <component
          :is="kind"
          ref="player"
          :controls="kind === 'audio'"
          :style="rotation ? { rotate: rotation + 'deg' } : undefined"
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
          @resize="syncPresentation"
          @emptied="syncPresentation"
          @enterpictureinpicture="syncPresentation"
          @leavepictureinpicture="syncPresentation"
          @webkitpresentationmodechanged="syncPresentation"
          @webkitbeginfullscreen="syncPresentation"
          @webkitendfullscreen="syncPresentation"
          @click="kind === 'video' && toggle()"
        />
        <button
          v-if="kind === 'video' && paused && !error"
          class="media-stage-play"
          type="button"
          aria-label="开始播放"
          @click="toggle"
        ><AppIcon name="play" :size="28" /></button>
        <span v-if="loading && !error" class="media-loading" role="status">正在缓冲…</span>
        <div v-if="error" class="media-error" role="alert">
          <p>{{ error }}</p>
          <button class="ghost" type="button" @click="retry">重新加载</button>
        </div>
      </div>
      <div class="media-controls">
        <div class="media-timeline">
          <span>{{ mediaTime(scrub ?? current) }}</span>
          <input
            aria-label="播放进度"
            :aria-valuetext="mediaTime(scrub ?? current) + ' / ' + mediaTime(duration)"
            type="range"
            min="0"
            :max="duration || 1"
            step="0.1"
            :value="scrub ?? current"
            :disabled="!duration"
            @input="scrub = Number(($event.target as HTMLInputElement).value)"
            @change="commitSeek"
            @blur="scrub = null"
          />
          <span>{{ mediaTime(duration) }}</span>
        </div>
        <div class="media-toolbar">
          <div class="media-buttons" role="group" aria-label="播放控制">
            <button class="media-play" type="button" :aria-label="paused ? '播放' : '暂停'" :title="paused ? '播放' : '暂停'" @click="toggle">
              <AppIcon :name="paused ? 'play' : 'pause'" />
            </button>
            <button type="button" aria-label="后退 10 秒" title="后退 10 秒" :disabled="!duration" @click="seek(current - 10)">
              <AppIcon name="rewind" :size="15" />10
            </button>
            <button type="button" aria-label="前进 10 秒" title="前进 10 秒" :disabled="!duration" @click="seek(current + 10)">
              10<AppIcon name="forward" :size="15" />
            </button>
            <button type="button" :aria-label="muted ? '取消静音' : '静音'" :title="muted ? '取消静音' : '静音'" @click="mute">
              <AppIcon :name="muted ? 'volume-off' : 'volume-on'" />
            </button>
          </div>
          <div class="media-buttons" role="group" aria-label="播放器操作">
            <button type="button" aria-label="下载文件" :title="downloadPending ? '正在检查下载…' : '下载文件'" :disabled="downloadPending" @click="emit('download')">
              <AppIcon name="download" />
            </button>
            <button
              v-if="pipAvailable"
              type="button"
              aria-label="画中画"
              :aria-pressed="pipActive"
              :title="pipActive ? '退出画中画' : '画中画'"
              :disabled="fullscreenActive || !videoWidth"
              @click="pictureInPicture"
            ><AppIcon name="pip" /></button>
            <button
              v-if="kind === 'video'"
              type="button"
              :aria-label="fullscreenActive ? '退出全屏' : '全屏'"
              :title="fullscreenActive ? '退出全屏' : '全屏'"
              :disabled="pipActive"
              @click="fullscreen"
            ><AppIcon :name="fullscreenActive ? 'fullscreen-exit' : 'fullscreen'" /></button>
            <button
              ref="settingsButton"
              type="button"
              aria-label="播放设置"
              title="播放设置"
              :aria-expanded="settingsOpen"
              :aria-controls="settingsOpen ? settingsId : undefined"
              @click="settingsOpen = !settingsOpen"
            ><AppIcon name="settings" /></button>
          </div>
        </div>
        <div v-if="settingsOpen" :id="settingsId" class="media-settings" role="group" aria-label="播放设置">
          <div class="media-settings-head"><strong>播放设置</strong><span>{{ formatBytes(size) }}</span></div>
          <label class="media-setting">播放速度
            <select v-model="rate" aria-label="播放速度" @change="changeRate">
              <option v-for="speed in [0.5, 0.75, 1, 1.25, 1.5, 2]" :key="speed" :value="speed">{{ speed }}×</option>
            </select>
          </label>
          <label class="media-setting">音量
            <input class="media-volume" aria-label="音量" type="range" min="0" max="1" step=".05" v-model.number="volume" @input="changeVolume" />
          </label>
          <label v-if="kind === 'video'" class="media-setting">播放方式
            <select v-model="playbackMode" aria-label="播放方式" @change="retry"><option value="auto">兼容模式</option><option value="native">浏览器原生</option></select>
          </label>
          <label v-if="streaming" class="media-setting">预缓冲时长
            <select v-model.number="bufferSeconds" aria-label="预缓冲时长"><option :value="0">自动</option><option :value="60">最多 60 秒</option><option :value="120">最多 120 秒</option></select>
          </label>
          <div class="media-buttons media-setting-actions">
            <button v-if="duration" type="button" @click="restart">从头播放</button>
            <button v-if="kind === 'video'" type="button" @click="rotation = (rotation + 90) % 360">旋转视频 {{ rotation }}°</button>
            <button v-if="kind === 'video' && cacheSupported && !cacheBusy && !downloaded" type="button" @click="cacheHere">缓存到本机</button>
            <button v-if="cacheBusy" type="button" @click="cacheController?.abort()">取消缓存 {{ cacheProgress }}%</button>
            <button v-if="kind === 'video' && cacheSupported && !cacheBusy" type="button" @click="clearCache">清除本机缓存</button>
          </div>
        </div>
        <p v-if="notice || cacheBusy || local" class="media-notice" role="status">
          {{ cacheBusy ? '正在缓存到本机 ' + cacheProgress + '%' : notice || '正在播放本机缓存' }}
        </p>
        <p v-if="downloadStatus" class="media-notice" role="status">{{ downloadStatus }}</p>
      </div>
    </div>
  </div>
</template>
