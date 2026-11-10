// Share the budget across every visible video card, including list/grid swaps.
let active = 0
let foreground = 0
const pending: Array<() => void> = []
const running = new Set<() => void>()

function drain(): void {
  while (!foreground && active < 2 && pending.length) pending.shift()?.()
}

export function holdCoverQueue(): () => void {
  foreground++
  for (const suspend of [...running]) suspend()
  let released = false
  return () => {
    if (released) return
    released = true
    foreground--
    drain()
  }
}

export function acquireCoverSlot(signal: AbortSignal, suspend?: () => void): Promise<() => void> {
  return new Promise((resolve, reject) => {
    const abort = () => {
      const index = pending.indexOf(start)
      if (index >= 0) pending.splice(index, 1)
      reject(new DOMException('Aborted', 'AbortError'))
    }
    const start = () => {
      signal.removeEventListener('abort', abort)
      active++
      if (suspend) running.add(suspend)
      let released = false
      resolve(() => {
        if (released) return
        released = true
        active--
        if (suspend) running.delete(suspend)
        drain()
      })
    }
    if (signal.aborted) { abort(); return }
    if (!foreground && active < 2) start()
    else { pending.push(start); signal.addEventListener('abort', abort, { once: true }) }
  })
}
