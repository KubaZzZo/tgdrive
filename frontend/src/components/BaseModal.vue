<script setup lang="ts">
import { nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from 'vue'

const props = withDefaults(defineProps<{ open: boolean; title: string; wide?: boolean }>(), {
  wide: false,
})
const emit = defineEmits<{ close: [] }>()
const titleId = useId()
const card = ref<HTMLElement | null>(null)
let previousOverflow = ''
let previousFocus: HTMLElement | null = null
let locked = false

function release(): void {
  if (!locked) return
  document.body.style.overflow = previousOverflow
  if (previousFocus?.isConnected) previousFocus.focus()
  locked = false
}
function onKeydown(event: KeyboardEvent): void {
  if (!props.open) return
  if (event.key === 'Escape') {
    event.preventDefault()
    emit('close')
  }
  if (event.key !== 'Tab' || !card.value) return
  const controls = Array.from(
    card.value.querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
    ),
  ).filter((element) => element.getClientRects().length > 0)
  const first = controls[0]
  const last = controls.at(-1)
  if (!first) {
    event.preventDefault()
    card.value.focus()
    return
  }
  if (
    event.shiftKey &&
    (document.activeElement === first || document.activeElement === card.value)
  ) {
    event.preventDefault()
    last?.focus()
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault()
    first.focus()
  }
}
watch(
  () => props.open,
  async (open) => {
    if (!open) {
      release()
      return
    }
    previousOverflow = document.body.style.overflow
    previousFocus = document.activeElement as HTMLElement | null
    document.body.style.overflow = 'hidden'
    locked = true
    await nextTick()
    if (props.open)
      (card.value?.querySelector<HTMLElement>('input:not(:disabled)') ?? card.value)?.focus()
  },
  { immediate: true },
)
onMounted(() => window.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  window.removeEventListener('keydown', onKeydown)
  release()
})
</script>
<template>
  <Teleport to="body">
    <Transition name="modal">
      <div
        v-if="open"
        class="modal"
        role="dialog"
        aria-modal="true"
        :aria-labelledby="titleId"
        @click.self="emit('close')"
      >
        <div ref="card" class="modal-card" :class="{ wide }" tabindex="-1">
          <header class="modal-head">
            <h2 :id="titleId">{{ title }}</h2>
            <button class="modal-close" type="button" aria-label="关闭" @click="emit('close')">
              ×
            </button>
          </header>
          <div class="modal-body"><slot /></div>
          <footer v-if="$slots.footer" class="modal-footer"><slot name="footer" /></footer>
        </div>
      </div>
    </Transition>
  </Teleport>
</template>
