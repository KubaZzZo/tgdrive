<script setup lang="ts">
import { ref } from 'vue'

import type { DriveFilters } from '@/utils/driveLocation'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ filters: DriveFilters }>()
const emit = defineEmits<{ close: []; apply: [filters: DriveFilters] }>()
const mib = 1024 ** 2
const from = ref(props.filters.created_from ?? '')
const to = ref(props.filters.created_to ?? '')
const min = ref(props.filters.min_size === undefined ? '' : String(props.filters.min_size / mib))
const max = ref(props.filters.max_size === undefined ? '' : String(props.filters.max_size / mib))
const extension = ref(props.filters.extension ?? '')
const error = ref('')

function apply(): void {
  const minSize = min.value === '' ? undefined : Math.round(Number(min.value) * mib)
  const maxSize = max.value === '' ? undefined : Math.round(Number(max.value) * mib)
  if (from.value && to.value && from.value > to.value) {
    error.value = '起始日期不能晚于结束日期'
    return
  }
  if ([minSize, maxSize].some((value) => value !== undefined && (!Number.isSafeInteger(value) || value < 0))
    || (minSize !== undefined && maxSize !== undefined && minSize > maxSize)) {
    error.value = '请输入有效的文件大小范围'
    return
  }
  const suffix = extension.value.trim()
  if (suffix && !/^\.?[a-zA-Z0-9][a-zA-Z0-9._+-]{0,31}$/.test(suffix)) {
    error.value = '请输入有效扩展名，例如 pdf 或 tar.gz'
    return
  }
  emit('apply', {
    created_from: from.value || undefined, created_to: to.value || undefined,
    min_size: minSize, max_size: maxSize, extension: suffix || undefined,
  })
}
</script>

<template>
  <BaseModal :open="true" title="高级搜索" @close="emit('close')">
    <form class="advanced-search-form" @submit.prevent="apply">
      <fieldset>
        <legend>上传日期（UTC）</legend>
        <label>起始日期<input v-model="from" type="date" min="0001-01-01" max="9999-12-31" /></label>
        <label>结束日期<input v-model="to" type="date" min="0001-01-01" max="9999-12-31" /></label>
      </fieldset>
      <fieldset>
        <legend>文件大小（MiB）</legend>
        <label>最小值<input :value="min" type="number" min="0" step="any" @input="min = ($event.target as HTMLInputElement).value" /></label>
        <label>最大值<input :value="max" type="number" min="0" step="any" @input="max = ($event.target as HTMLInputElement).value" /></label>
      </fieldset>
      <label>扩展名<input v-model="extension" placeholder="例如 pdf" maxlength="33" /></label>
      <p v-if="error" class="inline-error" role="alert">{{ error }}</p>
      <div class="operation-actions">
        <button class="ghost" type="button" @click="emit('apply', {})">清除筛选</button>
        <button class="primary">应用筛选</button>
      </div>
    </form>
  </BaseModal>
</template>
