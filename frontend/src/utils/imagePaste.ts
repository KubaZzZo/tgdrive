type ImagePasteEvent = Pick<Event, 'target' | 'defaultPrevented'> & {
  clipboardData: { files: ArrayLike<File> } | null
}

export function pastedImageFiles(event: ImagePasteEvent, modalOpen: boolean): File[] {
  // Pasting inside another workflow must never publish clipboard content.
  if (modalOpen || event.defaultPrevented) return []
  const target = event.target as Element | null
  if (typeof target?.closest === 'function' && target.closest('input,textarea,[contenteditable="true"]')) return []
  return Array.from(event.clipboardData?.files ?? [])
}
