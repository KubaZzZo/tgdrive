import type { RouterHistory } from 'vue-router'
import type { DriveItem } from '@/api/types'

const launchKey = 'tgdrive_telegram_mini_app'
const sdkUrl = 'https://telegram.org/js/telegram-web-app.js?65'

interface TelegramWebApp {
  initData: string
  platform: string
  ready(): void
  openTelegramLink(url: string): void
}

declare global {
  interface Window {
    Telegram?: { WebApp?: TelegramWebApp }
  }
}

let inMiniApp = false

function loadSdk(): Promise<void> {
  return new Promise((resolve) => {
    const script = document.createElement('script')
    const timeout = window.setTimeout(() => {
      script.remove()
      resolve()
    }, 2500)
    const finish = () => {
      window.clearTimeout(timeout)
      resolve()
    }
    script.onload = finish
    script.onerror = finish
    script.src = sdkUrl
    document.head.append(script)
  })
}

export async function initTelegramMiniApp(history: RouterHistory): Promise<TelegramWebApp | undefined> {
  const url = new URL(window.location.href)
  const fragment = url.hash.slice(1)
  const queryStart = fragment.indexOf('?') + 1
  const params = new URLSearchParams(fragment.slice(queryStart))
  const launched = params.has('tgWebAppData') || (
    params.has('tgWebAppVersion') && params.has('tgWebAppPlatform')
  )
  const webApp = window.Telegram?.WebApp
  inMiniApp = launched || Boolean(webApp?.initData || (webApp?.platform && webApp.platform !== 'unknown'))
  try {
    if (inMiniApp) window.sessionStorage.setItem(launchKey, '1')
    else inMiniApp = window.sessionStorage.getItem(launchKey) === '1'
  } catch { /* Session storage can be unavailable in a WebView. */ }
  if (!inMiniApp) return

  // The SDK reads its launch parameters from the fragment during initialization.
  if (!webApp) await loadSdk()
  if (launched) {
    // Keep signed Telegram launch data out of Vue Router's login redirect query.
    for (const key of Array.from(params.keys())) {
      if (key.startsWith('tgWebApp')) params.delete(key)
    }
    url.hash = `${fragment.slice(0, queryStart)}${params}`.replace(/\?$/, '')
    history.replace(`${url.pathname}${url.search}${url.hash}`)
  }
  return window.Telegram?.WebApp
}

export function telegramVideoUrl(item: DriveItem): string | undefined {
  return inMiniApp ? item.telegram_video_url : undefined
}

export function openTelegramVideo(url: string): void {
  try {
    const webApp = window.Telegram?.WebApp
    if (webApp) {
      webApp.openTelegramLink(url)
      return
    }
  } catch { /* Try the same message link when the client bridge fails. */ }
  window.location.assign(url)
}
