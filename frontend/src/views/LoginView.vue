<script setup lang="ts">
import { ref } from 'vue'

import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const password = ref('')

async function submit(): Promise<void> {
  if (!password.value || auth.loading) return
  try {
    await auth.login(password.value)
    password.value = ''
  } catch {
    // The auth store exposes a user-facing error.
  }
}
</script>

<template>
  <div class="login-screen">
    <form class="login-card" @submit.prevent="submit">
      <div class="brand-mark">TG</div>
      <h1>{{ auth.meta?.app_name ?? 'TG Drive' }}</h1>
      <p class="muted">Telegram 驱动的私人网盘</p>
      <label class="sr-only" for="password">访问密码</label>
      <input
        id="password"
        v-model="password"
        type="password"
        placeholder="访问密码"
        autocomplete="current-password"
        autofocus
        required
      />
      <button class="primary login-submit" type="submit" :disabled="auth.loading">
        {{ auth.loading ? '正在验证…' : '进入' }}
      </button>
      <p v-if="auth.error" class="form-error">{{ auth.error }}</p>
    </form>
  </div>
</template>

<style scoped>
.login-screen {
  min-height: 100dvh;
  display: grid;
  place-items: center;
  padding: 24px;
}
.login-card {
  width: min(420px, 100%);
  padding: 40px;
  border: 1px solid var(--line);
  border-radius: 20px;
  background: white;
  box-shadow: var(--shadow);
  text-align: center;
}
.login-card h1 {
  margin: 20px 0 8px;
}
.login-card > p {
  margin: 0 0 25px;
  font-size: 13px;
}
.login-submit {
  width: 100%;
  margin-top: 14px;
}
</style>
