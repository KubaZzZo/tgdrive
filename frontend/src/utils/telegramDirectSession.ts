import { untilAborted } from './telegramMedia'

const sessionKey = 'tgdrive_telegram_direct_session'

export interface TelegramDirectSession {
  apiId: number
  session: string
}

export function readTelegramDirectSession(): TelegramDirectSession | null {
  try {
    const data = JSON.parse(sessionStorage.getItem(sessionKey) ?? 'null')
    if (data && Number.isSafeInteger(data.apiId) && data.apiId > 0 && typeof data.session === 'string' && data.session) {
      return { apiId: data.apiId, session: data.session }
    }
  } catch { /* A WebView may disable session storage. */ }
  return null
}

export function saveTelegramDirectSession(data: TelegramDirectSession): void {
  try { sessionStorage.setItem(sessionKey, JSON.stringify(data)) }
  catch { /* Playback can still use the current connection. */ }
}

export function clearTelegramDirectSession(): void {
  try { sessionStorage.removeItem(sessionKey) }
  catch { /* A WebView may disable session storage. */ }
}

export async function revokeTelegramDirectSession(): Promise<void> {
  const saved = readTelegramDirectSession()
  if (!saved) return
  const signal = AbortSignal.timeout(8000)
  const { revokeTelegramDirectAuthorization } = await untilAborted(signal, () => import('./telegramDirectClient'))
  await revokeTelegramDirectAuthorization(saved, signal)
  if (readTelegramDirectSession()?.session === saved.session) clearTelegramDirectSession()
}
