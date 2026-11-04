import type { CategoryStats } from '@/api/types'

export const categories: CategoryStats[] = [
  { id: 'image', label: '图片', count: 0, bytes: 0 },
  { id: 'video', label: '视频', count: 0, bytes: 0 },
  { id: 'audio', label: '音频', count: 0, bytes: 0 },
  { id: 'document', label: '文档', count: 0, bytes: 0 },
  { id: 'archive', label: '压缩包', count: 0, bytes: 0 },
  { id: 'code', label: '代码', count: 0, bytes: 0 },
  { id: 'application', label: '安装包', count: 0, bytes: 0 },
  { id: 'other', label: '其他', count: 0, bytes: 0 },
]
export function categoryLabel(id: string | null): string {
  return categories.find((category) => category.id === id)?.label ?? '文件'
}
