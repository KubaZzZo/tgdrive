<script setup lang="ts">
import { nextTick, ref, watch } from 'vue'

import BaseModal from '@/components/ui/BaseModal.vue'

const props = withDefaults(
  defineProps<{
    open: boolean
    title: string
    label: string
    initial?: string
    confirmText?: string
    placeholder?: string
    maxLength?: number
  }>(),
  { initial: '', confirmText: '确定', placeholder: '', maxLength: 255 },
)
const emit = defineEmits<{ close: []; confirm: [value: string] }>()
const value = ref('')
const input = ref<HTMLInputElement | null>(null)

watch(
  () => props.open,
  async (open) => {
    if (!open) return
    value.value = props.initial
    await nextTick()
    input.value?.focus()
    input.value?.select()
  },
)

function submit(): void {
  const cleaned = value.value.trim()
  if (cleaned) emit('confirm', cleaned)
}
</script>

<template>
  <BaseModal :open="open" :title="title" @close="emit('close')">
    <form class="dialog-form" @submit.prevent="submit">
      <label>{{ label }}</label>
      <input ref="input" v-model="value" :placeholder="placeholder" :maxlength="maxLength" required />
    </form>
    <template #footer>
      <button class="ghost" type="button" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="!value.trim()" @click="submit">
        {{ confirmText }}
      </button>
    </template>
  </BaseModal>
</template>

<style scoped>
.dialog-form label {
  display: block;
  font-size: 12px;
  color: var(--muted);
  margin-bottom: 9px;
}
</style>
