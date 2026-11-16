<script setup lang="ts">
import { ref } from 'vue'
import { api, type TelegramDiagnostics } from '@/api/client'
import { formatDate } from '@/utils/format'

const report = ref<TelegramDiagnostics>()
const busy = ref(false)
const error = ref('')
const sent = ref('')
const connections: Record<string, string> = {
  connected: '已连接', unavailable: '未连接', auth_required: '需要重新登录',
  error: '连接或权限异常', not_configured: '未配置',
}
const syncStates: Record<string, string> = {
  idle: '待同步', syncing: '同步中', disabled: '已关闭', stopped: '已停止',
  rate_limited: '等待限流恢复', error: '同步异常', auth_required: '需要重新登录',
  unavailable: '不可用', listening: '监听中', connecting: '连接中',
}
const permission = (value: boolean | null) => value === null ? '未知' : value ? '允许' : '禁止'

async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return
  busy.value = true
  error.value = ''
  try { await action() }
  catch (cause) { error.value = cause instanceof Error ? cause.message : '请求失败' }
  finally { busy.value = false }
}

function inspect(): void {
  sent.value = ''
  void run(async () => { report.value = await api.telegramDiagnostics() })
}

function send(): void {
  const id = report.value?.test_channel_id
  if (!id || !window.confirm(`向专用测试频道 ${id} 发一条诊断消息？`)) return
  void run(async () => {
    const result = await api.testTelegramMessage(id)
    sent.value = `测试消息已发送至 ${result.channel_id}`
  })
}
</script>

<template>
  <section class="diagnostics" aria-labelledby="diagnostics-title">
    <div class="diagnostic-head">
      <h2 id="diagnostics-title">Telegram 诊断</h2>
      <button class="ghost" type="button" :disabled="busy" @click="inspect">一键诊断</button>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <template v-if="report">
      <p class="muted">存储频道 {{ report.channel_id ?? '未配置' }}</p>
      <dl>
        <dt>{{ report.session.mode === 'bot' ? '主 Bot 会话' : '个人会话' }}</dt>
        <dd>{{ connections[report.session.state] }} · 发帖{{ permission(report.session.can_post) }} · 删除{{ permission(report.session.can_delete) }}</dd>
        <dd v-if="report.session.error" class="muted">{{ report.session.error }}</dd>
        <dt>备用 Bot</dt>
        <dd>{{ connections[report.bot.state] }} · 发帖{{ permission(report.bot.can_post) }} · 删除{{ permission(report.bot.can_delete) }}</dd>
        <dd v-if="report.bot.error" class="muted">{{ report.bot.error }}</dd>
        <dt>频道同步</dt>
        <dd>{{ syncStates[report.sync.state] ?? report.sync.state }}<template v-if="report.sync.bot_state"> · Bot {{ syncStates[report.sync.bot_state] ?? report.sync.bot_state }}</template></dd>
        <dd v-if="report.sync.last_success">最近成功 {{ formatDate(report.sync.last_success) }}</dd>
        <dd>最近错误 {{ report.sync.error ?? '无' }}</dd>
        <dd v-if="report.sync.bot_error">备用同步错误 {{ report.sync.bot_error }}</dd>
      </dl>
      <button v-if="report.test_channel_id" class="ghost" type="button" :disabled="busy" @click="send">
        向专用测试频道发送消息
      </button>
      <p v-if="sent" role="status">{{ sent }}</p>
    </template>
  </section>
</template>

<style scoped>
.diagnostics { margin-top: 24px; border-top: 1px solid var(--line); padding-top: 16px; }
.diagnostic-head { display: flex; justify-content: space-between; align-items: center; gap: 12px; }
h2 { margin: 0; font-size: 18px; }
dt { margin-top: 10px; font-weight: 650; }
dd { margin: 2px 0; overflow-wrap: anywhere; }
</style>
