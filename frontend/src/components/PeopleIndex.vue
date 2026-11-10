<script setup lang="ts">
import { ref, watch } from 'vue'

import { api } from '@/api/client'
import type { VideoPerson } from '@/api/types'
import { useSyncStore } from '@/stores/sync'
import AppIcon from './AppIcon.vue'

const sync = useSyncStore()
const query = ref('')
const page = ref(1)
const pageSize = 48
const people = ref<VideoPerson[]>([])
const total = ref(0)
const loading = ref(true)
const error = ref('')
const retry = ref(0)

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
      <label class="sr-only" for="people-search">搜索人名</label>
      <input id="people-search" v-model="query" type="search" placeholder="输入中文名或原名…" maxlength="80" @input="page = 1" />
    </div>
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
      <a v-for="person in people" :key="person.name" class="person-card" :href="`#people?name=${encodeURIComponent(person.name)}`">
        <span class="file-art video"><AppIcon name="people" :size="21" /></span>
        <span class="person-card-label">
          <strong>{{ person.display_name }}</strong>
          <small v-if="person.display_name !== person.name">{{ person.name }}</small>
          <small>{{ person.count }} 部视频</small>
        </span>
        <AppIcon name="chevron" :size="14" />
      </a>
    </div>
    <footer v-if="total > pageSize" class="pagination">
      <span>{{ total }} 位人物</span>
      <div>
        <button class="ghost" type="button" :disabled="page <= 1" @click="page--">上一页</button>
        <span>{{ page }} / {{ Math.ceil(total / pageSize) }}</span>
        <button class="ghost" type="button" :disabled="page * pageSize >= total" @click="page++">下一页</button>
      </div>
    </footer>
  </template>
  <div v-else class="empty">
    <span class="empty-art"><AppIcon :name="query.trim() ? 'search' : 'people'" :size="42" /></span>
    <h2>{{ query.trim() ? '没有找到匹配的人名' : '还没有识别到人物' }}</h2>
    <p>{{ query.trim() ? '试试只输入名字的一部分。' : '在频道视频描述里写上「人物：张三、李四」，同步后就能在这里查看。' }}</p>
  </div>
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
  background: white;
  color: var(--text);
  text-decoration: none;
}
.person-card:hover { border-color: var(--accent); }
.person-card-label { display: grid; gap: 4px; flex: 1; min-width: 0; overflow-wrap: anywhere; }
.person-card-label small { color: var(--muted); }
</style>
