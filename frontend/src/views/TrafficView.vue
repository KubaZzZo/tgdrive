<script setup lang="ts">
import { storeToRefs } from 'pinia'
import { computed, ref, watch } from 'vue'

import { gb, useTrafficStore } from '@/stores/traffic'

const traffic = useTrafficStore()
const { data, error, saving, host, cdn } = storeToRefs(traffic)
const quotaGb = ref(100)
watch(() => data.value?.quota_bytes, (bytes) => {
  if (bytes !== undefined) quotaGb.value = bytes / 1e9
}, { immediate: true })
const chartMax = computed(() => Math.max(1, ...(host.value?.history ?? []).map(day => day.inbound + day.outbound)))
function timestamp(value?: string): string {
  return value ? new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false }) : '—'
}
function save(): void {
  void traffic.save(Math.round(quotaGb.value * 1e9))
}
</script>

<template>
  <section class="traffic-panel panel-view" aria-labelledby="traffic-title">
    <h1 id="traffic-title">CDN 与 VPS 流量</h1>
    <section v-if="cdn?.configured" class="traffic-panel" aria-label="CDN 套餐流量">
      <h3>AxisNow CDN 月流量</h3>
      <p class="muted">{{ cdn.warning_percent }}% 预警 · {{ cdn.stop_percent }}% 停止下载、分享下载和视频缓冲 · 每 60 秒同步</p>
      <p v-if="cdn.blocked" class="form-error" role="alert">CDN 流量保护中：订阅余量不足或用量数据过期，大流量传输已暂停。补充额度或同步恢复后自动解除。</p>
      <p v-else-if="cdn.warning" class="traffic-note" role="alert">部分 CDN 订阅已接近额度，请减少下载和本机缓存。</p>
      <p v-if="cdn.error" class="form-error" role="alert">{{ cdn.error }}</p>
      <div v-for="sub in cdn.subscriptions" :key="sub.id" class="traffic-panel">
        <strong>{{ sub.name }} · {{ sub.period }}{{ sub.stale ? ' · 数据过期' : '' }}</strong>
        <p>平台已用 {{ gb(sub.provider_used) }} GB · 保护估算 {{ gb(sub.used) }} GB / {{ sub.quota_bytes === null ? '不限流量' : `${gb(sub.quota_bytes)} GB` }}</p>
        <progress v-if="sub.percent !== null" class="traffic-progress" :class="{ danger: sub.warning }" :value="Math.min(100, sub.percent)" max="100" :aria-label="`${sub.name} 流量使用比例`" />
        <p v-if="sub.remaining !== null">剩余 {{ gb(sub.remaining) }} GB · 已用 {{ sub.percent }}%</p>
        <p class="muted">平台用量更新于 {{ timestamp(sub.usage_updated_at) }}</p>
      </div>
      <p class="muted">平台 MB 按 1024² 字节换算，页面 GB 按 10⁹ 字节展示。用量延迟期间会将本站传输计入每个受监控订阅，保守预留余额；估算可能高于平台用量。这里只能阻止本站回源传输，CDN 已缓存内容及其他站点的流量需在平台限制。</p>
    </section>
    <p v-else class="traffic-note">CDN 尚未接入：在服务端配置 AXISNOW_API_TOKEN 后启用套餐监控与传输保护。</p>
    <h3>VPS 每日流量</h3>
    <p class="muted">入站＋出站合计 · 每天北京时间 00:00 重置 · 1 GB = 10⁹ 字节</p>
    <p v-if="error || host?.error" class="form-error" role="alert">{{ error || host?.error }}</p>
    <p v-if="!host?.available" class="traffic-note">尚未获得 VPS 网卡数据。请在宿主机安装采集服务；接入前不会将项目估算当作 VPS 实际用量。</p>
    <template v-else>
      <div class="traffic-hero"><strong>{{ gb(host.used ?? 0) }}<small> GB</small></strong><span>/ {{ gb(data?.quota_bytes ?? 100e9) }} GB 今日额度</span></div>
      <progress class="traffic-progress" :class="{ danger: host.warning }" :value="Math.min(100, host.percent ?? 0)" max="100" aria-label="每日流量额度使用比例" />
      <p v-if="host.warning === 'exceeded'" class="form-error" role="alert">今日流量已达额度，超出 {{ gb(Math.max(0, (host.used ?? 0) - (data?.quota_bytes ?? 0))) }} GB。此功能只监测，不会自动中断传输。</p>
      <p v-else-if="host.warning === 'near'" class="traffic-note" role="alert">已使用 {{ host.percent }}%，接近每日额度。</p>
      <p v-else>今日剩余 {{ gb(host.remaining ?? 0) }} GB</p>
      <p v-if="host.incomplete" class="traffic-note" role="alert">今日记录不完整：首次启用前的流量无法回溯，重启、网卡重置或跨日采集中断也可能产生缺口。剩余额度仅供参考。</p>
      <div class="traffic-totals"><span>入站 <strong>{{ gb(host.inbound ?? 0) }} GB</strong></span><span>出站 <strong>{{ gb(host.outbound ?? 0) }} GB</strong></span></div>
      <p class="muted">网卡：{{ host.interfaces?.join('、') }} · 更新于 {{ timestamp(host.updated_at) }}（北京时间）</p>
      <h3>近 7 天</h3>
      <div v-for="day in host.history" :key="day.day" class="traffic-history">
        <span :title="day.incomplete ? '记录不完整' : day.day">{{ day.day.slice(5) }}{{ day.incomplete ? '*' : '' }}</span><div class="traffic-bar"><i :style="{ width: `${(day.inbound + day.outbound) / chartMax * 100}%` }" /></div><span>{{ gb(day.inbound + day.outbound) }} GB</span>
      </div>
      <p class="muted">开始记录：{{ timestamp(host.started_at) }}。读取公网网卡字节计数，包含 VPS 上其他程序和重传；每 5 秒采样，跨日边界可能有一个采样间隔的偏差。最终账单以服务商为准。</p>
    </template>
    <form class="traffic-settings" @submit.prevent="save">
      <label for="traffic-quota">每日额度（GB）</label><input id="traffic-quota" v-model.number="quotaGb" type="number" min="0.01" max="10000000" step="0.01" required /><button class="primary" :disabled="saving">{{ saving ? '保存中…' : '保存额度' }}</button>
    </form>
    <details v-if="data" class="traffic-detail"><summary>项目传输明细（今日有效载荷估算）</summary><table><thead><tr><th>连接</th><th>入站</th><th>出站</th></tr></thead><tbody><tr><td>浏览器 / 分享链接</td><td>{{ gb(data.sources.web.inbound) }} GB</td><td>{{ gb(data.sources.web.outbound) }} GB</td></tr><tr><td>Telegram</td><td>{{ gb(data.sources.telegram.inbound) }} GB</td><td>{{ gb(data.sources.telegram.outbound) }} GB</td></tr></tbody></table><p class="muted">明细不含协议头和所有底层重试，不用于判断 VPS 剩余额度。</p></details>
  </section>
