import { createPinia } from 'pinia'
import { createApp } from 'vue'

import App from './App.vue'
import { router } from './router'
import { initTelegramMiniApp } from './utils/telegramMiniApp'
import './stores/theme'
import './styles/main.css'

async function start(): Promise<void> {
  const webApp = await initTelegramMiniApp(router.options.history)
  createApp(App).use(createPinia()).use(router).mount('#app')
  if (webApp) {
    await router.isReady()
    webApp.ready()
  }
}

void start()
