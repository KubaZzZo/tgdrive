<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { mediaFailure } from '@/api/client'
import { mediaTime, playbackPosition, savePlayback } from '@/utils/playback'
import { formatBytes } from '@/utils/format'
import { holdCoverQueue } from '@/utils/coverQueue'
import AppIcon from '@/components/ui/AppIcon.vue'

const props = withDefaults(defineProps<{
  src: string; itemId: string; kind: 'video' | 'audio'; size: number
  openedAt?: number
  poster?: string; downloadPending?: boolean; downloadStatus?: string; allowDownload?: boolean
  openSource?: (element: HTMLMediaElement, signal: AbortSignal) => Promise<void>
}>(), { allowDownload: true })
const emit = defineEmits<{ download: []; close: [] }>()
const closeDeadline = defineModel<number>('closeDeadline', { default: 0 })
type SafariVideo = HTMLVideoElement & {
  webkitDisplayingFullscreen?: boolean
  webkitSupportsFullscreen?: boolean
  webkitEnterFullscreen?: () => void
  webkitExitFullscreen?: () => void
  webkitPresentationMode?: 'inline' | 'picture-in-picture' | 'fullscreen'
  webkitSupportsPresentationMode?: (mode: 'picture-in-picture') => boolean
  webkitSetPresentationMode?: (mode: 'inline' | 'picture-in-picture') => void
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
const pipPending = ref(false)
const listening = ref(false)
const pipDisabled = computed(() => pipPending.value || fullscreenActive.value || listening.value || !videoWidth.value)
const listenDisabled = computed(() => fullscreenActive.value || pipActive.value || pipPending.value)
const settingsOpen = ref(false)
const settingsButton = ref<HTMLButtonElement | null>(null)
const settingsId = `media-settings-${props.itemId}`
const closeTimerPresets = [15, 30, 60]
const closeTimerChoice = ref<number | 'custom'>(15)
const customCloseMinutes = ref<number | string>('')
const closeSeconds = ref(0)
let closeTimeout: ReturnType<typeof setTimeout> | undefined
let orientationLocked = false
const videoElement = () => props.kind === 'video' ? player.value as SafariVideo | null : null
let restored = false
let resumePending = false
let lastSaved = 0
let controller = new AbortController()
let disposed = false
const startupBudgetMs = 15_000
const nativeVideoStartupLimitMs = 180_000
let startupTimer: ReturnType<typeof setTimeout> | undefined
let startupStarted = 0
let openedAt = 0
let active = false
let startedPlayback = false
let releaseCovers: (() => void) | undefined

function cancelCloseTimer(): void {
  closeDeadline.value = 0
}
function updateCloseTimer(): void {
  clearTimeout(closeTimeout)
  if (!closeDeadline.value) {
    closeSeconds.value = 0
    return
  }
  const remaining = closeDeadline.value - Date.now()
  if (remaining <= 0) {
    cancelCloseTimer()
    emit('close')
    return
  }
  closeSeconds.value = Math.ceil(remaining / 1000)
  closeTimeout = setTimeout(updateCloseTimer, remaining % 1000 || 1000)
}
function startCloseTimer(): void {
  const minutes = closeTimerChoice.value === 'custom' ? Number(customCloseMinutes.value) : closeTimerChoice.value
  const deadline = Date.now() + minutes * 60_000
  if (!Number.isInteger(minutes) || minutes < 1 || !Number.isSafeInteger(deadline)) return
  closeDeadline.value = deadline
}
watch(closeDeadline, updateCloseTimer)
function save(): void {
  const media = player.value
  if (!media || !restored || media.seeking || (resumePending && media.currentTime < 2)) return
  resumePending = false
  savePlayback(props.itemId, media.currentTime, media.duration)
}
function measureStartup(stage: 'source' | 'metadata' | 'first-frame'): void {
  if (props.kind !== 'video' || props.openedAt === undefined) return
  try {
    if (stage === 'source') {
      performance.clearMeasures('tgdrive.video.open-to-metadata')
      performance.clearMeasures('tgdrive.video.open-to-first-frame')
      performance.clearMeasures('tgdrive.video.open-to-source')
    }
    performance.measure(`tgdrive.video.open-to-${stage}`, { start: openedAt })
  } catch { /* Timing is optional on older WebViews. */ }
}
function metadata(): void {
  const media = player.value
  if (!media) return
  startupProgress()
  syncPresentation()
  media.playbackRate = rate.value
  duration.value = Number.isFinite(media.duration) ? media.duration : 0
  if (!restored && duration.value > 0) {
    restored = true
    measureStartup('metadata')
    const position = playbackPosition(props.itemId, media.duration)
    resumePending = position > 0
    if (position) {
      media.currentTime = position
      notice.value = `已恢复至 ${mediaTime(position)}`
    }
  }
  if (props.kind === 'audio') ready()
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
  if (pipActive.value) listening.value = false
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
  const media = player.value
  if (!media || !active) return
  if (!media.paused) {
    media.pause()
    return
  }
  const signal = controller.signal
  try {
    await media.play()
  } catch {
    if (!signal.aborted && active) notice.value = '请点击播放器中的播放按钮，或等待缓冲完成。'
  }
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
  if (!video || !element || pipActive.value || pipPending.value || listening.value) return
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
async function leavePictureInPicture(video: SafariVideo): Promise<void> {
  if (document.pictureInPictureElement === video) await document.exitPictureInPicture()
  else if (video.webkitPresentationMode === 'picture-in-picture') video.webkitSetPresentationMode?.('inline')
}
async function pictureInPicture(): Promise<void> {
  const video = videoElement()
  if (!video || !pipAvailable.value || pipDisabled.value) return
  const exiting = pipActive.value
  const signal = controller.signal
  pipPending.value = true
  notice.value = ''
  try {
    if (exiting) await leavePictureInPicture(video)
    else if (document.pictureInPictureEnabled && video.requestPictureInPicture) await video.requestPictureInPicture()
    else video.webkitSetPresentationMode?.('picture-in-picture')
  } catch {
    if (!signal.aborted) notice.value = exiting ? '暂时无法退出画中画。' : '暂时无法进入画中画，请先开始播放。'
  } finally {
    if (signal.aborted) void leavePictureInPicture(video).catch(() => {})
    pipPending.value = false
    syncPresentation()
  }
}
function toggleListening(): void {
  if (listenDisabled.value) return
  listening.value = !listening.value
}
function failed(): void {
  const code = player.value?.error?.code
  if (!code || !active) return
  const unsupported = props.allowDownload
    ? '浏览器无法解码此文件，可以下载后使用本地播放器观看。'
    : '浏览器无法解码此视频。'
  recover(code === 2 ? '媒体传输中断，请重试加载。' : unsupported)
}
function recover(fallback: string): void {
  if (!active) return
  save()
  resetMedia()
  loading.value = false
  error.value = fallback
  if (props.openSource) return
  const signal = controller.signal
  void mediaFailure(props.src, fallback, AbortSignal.any([signal, AbortSignal.timeout(2000)]))
    .then(message => { if (!signal.aborted) error.value = message })
    .catch(() => {})
}
function resetMedia(): void {
  clearTimeout(startupTimer)
  active = false
  controller.abort()
  controller = new AbortController()
  releaseMedia()
  paused.value = true
}
function releaseMedia(): void {
  const media = player.value
  if (!media) return
  const video = videoElement()
  if (video) void leavePictureInPicture(video).catch(() => {})
  media.pause()
  media.removeAttribute('src')
  media.load()
}
function ready(): void {
  const media = player.value
  if (!media || !active || media.readyState < (props.kind === 'audio' ? 1 : 2) || media.seeking) return
  loading.value = false
  if (!startedPlayback) measureStartup('first-frame')
  startedPlayback = true
  clearTimeout(startupTimer)
}
function startupProgress(): void {
  if (!active || startedPlayback || props.kind !== 'video') return
  waitForStartup(Math.max(0, Math.min(startupBudgetMs, startupStarted + nativeVideoStartupLimitMs - performance.now())))
}
function waitForStartup(budget: number): void {
  clearTimeout(startupTimer)
  const message = props.allowDownload ? '媒体启动超时，请重试或下载后播放。' : 'Telegram 直连播放启动超时。'
  startupTimer = setTimeout(() => recover(message), budget)
}
function start(openTime = performance.now()): void {
  resetMedia()
  restored = false
  resumePending = false
  duration.value = 0
  current.value = 0
  loading.value = true
  error.value = ''
  notice.value = ''
  startedPlayback = false
  if (!player.value) return
  active = true
  openedAt = openTime
  startupStarted = performance.now()
  waitForStartup(startupBudgetMs)
  measureStartup('source')
  if (props.openSource) {
    const { signal } = controller
    void props.openSource(player.value, signal).catch((cause) => {
      if (!signal.aborted) recover(cause instanceof Error ? cause.message : 'Telegram 视频读取中断。')
    })
  } else {
    player.value.src = props.src
  }
}
function retry(): void {
  save()
  start()
}
function closeSettings(): void {
  settingsOpen.value = false
  settingsButton.value?.focus()
}
function onKey(event: KeyboardEvent): void {
  if (event.key === 'Escape' && (settingsOpen.value || fullscreenActive.value)) {
    event.preventDefault()
    event.stopPropagation()
    if (settingsOpen.value) closeSettings()
    else void fullscreen()
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
  if (!player.value?.paused) return
  paused.value = true
  if (!player.value?.getAttribute('src')) return
  save()
}
onMounted(() => {
  releaseCovers = holdCoverQueue()
  syncPresentation()
  start(props.openedAt)
  window.addEventListener('pagehide', save)
  document.addEventListener('visibilitychange', save)
  if (props.kind === 'video') {
    updateCloseTimer()
    document.addEventListener('visibilitychange', updateCloseTimer)
  }
  document.addEventListener('fullscreenchange', syncPresentation)
})
onBeforeUnmount(() => {
  save()
  disposed = true
  clearTimeout(closeTimeout)
  clearTimeout(startupTimer)
  controller.abort()
  window.removeEventListener('pagehide', save)
  document.removeEventListener('visibilitychange', save)
  document.removeEventListener('visibilitychange', updateCloseTimer)
  document.removeEventListener('fullscreenchange', syncPresentation)
  if (orientationLocked) screen.orientation?.unlock?.()
  if (document.fullscreenElement === container.value) void document.exitFullscreen().catch(() => {})
  releaseMedia()
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
      <div class="media-screen" :class="{ 'media-quarter-turn': quarterTurn && !listening }">
        <component
          :is="kind"
          v-show="!listening"
          ref="player"
          :controls="kind === 'audio'"
          :disablepictureinpicture="listening ? '' : undefined"
          :style="rotation ? { rotate: rotation + 'deg' } : undefined"
          playsinline
          :preload="kind === 'video' ? 'auto' : 'metadata'"
          :poster="kind === 'video' ? poster : undefined"
          :aria-label="kind === 'video' ? '视频播放' : '音频播放'"
          @loadedmetadata="metadata"
          @progress="startupProgress"
          @durationchange="metadata"
          @timeupdate="update"
          @seeking="seeking"
          @seeked="ready"
          @play="paused = false"
          @pause="pause"
          @ended="pause"
          @waiting="loading = true"
          @loadeddata="ready"
          @canplay="ready"
          @playing="ready"
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
        <div v-if="listening" class="media-listening" role="status">
          <AppIcon name="audio" :size="28" />
          听视频模式
        </div>
        <button
          v-if="kind === 'video' && !listening && paused && !error"
          class="media-stage-play"
          type="button"
          aria-label="开始播放"
          @click="toggle"
        ><AppIcon name="play" :size="28" /></button>
        <span v-if="loading && !error" class="media-loading" role="status">正在缓冲…</span>
        <div v-if="error" class="media-error" role="alert">
          <p>{{ error }}</p>
          <button class="ghost" type="button" @click="retry">重新加载</button>
          <slot name="error-actions" />
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
            <button v-if="allowDownload" class="media-secondary" type="button" aria-label="下载文件" :title="downloadPending ? '正在检查下载…' : '下载文件'" :disabled="downloadPending" @click="emit('download')">
              <AppIcon name="download" />
            </button>
            <button
              v-if="pipAvailable"
              type="button"
              aria-label="画中画"
              :aria-pressed="pipActive"
              :title="pipActive ? '退出画中画' : '画中画'"
              :disabled="pipDisabled"
              @click="pictureInPicture"
            ><AppIcon name="pip" /></button>
            <button
              v-if="kind === 'video'"
              type="button"
              :aria-label="fullscreenActive ? '退出全屏' : '全屏'"
              :title="fullscreenActive ? '退出全屏' : '全屏'"
              :disabled="pipActive || pipPending || listening"
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
        <p v-if="closeSeconds" class="media-notice media-close-countdown" role="timer">将在 {{ mediaTime(closeSeconds) }} 后关闭</p>
        <p v-if="notice" class="media-notice" role="status">{{ notice }}</p>
        <p v-if="downloadStatus" class="media-notice" role="status">{{ downloadStatus }}</p>
      </div>
      <div v-if="settingsOpen" :id="settingsId" class="media-settings" role="group" aria-label="播放设置">
        <header class="media-settings-head">
          <strong>播放设置</strong>
          <span>{{ formatBytes(size) }}</span>
          <button class="icon-btn media-settings-close" type="button" aria-label="关闭播放设置" @click="closeSettings">
            <AppIcon name="close" :size="18" />
          </button>
        </header>
        <div class="media-settings-body">
          <div v-if="kind === 'video'" class="media-buttons">
            <button type="button" :disabled="listenDisabled" @click="toggleListening">
              <AppIcon :name="listening ? 'video' : 'audio'" :size="15" />{{ listening ? '恢复画面' : '听视频' }}
            </button>
          </div>
          <label class="media-setting">播放速度
            <select v-model="rate" aria-label="播放速度" @change="changeRate">
              <option v-for="speed in [0.5, 0.75, 1, 1.25, 1.5, 2]" :key="speed" :value="speed">{{ speed }}×</option>
            </select>
          </label>
          <label class="media-setting">音量
            <input class="media-volume" aria-label="音量" type="range" min="0" max="1" step=".05" v-model.number="volume" @input="changeVolume" />
          </label>
          <form v-if="kind === 'video'" class="media-close-timer" @submit.prevent="startCloseTimer">
            <label class="media-setting">定时关闭
              <select v-model="closeTimerChoice">
                <option v-for="minutes in closeTimerPresets" :key="minutes" :value="minutes">{{ minutes }} 分钟</option>
                <option value="custom">自定义</option>
              </select>
            </label>
            <label v-if="closeTimerChoice === 'custom'" class="media-setting">自定义分钟
              <input v-model.number="customCloseMinutes" type="number" min="1" step="1" required />
            </label>
            <div class="media-buttons">
              <button type="submit">{{ closeSeconds ? '重设定时' : '开始定时' }}</button>
              <button v-if="closeSeconds" type="button" @click="cancelCloseTimer(); settingsButton?.focus()">取消定时</button>
            </div>
          </form>
          <div class="media-buttons media-setting-actions">
            <button v-if="duration" type="button" @click="restart">从头播放</button>
            <button v-if="kind === 'video'" type="button" @click="rotation = (rotation + 90) % 360">旋转视频 {{ rotation }}°</button>
          </div>
          <div v-if="allowDownload" class="media-buttons media-setting-actions media-compact-actions">
            <button type="button" :disabled="downloadPending" @click="emit('download')">
              <AppIcon name="download" :size="15" />{{ downloadPending ? '正在检查…' : '下载文件' }}
            </button>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<style scoped>
.media-player {
  position: relative;
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
  color: var(--text);
  outline-offset: -3px;
}
.media-layout {
  position: relative;
  display: grid;
  grid-template: minmax(0, 1fr) auto / minmax(0, 1fr);
  container: media-layout / inline-size;
}
.media-screen {
  grid-area: 1 / 1;
  position: relative;
  display: grid;
  place-items: center;
  min-height: 180px;
  overflow: hidden;
  background: #030712;
}
.media-screen video {
  display: block;
  width: 100%;
  max-height: 55dvh;
  object-fit: contain;
  cursor: pointer;
}
.media-quarter-turn {
  height: 55dvh;
  container-type: size;
}
.media-screen.media-quarter-turn video {
  position: absolute;
  top: 50%;
  left: 50%;
  width: 100cqh;
  height: 100cqw;
  max-height: none;
  translate: -50% -50%;
}
.media-screen audio {
  width: calc(100% - 36px);
}
.media-listening {
  display: grid;
  justify-items: center;
  gap: 8px;
  color: #dbeafe;
  font-size: 13px;
}
.media-stage-play {
  position: absolute;
  top: 50%;
  left: 50%;
  translate: -50% -50%;
  display: grid;
  place-items: center;
  width: 60px;
  height: 60px;
  border: 1px solid #ffffff40;
  border-radius: 50%;
  background: #101828d9;
  color: white;
}
.media-stage-play:hover {
  background: #263650;
}
.media-loading {
  position: absolute;
  top: 12px;
  left: 12px;
  pointer-events: none;
  padding: 5px 9px;
  border-radius: 6px;
  background: #101828e6;
  color: #dbeafe;
  font-size: 12px;
}
.media-error {
  position: absolute;
  inset: 0;
  display: grid;
  place-content: center;
  justify-items: center;
  gap: 12px;
  padding: 20px;
  background: #101828f5;
  color: #e2e8f0;
  text-align: center;
  font-size: 13px;
  line-height: 1.6;
}
.media-error p { margin: 0; }
.media-error .ghost { color: var(--text); }
.media-controls {
  grid-area: 2 / 1;
  padding: 8px 14px 12px;
  background: var(--panel);
}
.media-timeline {
  display: flex;
  align-items: center;
  gap: 10px;
  color: var(--muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.media-timeline input,
.media-volume {
  min-width: 0;
  height: 24px;
  padding: 0;
  border: 0;
  background: transparent;
  box-shadow: none;
  accent-color: var(--accent);
}
.media-timeline input { flex: 1; }
.media-timeline span:last-child { text-align: right; }
.media-toolbar,
.media-buttons {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 5px;
}
.media-toolbar {
  justify-content: space-between;
  margin-top: 4px;
}
.media-toolbar > :last-child { margin-inline-start: auto; }
.media-buttons button {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 2px;
  min-width: 38px;
  min-height: 38px;
  border: 1px solid transparent;
  border-radius: 7px;
  padding: 0 6px;
  color: var(--text);
  background: transparent;
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}
.media-buttons button:hover:not(:disabled) {
  background: var(--panel-2);
}
.media-buttons .media-play,
.media-buttons button[aria-pressed="true"],
.media-buttons button[aria-expanded="true"] {
  border-color: var(--line);
  background: var(--accent-soft);
  color: var(--accent);
}
.media-player :is(button, select, input):focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 1px;
}
.media-settings {
  grid-area: 1 / 1 / 2 / 2;
  position: absolute;
  z-index: 2;
  inset-inline-end: 8px;
  inset-block-end: 8px;
  display: flex;
  flex-direction: column;
  width: min(300px, calc(100% - 16px));
  max-height: min(340px, calc(100% - 16px));
  overflow: hidden;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--panel);
  box-shadow: var(--shadow);
  font-size: 13px;
}
.media-settings-head {
  display: flex;
  align-items: center;
  gap: 8px;
  flex: none;
  padding: 4px 6px 4px 12px;
  border-bottom: 1px solid var(--line);
}
.media-settings-head span { margin-inline-start: auto; color: var(--muted); }
.media-settings-close { width: 32px; height: 32px; }
.media-settings-body {
  display: grid;
  gap: 8px;
  min-height: 0;
  padding: 10px 12px;
  overflow: auto;
  overscroll-behavior: contain;
}
.media-settings .media-compact-actions { display: none; }
.media-setting {
  display: grid;
  grid-template-columns: 1fr minmax(0, 118px);
  align-items: center;
  gap: 8px;
}
.media-setting :is(select, input[type="number"]) {
  min-height: 34px;
  min-width: 0;
  padding: 4px 8px;
  border: 1px solid var(--line);
  border-radius: 6px;
  background: var(--panel-2);
  color: var(--text);
  font-size: 12px;
}
.media-close-timer {
  display: grid;
  gap: 8px;
}
.media-close-countdown { font-variant-numeric: tabular-nums; }
.media-setting-actions {
  border-top: 1px solid var(--line);
  padding-top: 5px;
}
.media-notice {
  margin: 5px 0 0;
  color: var(--accent);
  font-size: 12px;
  line-height: 1.4;
  overflow-wrap: anywhere;
}
.media-player:is(:fullscreen, .media-expanded) {
  width: 100%;
  height: 100%;
  border: 0;
  border-radius: 0;
  background: #030712;
}
.media-player:is(:fullscreen, .media-expanded) .media-layout {
  container-type: size;
  width: 100%;
  height: 100%;
}
.media-player:is(:fullscreen, .media-expanded) .media-screen {
  height: auto;
  min-height: 0;
}
.media-player:is(:fullscreen, .media-expanded) .media-screen:not(.media-quarter-turn) video {
  position: absolute;
  inset: 0;
  height: 100%;
  max-height: none;
}
.media-fullscreen .media-controls {
  padding-right: max(14px, env(safe-area-inset-right));
  padding-bottom: max(8px, env(safe-area-inset-bottom));
  padding-left: max(14px, env(safe-area-inset-left));
}
@media (orientation: portrait) and (pointer: coarse), (orientation: portrait) and (max-width: 760px) {
  .media-player.media-fullscreen.media-landscape .media-layout {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 100dvh;
    height: 100dvw;
    transform: translate(-50%, -50%) rotate(90deg);
  }
}
@media (pointer: coarse) {
  .media-buttons button,
  .media-settings-close {
    min-width: 44px;
    min-height: 44px;
  }
}
@media (max-width: 760px) {
  .media-screen video { max-height: 48dvh; }
  .media-quarter-turn { height: 48dvh; }
  .media-controls { padding-inline: 8px; }
}
@container media-layout (max-width: 420px) {
  .media-toolbar { gap: 4px; }
  .media-toolbar .media-buttons { flex-wrap: nowrap; gap: 0; }
  .media-toolbar .media-secondary { display: none; }
  .media-settings .media-compact-actions { display: flex; }
  .media-settings { width: calc(100% - 16px); }
}
</style>
