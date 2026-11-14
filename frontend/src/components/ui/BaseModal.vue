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
  const fullscreen = document.fullscreenElement
  const fullscreenInside = fullscreen && card.value?.contains(fullscreen) ? fullscreen : null
  if (event.key === 'Escape') {
    if (fullscreenInside) return
    event.preventDefault()
    emit('close')
  }
  if (event.key !== 'Tab' || !card.value) return
  const controls = Array.from(
    (fullscreenInside ?? card.value).querySelectorAll<HTMLElement>(
      'button:not(:disabled), input:not(:disabled), select:not(:disabled), a[href], [tabindex="0"]',
    ),
  ).filter((element) => element.getClientRects().length > 0)
  const first = controls[0]
  const last = controls.at(-1)
  if (!first) {
    event.preventDefault()
    if (fullscreenInside instanceof HTMLElement) fullscreenInside.focus()
    else card.value.focus()
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

<style scoped>
.modal {
  position: fixed;
  inset: 0;
  z-index: 50;
  background: #14233a66;
  backdrop-filter: blur(3px);
  display: grid;
  place-items: center;
  padding: 24px;
}
.modal-card {
  width: min(540px, 100%);
  max-height: 90dvh;
  display: flex;
  flex-direction: column;
  background: white;
  border: 1px solid var(--line);
  border-radius: 16px;
  box-shadow: var(--shadow);
}
.modal-card.wide {
  width: min(980px, 100%);
}
.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;
  padding: 18px 22px;
  border-bottom: 1px solid var(--line);
}
.modal-head h2 {
  margin: 0;
  font-size: 16px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.modal-close {
  display: grid;
  place-items: center;
  width: 36px;
  height: 36px;
  border: 0;
  border-radius: 50%;
  background: var(--bg);
  color: var(--muted);
  font-size: 22px;
  flex-shrink: 0;
}
.modal-body {
  padding: 22px;
  overflow: auto;
}
.modal-footer {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 10px;
  padding: 16px 22px;
  border-top: 1px solid var(--line);
}
.modal-enter-active,
.modal-leave-active {
  transition:
  opacity 0.16s,
  transform 0.16s;
}
.modal-enter-from,
.modal-leave-to {
  opacity: 0;
}
@media (max-width: 760px) {
  .modal {
    padding: 14px;
  }
  .modal-body {
    padding: 18px;
  }
  .modal-card {
    max-height: 88dvh;
  }
  .modal-close {
    width: 44px;
    height: 44px;
  }
}
.modal {
  grid-template-columns: minmax(0, 1fr);
}
.modal-card {
  min-width: 0;
  max-width: 100%;
}
.modal-head h2 {
  min-width: 0;
}
.modal:has(:global(.media-expanded)) { padding: 0; }
.modal-card:has(:global(.media-expanded)) {
  width: 100%;
  height: 100%;
  max-height: none;
  min-height: 0;
  border: 0;
  border-radius: 0;
}
.modal-card:has(:global(.media-expanded)) > :is(.modal-head, .modal-footer) { display: none; }
.modal-card:has(:global(.media-expanded)) > .modal-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  padding: 0;
}
</style>
