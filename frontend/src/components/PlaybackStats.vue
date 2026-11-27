<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { api } from '@/api/client'
import { formatBytes } from '@/utils/format'
import { bufferedSeconds, TransferRate } from '@/utils/playbackStats'

const props = defineProps<{
  media: HTMLMediaElement | null
  itemId?: string
  sessionId: string
  receivedBytes?: number
}>()
const speed = ref<number | null>(null)
const buffered = ref(0)
const rate = new TransferRate()
const lifetime = new AbortController()
let timer: ReturnType<typeof setInterval>
let polling = false

async function sample(): Promise<void> {
  buffered.value = props.media ? bufferedSeconds(props.media) : 0
  if (polling) return
  polling = true
  try {
    const result = props.itemId
      ? await api.playbackTransfer(props.itemId, props.sessionId, AbortSignal.any([lifetime.signal, AbortSignal.timeout(2500)]))
      : { bytes: props.receivedBytes ?? null, sampled_at: performance.now() }
    if (!lifetime.signal.aborted) speed.value = rate.sample(result.bytes, result.sampled_at)
  } catch {
    speed.value = rate.sample(null, performance.now())
  } finally {
    polling = false
  }
}
onMounted(() => { void sample(); timer = setInterval(() => void sample(), 1000) })
onBeforeUnmount(() => { lifetime.abort(); clearInterval(timer) })
</script>

<template>
  <div class="playback-stats" aria-label="播放传输状态">
    <span v-if="itemId || receivedBytes !== undefined">{{ itemId ? '服务器发送' : 'Telegram 下载' }} {{ speed === null ? '—' : formatBytes(speed) + '/s' }}</span>
    <span>已缓冲 {{ Math.floor(buffered) }} 秒</span>
  </div>
</template>

<style scoped>
.playback-stats { display: flex; flex-wrap: wrap; gap: 4px 16px; color: var(--muted); font-size: 12px; font-variant-numeric: tabular-nums; }
</style>
