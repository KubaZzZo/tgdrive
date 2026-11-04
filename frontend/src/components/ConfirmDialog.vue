<script setup lang="ts">
import BaseModal from './BaseModal.vue'

withDefaults(
  defineProps<{ open: boolean; title: string; message: string; confirmText?: string; danger?: boolean }>(),
  { confirmText: '确定', danger: false },
)
const emit = defineEmits<{ close: []; confirm: [] }>()
</script>

<template>
  <BaseModal :open="open" :title="title" @close="emit('close')">
    <p class="dialog-message">{{ message }}</p>
    <template #footer>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
      <button class="primary" :class="{ danger }" type="button" @click="emit('confirm')">
        {{ confirmText }}
      </button>
    </template>
  </BaseModal>
</template>
