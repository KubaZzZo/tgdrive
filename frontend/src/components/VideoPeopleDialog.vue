<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { api } from '@/api/client'
import type { DriveItem, PersonLabel } from '@/api/types'
import { useToastStore } from '@/stores/toast'
import BaseModal from './BaseModal.vue'

const props = defineProps<{ item: DriveItem }>()
const emit = defineEmits<{ close: []; saved: [] }>()
const toast = useToastStore()
const original = ref<PersonLabel[]>([])
const selected = ref<PersonLabel[]>([])
const options = ref<PersonLabel[]>([])
const query = ref('')
const page = ref(1)
const total = ref(0)
const pageSize = 24
const loading = ref(true)
const searching = ref(true)
const saving = ref(false)
const loadError = ref('')
const searchError = ref('')
const saveError = ref('')
const loadRetry = ref(0)
const searchRetry = ref(0)
const selectedKeys = computed(() => new Set(selected.value.map(label => label.key)))
const originalKeys = computed(() => new Set(original.value.map(label => label.key)))
const additions = computed(() => selected.value.filter(label => !originalKeys.value.has(label.key)))
const removals = computed(() => original.value.filter(label => !selectedKeys.value.has(label.key)))

watch([() => props.item.id, loadRetry], async ([id], _, onCleanup) => {
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  loading.value = true
  loadError.value = ''
  try {
    const result = await api.videoPeople(id, controller.signal)
    if (controller.signal.aborted) return
    original.value = result.people
    selected.value = result.people
  } catch (cause) {
    if (!controller.signal.aborted)
      loadError.value = cause instanceof Error ? cause.message : '无法读取视频标签'
  } finally {
    if (!controller.signal.aborted) loading.value = false
  }
}, { immediate: true })

watch([query, page, searchRetry], async ([name, current], _, onCleanup) => {
  const controller = new AbortController()
  onCleanup(() => controller.abort())
  searching.value = true
  searchError.value = ''
  try {
    const result = await api.listPeople(name.trim(), pageSize, (current - 1) * pageSize, controller.signal)
    if (controller.signal.aborted) return
    const lastPage = Math.max(1, Math.ceil(result.total / pageSize))
    if (current > lastPage) {
      page.value = lastPage
      return
    }
    options.value = result.people
    total.value = result.total
  } catch (cause) {
    if (!controller.signal.aborted)
      searchError.value = cause instanceof Error ? cause.message : '无法读取标签列表'
  } finally {
    if (!controller.signal.aborted) searching.value = false
  }
}, { immediate: true })

function toggle(label: PersonLabel): void {
  selected.value = selectedKeys.value.has(label.key)
    ? selected.value.filter(value => value.key !== label.key)
    : [...selected.value, label]
}

async function save(): Promise<void> {
  if (saving.value || loading.value || loadError.value) return
  saving.value = true
  saveError.value = ''
  try {
    await api.editVideoPeople(props.item.id, additions.value.map(label => label.name), removals.value.map(label => label.name))
    toast.success('视频标签已保存')
    emit('saved')
  } catch (cause) {
    saveError.value = cause instanceof Error ? cause.message : '保存视频标签失败'
  } finally {
    saving.value = false
  }
}
</script>

<template>
  <BaseModal open title="分配人物标签" @close="!saving && emit('close')">
    <p class="dialog-message">{{ item.name }}</p>
    <p v-if="loading" role="status">正在读取视频标签…</p>
    <div v-else-if="loadError" class="inline-error" role="alert">
      {{ loadError }} <button type="button" class="text-btn" @click="loadRetry++">重新加载</button>
    </div>
    <div v-else class="label-editor">
      <div v-if="selected.length" class="label-editor-selected" aria-label="已选标签">
        <button v-for="label in selected" :key="label.key" class="ghost" type="button"
          :aria-label="`移除 ${label.display_name}`" :disabled="saving" @click="toggle(label)">{{ label.display_name }} ×</button>
      </div>
      <label>
        <span class="sr-only">搜索人物标签</span>
        <input v-model="query" type="search" placeholder="搜索人物标签…" maxlength="80" @input="page = 1" />
      </label>
      <p v-if="searching" role="status">正在读取标签列表…</p>
      <div v-else-if="searchError" class="inline-error" role="alert">
        {{ searchError }} <button type="button" class="text-btn" @click="searchRetry++">重新加载</button>
      </div>
      <div v-else-if="options.length" class="label-editor-options" role="group" aria-label="可分配的标签">
        <label v-for="label in options" :key="label.key">
          <input type="checkbox" :checked="selectedKeys.has(label.key)" :disabled="saving" @change="toggle(label)" />
          <span>{{ label.display_name }}<small v-if="label.display_name !== label.name"> {{ label.name }}</small></span>
        </label>
      </div>
      <p v-else class="muted">{{ query.trim() ? '没有匹配的标签' : '请先在「人物视频」中新增标签。' }}</p>
      <footer v-if="total > pageSize && !searchError" class="pagination">
        <span>{{ page }} / {{ Math.ceil(total / pageSize) }}</span>
        <div>
          <button type="button" class="ghost" :disabled="page <= 1 || searching" @click="page--">上一页</button>
          <button type="button" class="ghost" :disabled="page * pageSize >= total || searching" @click="page++">下一页</button>
        </div>
      </footer>
      <p v-if="saveError" class="inline-error" role="alert">{{ saveError }}</p>
    </div>
    <template #footer>
      <button class="ghost" type="button" :disabled="saving" @click="emit('close')">取消</button>
      <button class="primary" type="button" :disabled="loading || !!loadError || saving || !(additions.length || removals.length)" @click="save">保存</button>
    </template>
  </BaseModal>
</template>

<style scoped>
.label-editor { display: grid; gap: 12px; margin-top: 12px; }
.label-editor-selected { display: flex; flex-wrap: wrap; gap: 6px; }
.label-editor input[type="search"] { width: 100%; }
.label-editor-options { display: grid; max-height: 36dvh; overflow: auto; }
.label-editor-options label { display: flex; align-items: center; gap: 8px; padding: 6px 2px; overflow-wrap: anywhere; }
.label-editor-options small { color: var(--muted); }
</style>
