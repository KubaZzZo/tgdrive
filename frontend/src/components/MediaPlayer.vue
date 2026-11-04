<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { mediaTime, playbackPosition, savePlayback } from '@/utils/playback'
import { attachMedia, bufferedSeek, type MediaSession } from '@/utils/fragmentedMp4'

const props = defineProps<{ src: string; itemId: string; kind: 'video' | 'audio' }>()
const player = ref<HTMLMediaElement | null>(null)
const container = ref<HTMLElement | null>(null)
const loading = ref(true)
const error = ref('')
const notice = ref('')
const paused = ref(true)
const duration = ref(0)
const current = ref(0)
const rate = ref(1)
const volume = ref(1)
const muted = ref(false)
let restored = false
let lastSaved = 0
let disposed = false
let session: MediaSession | undefined
let setup: Promise<void> | undefined
let controller = new AbortController()
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
  let target = Math.max(0, Math.min(media.duration - 0.05, value))
  if (streaming.value) {
    const available = bufferedSeek(media.buffered, target)
    if (available === null) return
    if (Math.abs(available - target) > 0.2) notice.value = '此分片视频可拖动到已缓冲的位置，请等待后续内容缓冲。'
    target = available
  }
  if (Math.abs(media.currentTime - target) > 0.05) media.currentTime = target
  current.value = media.currentTime
}
function restart(): void {
  seek(0)
  notice.value = ''
}
function seeking(): void {
  if (streaming.value && player.value) seek(player.value.currentTime)
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
  loading.value = false
  const code = player.value?.error?.code
  error.value =
    code === 2
      ? '视频传输中断，请重试加载。'
      : '浏览器无法播放此文件的格式或编码，可以下载后使用本地播放器观看。'
}
function start(): void {
  controller.abort()
  session?.dispose()
  controller = new AbortController()
  const signal = controller.signal
  const media = player.value
  if (!media) return
  media.pause()
  media.removeAttribute('src')
  media.load()
  streaming.value = false
  if (props.kind === 'audio') { media.src = props.src; return }
  setup = attachMedia(media, props.src, signal, () => {
    if (!signal.aborted) {
      loading.value = false
      error.value = '视频传输中断，请重试加载。'
    }
  }).then(value => {
    if (signal.aborted) value.dispose()
    else { session = value; streaming.value = value.streaming }
  }).catch(() => {
    if (!signal.aborted) { loading.value = false; error.value = '视频加载失败，请重试。' }
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
  start()
  window.addEventListener('pagehide', save)
  document.addEventListener('visibilitychange', save)
})
onBeforeUnmount(() => {
  save()
  disposed = true
  controller.abort()
  session?.dispose()
  window.removeEventListener('pagehide', save)
  document.removeEventListener('visibilitychange', save)
  if (player.value) {
    player.value.pause()
    player.value.removeAttribute('src')
    player.value.load()
  }
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
    <div class="media-screen">
      <component
        :is="kind"
        ref="player"
        controls
        playsinline
        preload="metadata"
        :aria-label="kind === 'video' ? '视频播放' : '音频播放'"
        @loadedmetadata="metadata"
        @durationchange="metadata"
        @timeupdate="update"
        @seeking="seeking"
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
          :value="current"
          :disabled="!duration"
          @input="seek(Number(($event.target as HTMLInputElement).value))"
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
      </div>
      <p v-if="notice" class="media-notice" role="status">
        {{ notice }}
        <button @click="restart">从头播放</button>
      </p>
      <p class="media-hint">{{ streaming ? '流式播放 · 可在已缓冲范围内拖动进度' : '自动记住播放位置' }} · 空格播放/暂停 · ← → 快进快退</p>
    </div>
  </div>
</template>
