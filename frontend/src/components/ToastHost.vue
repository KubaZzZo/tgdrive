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
