import { ref } from 'vue'

export type Theme = 'light' | 'dark' | 'system'

const query = window.matchMedia('(prefers-color-scheme: dark)')
const saved = document.cookie.match(/(?:^|;\s*)tgdrive_theme=(light|dark|system)(?:;|$)/)?.[1]
export const theme = ref<Theme>(saved as Theme | undefined ?? 'system')

function applyTheme(): void {
  document.documentElement.dataset.theme = theme.value === 'system'
    ? (query.matches ? 'dark' : 'light')
    : theme.value
}

export function setTheme(value: Theme): void {
  theme.value = value
  const secure = window.location.protocol === 'https:' ? '; Secure' : ''
  document.cookie = `tgdrive_theme=${value}; Path=/; Max-Age=31536000; SameSite=Lax${secure}`
  applyTheme()
}

query.addEventListener('change', applyTheme)
applyTheme()
