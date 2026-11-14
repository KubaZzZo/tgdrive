<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import type { DriveItem } from '@/api/types'
import { api } from '@/api/client'
import { acquireCoverSlot } from '@/utils/coverQueue'
import AppIcon from '@/components/ui/AppIcon.vue'

const props = defineProps<{ item: DriveItem }>()
const root = ref<HTMLElement | null>(null)
const image = ref('')
let observer: IntersectionObserver | undefined
let controller: AbortController | undefined
let release: (() => void) | undefined
let visible = false

function finish(): void {
  release?.()
  release = undefined
}
function stop(): void {
  controller?.abort()
  finish()
  image.value = ''
}
async function load(): Promise<void> {
  stop()
  controller = new AbortController()
  const signal = controller.signal
  try {
    const slot = await acquireCoverSlot(signal)
    if (signal.aborted) { slot(); return }
    release = slot
    image.value = api.thumbnailUrl(props.item.id, props.item.updated_at)
  } catch { /* The card left the page before a slot was available. */ }
}

watch(() => [props.item.id, props.item.updated_at], () => { if (visible) void load() })
onMounted(() => {
  observer = new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) {
      visible = true
      observer?.disconnect()
      void load()
    }
  })
  if (root.value) observer.observe(root.value)
})
onBeforeUnmount(() => { observer?.disconnect(); stop() })
</script>

<template>
  <span ref="root" class="video-cover">
    <img v-if="image" :key="image" :src="image" :alt="item.name" decoding="async" @load="finish" @error="stop" />
    <AppIcon v-else name="video" :size="32" />
  </span>
</template>

<style scoped>
.video-cover {display:flex;width:100%;height:100%;align-items:center;justify-content:center;overflow:hidden;border-radius:inherit}
.video-cover img {width:100%;height:100%;object-fit:cover}
</style>
