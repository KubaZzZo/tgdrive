export function groupFolderFiles(files: Iterable<File>): { path: string[]; files: File[] }[] {
  const groups = new Map<string, { path: string[]; files: File[] }>()
  for (const file of files) {
    const path = file.webkitRelativePath.split('/')
    if (path.pop() !== file.name || !path.length || path.length > 64
      || path.some((part) => !part.trim() || Array.from(part).length > 255
        || ['.', '..'].includes(part.trim()) || part.includes('\\'))) {
      throw new Error('所选文件夹包含无效的相对路径')
    }
    const key = JSON.stringify(path)
    let group = groups.get(key)
    if (!group) {
      group = { path, files: [] }
      groups.set(key, group)
    }
    group.files.push(file)
  }
  return [...groups.values()]
}
