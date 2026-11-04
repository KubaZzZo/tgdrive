import { defineStore } from 'pinia'
import { ref } from 'vue'

export type ToastTone = 'info' | 'success' | 'error'

interface ToastItem {
  id: number
  message: string
  tone: ToastTone
}

let sequence = 0

export const useToastStore = defineStore('toast', () => {
  const items = ref<ToastItem[]>([])

  function push(message: string, tone: ToastTone = 'info', ttl = 3200): void {
    if (!message) return
    const id = ++sequence
    items.value.push({ id, message, tone })
    window.setTimeout(() => dismiss(id), ttl)
  }

  function dismiss(id: number): void {
    items.value = items.value.filter((item) => item.id !== id)
  }

  const success = (message: string) => push(message, 'success')
  const error = (message: string) => push(message, 'error', 4500)

  return { items, push, dismiss, success, error }
})
