<script setup lang="ts">
withDefaults(defineProps<{ name: string; size?: number }>(), { size: 20 })
const paths: Record<string, string[]> = {
  folder: ['M3 7V5a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v12a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V7Z'],
  image: [
    'M4 3h16a1 1 0 0 1 1 1v16a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1V4a1 1 0 0 1 1-1Z',
    'm3 17 5-5 4 4 4-6 5 8',
    'M8 8h.01',
  ],
  video: ['M4 4h16v16H4Z', 'm10 8 6 4-6 4Z'],
  people: ['M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2', 'M9 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8Z', 'M22 21v-2a4 4 0 0 0-3-4', 'M16 3a4 4 0 0 1 0 8'],
  audio: [
    'M9 18V5l11-2v13',
    'M9 14H5a3 3 0 1 0 0 6h1a3 3 0 0 0 3-3',
    'M20 12h-4a3 3 0 1 0 0 6h1a3 3 0 0 0 3-3',
  ],
  document: ['M5 3h9l5 5v13H5Z', 'M14 3v6h5', 'M8 13h8M8 17h6'],
  archive: ['M4 7h16v14H4Z', 'M3 3h18v4H3Z', 'M10 11h4'],
  code: ['m8 7-5 5 5 5', 'm16 7 5 5-5 5', 'm14 4-4 16'],
  application: ['M4 4h16v16H4Z', 'M4 9h16', 'M7 6.5h.01M10 6.5h.01'],
  other: ['M4 4h6v6H4ZM14 4h6v6h-6ZM4 14h6v6H4ZM14 14h6v6h-6Z'],
  all: ['M4 3h16v18H4Z', 'M8 8h8M8 12h8M8 16h4'],
  recent: ['M12 3a9 9 0 1 0 9 9 9 9 0 0 0-9-9Z', 'M12 7v5l3 2'],
  upload: ['M12 16V3m-5 5 5-5 5 5', 'M4 15v6h16v-6'],
  download: ['M12 3v13m-5-5 5 5 5-5', 'M4 16v5h16v-5'],
  play: ['m8 5 12 7-12 7Z'],
  pause: ['M9 5v14m6-14v14'],
  rewind: ['M4 10a8 8 0 1 1 2 8M4 4v6h6'],
  forward: ['M20 10a8 8 0 1 0-2 8m2-14v6h-6'],
  'volume-on': ['M3 9h4l5-4v14l-5-4H3Z', 'M16 9a4 4 0 0 1 0 6m3-9a8 8 0 0 1 0 12'],
  'volume-off': ['M3 9h4l5-4v14l-5-4H3Z', 'm16 9 6 6m0-6-6 6'],
  pip: ['M3 5h18v14H3Z', 'M13 11h6v6h-6Z'],
  fullscreen: ['M9 4H4v5m11-5h5v5m0 6v5h-5M4 15v5h5'],
  'fullscreen-exit': ['M4 9h5V4m6 0v5h5m0 6h-5v5m-6 0v-5H4'],
  settings: ['M4 6h16M4 12h16M4 18h16', 'M8 4v4m8 2v4m-6 2v4'],
  search: ['M10.5 3a7.5 7.5 0 1 0 0 15 7.5 7.5 0 0 0 0-15Z', 'm16 16 5 5'],
  grid: ['M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7Z'],
  list: ['M8 5h13M8 12h13M8 19h13', 'M3 5h.01M3 12h.01M3 19h.01'],
  more: ['M5 12h.01M12 12h.01M19 12h.01'],
  chevron: ['m9 5 7 7-7 7'],
  cloud: ['M7 18a5 5 0 1 1 0-10h1a6 6 0 0 1 11 2 4 4 0 0 1 0 8Z'],
  refresh: ['M20 7a9 9 0 1 0 1 9', 'M20 3v5h-5'],
  close: ['m6 6 12 12M6 18 18 6'],
  plus: ['M12 4v16M4 12h16'],
  link: ['M10 13a4 4 0 0 0 6 0l4-4a4 4 0 0 0-6-6l-2 2', 'M14 11a4 4 0 0 0-6 0l-4 4a4 4 0 0 0 6 6l2-2'],
  trash: ['M4 7h16M6 7l1 14h10l1-14M9 7V4h6v3M10 10v7m4-7v7'],
}
</script>
<template>
  <svg
    :width="size"
    :height="size"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    stroke-width="1.7"
    stroke-linecap="round"
    stroke-linejoin="round"
    aria-hidden="true"
    class="app-icon"
  >
    <path v-for="(path, index) in paths[name] ?? paths.document" :key="index" :d="path" />
  </svg>
</template>
