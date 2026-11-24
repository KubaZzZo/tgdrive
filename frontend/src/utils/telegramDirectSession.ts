import { untilAborted } from './telegramMedia'

const sessionKey = 'tgdrive_telegram_direct_session'
let inMemory: TelegramDirectSession | null = null

export interface TelegramDirectSession {
  apiId: number
  session: string
}

function storedSession(kind: 'localStorage' | 'sessionStorage'): TelegramDirectSession | null | undefined {
  try {
    const raw = window[kind].getItem(sessionKey)
    if (raw === null) return undefined
    const data = JSON.parse(raw)
    if (data && Number.isSafeInteger(data.apiId) && data.apiId > 0 && typeof data.session === 'string' && data.session) {
      return { apiId: data.apiId, session: data.session }
    }
    return null
  } catch { return undefined }
}

export function readTelegramDirectSession(): TelegramDirectSession | null {
  const device = storedSession('localStorage')
  // A revoked device record also retires legacy sessions in other open tabs.
  return device ?? inMemory ?? (device === undefined ? storedSession('sessionStorage') ?? null : null)
}

export function hasSavedTelegramDirectSession(): boolean {
  return Boolean(storedSession('localStorage'))
}

export async function withTelegramDirectSessionLock(action: () => Promise<void>, signal?: AbortSignal): Promise<void> {
  await (navigator.locks ? navigator.locks.request(sessionKey, { signal }, action) : action())
}

function writeDeviceSession(data: TelegramDirectSession | null): boolean {
  try {
    localStorage.setItem(sessionKey, JSON.stringify(data))
    return true
  } catch { return false }
}

function removeLegacySession(): void {
  try { sessionStorage.removeItem(sessionKey) }
  catch { /* A WebView may disable storage. */ }
}

export function saveTelegramDirectSession(data: TelegramDirectSession): void {
  // WebViews without cross-tab locking can still use the session in this page.
  const saved = Boolean(navigator.locks) && writeDeviceSession(data)
  inMemory = saved ? null : data
}

export function clearTelegramDirectSession(expected?: TelegramDirectSession): void {
  if (!expected || inMemory?.session === expected.session) inMemory = null
  if (!expected || storedSession('localStorage')?.session === expected.session) writeDeviceSession(null)
  if (!expected || storedSession('sessionStorage')?.session === expected.session) removeLegacySession()
}

export async function revokeTelegramDirectSession(): Promise<void> {
  const sessions = new Map<string, TelegramDirectSession>()
  for (const session of [storedSession('sessionStorage'), inMemory, storedSession('localStorage')]) {
    if (session) sessions.set(session.session, session)
  }
  if (sessions.size) {
    const signal = AbortSignal.timeout(8000)
    const { revokeTelegramDirectAuthorization } = await untilAborted(signal, () => import('./telegramDirectClient'))
    let failed = false
    for (const session of sessions.values()) {
      try {
        await revokeTelegramDirectAuthorization(session, signal)
        clearTelegramDirectSession(session)
      } catch { failed = true }
    }
    if (failed) throw new Error('Telegram 直连授权撤销未完成')
  }
  clearTelegramDirectSession()
}