</template>

<style scoped>
.panel-view {
  margin: 24px auto 0;
  max-width: 900px;
  padding: clamp(16px, 4vw, 32px);
  border: 1px solid var(--line);
  border-radius: 16px;
  background: white;
}
.panel-view h1 { margin: 0; }

.traffic-panel { display: grid; gap: 14px; }
.traffic-panel p { margin: 0; line-height: 1.65; }
.traffic-hero { display: flex; flex-wrap: wrap; align-items: baseline; gap: 10px; }
.traffic-hero strong { font-size: 38px; letter-spacing: -1px; }
.traffic-hero small { font-size: 16px; }
.traffic-hero span { color: var(--muted); }
.traffic-progress { width: 100%; height: 12px; accent-color: #287d63; }
.traffic-progress.danger { accent-color: #c86625; }
.traffic-note { padding: 12px; background: #fff6e6; color: #825414; border-radius: 8px; }
.traffic-totals { display: flex; flex-wrap: wrap; gap: 24px; }
.traffic-totals strong { margin-left: 8px; }
.traffic-history { display: flex; gap: 12px; align-items: center; font-size: 12px; }
.traffic-history > span:last-child { min-width: 75px; text-align: right; }
.traffic-bar { flex: 1; height: 9px; background: #edf0eb; border-radius: 4px; overflow: hidden; }
.traffic-bar i { display: block; height: 100%; background: #669b87; }
.traffic-settings { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
.traffic-settings input { width: 130px; min-height: 40px; }
.traffic-detail table { width: 100%; text-align: left; font-size: 13px; margin: 12px 0; }
</style>
