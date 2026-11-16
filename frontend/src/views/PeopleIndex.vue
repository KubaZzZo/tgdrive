<script setup lang="ts">
import { ref, watch } from 'vue'

import { RouterLink } from 'vue-router'

import { api } from '@/api/client'
import type { VideoPerson } from '@/api/types'
import { useSyncStore } from '@/stores/sync'
import { useToastStore } from '@/stores/toast'
import AppIcon from '@/components/ui/AppIcon.vue'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
import PromptDialog from '@/components/ui/PromptDialog.vue'

const sync = useSyncStore()
const toast = useToastStore()
const query = ref('')
const page = ref(1)
const pageSize = 48
const people = ref<VideoPerson[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref('')
const retry = ref(0)
const adding = ref(false)
const deleting = ref<VideoPerson | null>(null)
const saving = ref(false)

async function createLabel(name: string): Promise<void> {
  if (saving.value) return
  saving.value = true
  try {
    await api.createPeopleLabel(name)
    adding.value = false
    query.value = name
    page.value = 1
    retry.value++
    toast.success('标签已保存')
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : '新增标签失败')
  } finally {
    saving.value = false
  }
}

async function deleteLabel(): Promise<void> {
  if (!deleting.value || saving.value) return
  saving.value = true
  try {
    await api.deletePeopleLabel(deleting.value.name)
    deleting.value = null
    retry.value++
    toast.success('标签已删除')
  } catch (cause) {
    toast.error(cause instanceof Error ? cause.message : '删除标签失败')
  } finally {
    saving.value = false
  }
}

watch(
  [query, page, () => sync.status?.revision ?? 0, retry],
  async ([name, current], _, onCleanup) => {
    const controller = new AbortController()
    onCleanup(() => controller.abort())
    loading.value = true
    error.value = ''
    try {
      const result = await api.listPeople(name.trim(), pageSize, (current - 1) * pageSize, controller.signal)
      if (controller.signal.aborted) return
      const lastPage = Math.max(1, Math.ceil(result.total / pageSize))
      if (current > lastPage) {
        page.value = lastPage
        return
      }
      people.value = result.people
      total.value = result.total
    } catch (cause) {
      if (!controller.signal.aborted)
        error.value = cause instanceof Error ? cause.message : '无法加载人物列表'
    } finally {
      if (!controller.signal.aborted) loading.value = false
    }
  },
  { immediate: true },
)
</script>

<template>
  <div class="library-controls">
    <div class="search-field">
      <AppIcon name="search" :size="19" />
      <label class="sr-only" for="people-search">搜索标签</label>
      <input id="people-search" v-model="query" type="search" placeholder="搜索人物标签…" maxlength="80" @input="page = 1" />
    </div>
    <RouterLink class="ghost people-videos-link" :to="{ name: 'people', query: { videos: '1' } }">全部视频</RouterLink>
    <button class="primary" type="button" @click="adding = true">新增标签</button>
  </div>
  <div v-if="error" class="inline-error" role="alert">
    <span>{{ error }}</span>
    <button type="button" class="text-btn" @click="retry++">重新加载</button>
  </div>
  <div v-else-if="loading" class="table-wrap skeleton-wrap" aria-label="正在加载人物" aria-busy="true">
    <div v-for="row in 3" :key="row" class="skeleton-row" />
  </div>
  <template v-else-if="people.length">
    <div class="people-grid">
      <div v-for="person in people" :key="person.key" class="person-card">
        <RouterLink class="person-card-link" :to="{ name: 'people', query: { name: person.name } }">
          <span class="file-art video"><AppIcon name="people" :size="21" /></span>
          <span class="person-card-label">
            <strong>{{ person.display_name }}</strong>
            <small v-if="person.display_name !== person.name">{{ person.name }}</small>
            <small>{{ person.count }} 部视频</small>
          </span>
        </RouterLink>
        <button class="text-btn danger-text" type="button" :aria-label="`删除标签 ${person.display_name}`" @click="deleting = person">删除</button>
      </div>
    </div>
    <footer v-if="total > pageSize" class="pagination">
      <span>{{ total }} 个标签</span>
      <div>
        <button class="ghost" type="button" :disabled="page <= 1" @click="page--">上一页</button>
        <span>{{ page }} / {{ Math.ceil(total / pageSize) }}</span>
        <button class="ghost" type="button" :disabled="page * pageSize >= total" @click="page++">下一页</button>
      </div>
    </footer>
  </template>
  <div v-else class="empty">
    <span class="empty-art"><AppIcon :name="query.trim() ? 'search' : 'people'" :size="42" /></span>
    <h2>{{ query.trim() ? '没有找到匹配的标签' : '还没有人物标签' }}</h2>
  </div>
  <PromptDialog :open="adding" title="新增人物标签" label="标签名称" :max-length="80"
    confirm-text="新增" @close="adding = false" @confirm="createLabel" />
  <ConfirmDialog :open="!!deleting" title="删除人物标签"
    :message="`确定删除「${deleting?.display_name ?? ''}」？此标签会从所有关联视频上移除，视频本身会保留。`"
    confirm-text="删除" danger @close="deleting = null" @confirm="deleteLabel" />
</template>

<style scoped>
.people-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 190px), 1fr));
  gap: 12px;
}
.person-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 14px;
  border: 1px solid var(--line);
  border-radius: 12px;
  background: var(--panel);
  color: var(--text);
  text-decoration: none;
}
.person-card:hover { border-color: var(--accent); }
.person-card-link {
  display: flex;
  align-items: center;
  gap: 10px;
  flex: 1;
  min-width: 0;
  color: inherit;
  text-decoration: none;
}
.people-videos-link { display: inline-flex; align-items: center; text-decoration: none; }
.person-card-label { display: grid; gap: 4px; flex: 1; min-width: 0; overflow-wrap: anywhere; }
.person-card-label small { color: var(--muted); }
</style>

<style scoped src="../styles/library.css"></style>
