// Share the budget across every visible video card, including list/grid swaps.
let active = 0
let foreground = 0
const pending: Array<() => void> = []

function drain(): void {
  while (!foreground && active < 2 && pending.length) pending.shift()?.()
}

export function holdCoverQueue(): () => void {
  foreground++
  let released = false
  return () => {
    if (released) return
    released = true
    foreground--
    drain()
  }
}

export function acquireCoverSlot(signal: AbortSignal): Promise<() => void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      const index = pending.indexOf(start)
      if (index >= 0) pending.splice(index, 1)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const start = () => {
      signal.removeEventListener('abort', abort)
      active++
      let released = false
      resolve(() => {
        if (released) return
        released = true
        active--
        drain()
      })
    }
    if (signal.aborted) { abort(); return }
    if (!foreground && active < 2) start()
    else { pending.push(start); signal.addEventListener('abort', abort, { once: true }) }
  })
}
