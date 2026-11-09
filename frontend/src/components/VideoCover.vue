<script setup lang="ts">
import { ref, onBeforeUnmount, onMounted } from 'vue'
import type { DriveItem } from '@/api/types'
import { api } from '@/api/client'
import { attachMedia, type MediaSession } from '@/utils/fragmentedMp4'
import { acquireCoverSlot } from '@/utils/coverQueue'
import AppIcon from './AppIcon.vue'
const props = defineProps<{ item: DriveItem }>()
const root = ref<HTMLElement | null>(null)
const image = ref('')
let observer: IntersectionObserver | undefined
let controller: AbortController | undefined
let session: MediaSession | undefined
let video: HTMLVideoElement | undefined
let timer: ReturnType<typeof setTimeout> | undefined
let release: (() => void) | undefined
function stop(): void {
  clearTimeout(timer)
  controller?.abort(); session?.dispose()
  if (video) { video.pause(); video.removeAttribute('src'); video.load(); video.remove(); video = undefined }
  release?.(); release = undefined
}
async function firstFrame(): Promise<void> {
  if (controller) return
  controller = new AbortController()
  const signal = controller.signal
  const url = api.downloadUrl(props.item.id)
  const finish = () => { if (controller?.signal === signal) stop() }
  try {
    const slot = await acquireCoverSlot(signal, () => {
      if (controller?.signal !== signal) return
      stop()
      controller = undefined
      void firstFrame()
    })
    if (signal.aborted) { slot(); return }
    release = slot
    const response = await fetch(url, { method: 'HEAD', signal })
    signal.throwIfAborted()
    if (!response.ok) { finish(); return }
  }
  catch { finish(); return }
  video = document.createElement('video')
  video.muted = true; video.playsInline = true; video.preload = 'auto'
  video.style.cssText = 'position:fixed;width:1px;height:1px;opacity:0;pointer-events:none'
  document.body.append(video)
  video.addEventListener('loadeddata', () => {
    if (!video?.videoWidth) return
    try {
      const canvas = document.createElement('canvas')
      canvas.width = 320; canvas.height = Math.round(320 * video.videoHeight / video.videoWidth)
      canvas.getContext('2d')?.drawImage(video, 0, 0, canvas.width, canvas.height)
      image.value = canvas.toDataURL('image/jpeg', .7)
    } finally { finish() }
  }, { once: true })
  timer = setTimeout(finish, 20000)
  try {
    const value = await attachMedia(video, url, signal, finish)
    if (signal.aborted) value.dispose()
    else session = value
  } catch { finish() }
}
onMounted(() => {
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      image.value = api.thumbnailUrl(props.item.id, props.item.updated_at)
      observer?.disconnect()
    }
  })
  if (root.value) observer.observe(root.value)
})
onBeforeUnmount(() => { observer?.disconnect(); stop() })
</script>
<template>
  <span ref="root" class="video-cover">
    <img v-if="image" :src="image" :alt="item.name" loading="lazy" @error="image = ''; firstFrame()" />
    <AppIcon v-else name="video" :size="32" />
  </span>
</template>
