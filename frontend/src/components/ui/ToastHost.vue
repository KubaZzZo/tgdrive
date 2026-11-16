<script setup lang="ts">
import { useToastStore } from '@/stores/toast'

const toast = useToastStore()
</script>

<template>
  <div class="toast-stack" aria-live="polite">
    <TransitionGroup name="toast">
      <button
        v-for="item in toast.items"
        :key="item.id"
        class="toast"
        :class="`toast-${item.tone}`"
        type="button"
        @click="toast.dismiss(item.id)"
      >
        {{ item.message }}
      </button>
    </TransitionGroup>
  </div>
</template>

<style scoped>
.toast-stack {
  position: fixed;
  left: 50%;
  top: 90px;
  transform: translateX(-50%);
  z-index: 70;
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: center;
  pointer-events: none;
}
.toast {
  pointer-events: auto;
  border: 1px solid var(--line);
  border-radius: 9px;
  padding: 12px 20px;
  background: var(--panel);
  color: var(--text);
  font-size: 12px;
  box-shadow: var(--shadow);
  max-width: min(560px, calc(100vw - 32px));
}
.toast-error {
  background: var(--danger-soft);
  color: var(--danger-text);
  border-color: var(--danger-border);
}
.toast-success {
  background: var(--ok-soft);
  color: var(--ok);
  border-color: var(--ok-border);
}
.toast-enter-active,
.toast-leave-active {
  transition:
  opacity 0.16s,
  transform 0.16s;
}
.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(-5px);
}
@media (max-width: 760px) {
  .toast-stack {
    top: 76px;
  }
}
</style>
