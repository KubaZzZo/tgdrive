<script setup lang="ts">
import { computed, ref } from 'vue'
import { api, type IndexRestorePreview } from '@/api/client'

const file = ref<File>()
const includeLinks = ref(false)
const preview = ref<IndexRestorePreview>()
const busy = ref(false)
const error = ref('')
const result = ref('')
const labels: Record<string, string> = {
  items: '文件与目录', file_parts: '文件分段', channel_messages: '同步消息',
  channel_bot_files: 'Bot 文件映射', channel_bot_covers: 'Bot 封面映射', people_labels: '人物名称',
  video_people: '人物标签', video_people_edits: '人物标签编辑', shares: '分享链接', image_hosts: '图床直链',
}
const total = computed(() => Object.values(preview.value?.added ?? {}).reduce((sum, count) => sum + count, 0))

function reset(): void {
  preview.value = undefined
  error.value = ''
  result.value = ''
}

function select(event: Event): void {
  file.value = (event.target as HTMLInputElement).files?.[0]
  reset()
}

async function restore(confirm: boolean): Promise<void> {
  if (!file.value || busy.value || (confirm && (!preview.value || !total.value))) return
  busy.value = true
  error.value = ''
  result.value = ''
  const id = confirm ? preview.value?.preview_id : undefined
  preview.value = undefined
  try {
    const data = await api.restoreIndex(file.value, includeLinks.value, id)
    if (confirm) {
      result.value = `已补入 ${data.added.items} 个文件或目录、${Object.values(data.added).reduce((sum, n) => sum + n, 0)} 条索引记录。`
    } else {
      preview.value = data
    }
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '处理备份失败'
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <section class="restore-index" aria-labelledby="restore-title">
    <h3 id="restore-title">从备份补回索引</h3>
    <label>
      选择频道索引 ZIP
      <input type="file" accept=".zip,application/zip" :disabled="busy" @change="select" />
    </label>
    <label class="restore-option">
      <input type="checkbox" v-model="includeLinks" :disabled="busy" @change="reset" />
      恢复分享和图床直链
    </label>
    <button type="button" class="ghost" :disabled="!file || busy" @click="restore(false)">预览差异</button>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <p v-if="result" role="status">{{ result }}</p>
    <div v-if="preview" class="restore-preview">
      <p>目标频道 {{ preview.channel_id }} · 备份版本 {{ preview.backup.app_version }} · 数据库 {{ preview.backup.source_schema_version }}</p>
      <dl>
        <template v-for="(count, name) in preview.added" :key="name">
          <template v-if="count"><dt>{{ labels[name] ?? name }}</dt><dd>新增 {{ count }}</dd></template>
        </template>
        <dt>相同文件或目录</dt><dd>{{ preview.unchanged_items }}</dd>
        <dt>冲突</dt><dd>{{ preview.conflict_count }}</dd>
        <dt>跳过的回收站条目和外链</dt><dd>{{ preview.skipped_trash }} 个条目 · {{ preview.skipped_links }} 条外链</dd>
      </dl>
      <ul v-if="preview.conflicts.length">
        <li v-for="(conflict, index) in preview.conflicts" :key="index">
          {{ labels[conflict.name] ?? conflict.name }} · {{ conflict.reason }}
        </li>
      </ul>
      <button type="button" class="primary" :disabled="!total || busy" @click="restore(true)">确认恢复 {{ total }} 条记录</button>
    </div>
  </section>
</template>

<style scoped>
.restore-index { margin-top: 20px; border-top: 1px solid var(--line); padding-top: 16px; }
h3 { margin: 0 0 12px; font-size: 16px; }
label { display: block; margin-bottom: 10px; }
input[type="file"] { display: block; margin-top: 5px; }
.restore-option { display: flex; align-items: center; gap: 8px; }
.restore-option input { width: auto; }
.restore-preview { margin-top: 14px; padding: 12px; background: var(--panel-2); border-radius: 8px; }
dl { display: grid; grid-template-columns: 1fr auto; gap: 5px 16px; }
dd { margin: 0; }
li { overflow-wrap: anywhere; }
</style>
