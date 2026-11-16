import { createMemoryHistory, createRouter, createWebHistory, parseQuery } from 'vue-router'
import type { RouteLocation, RouteLocationRaw } from 'vue-router'

import { useAuthStore } from '@/stores/auth'

const legacyPaths: Record<string, string> = { drive: '/drive', people: '/people', images: '/images' }

function landing(to: RouteLocation): RouteLocationRaw {
  const hash = to.fullPath.slice(to.fullPath.indexOf('#') + 1)
  const [section] = hash.split('?', 1)
  if (!to.hash || !Object.hasOwn(legacyPaths, section)) return { path: '/drive', hash: '' }
  // Use the raw fragment so encoded query delimiters stay inside their values.
  return { path: legacyPaths[section], query: parseQuery(hash.slice(section.length + 1)), hash: '' }
}

export const router = createRouter({
  history: typeof window === 'undefined' ? createMemoryHistory() : createWebHistory(),
  strict: true,
  sensitive: true,
  routes: [
    { path: '/', redirect: landing },
    { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue') },
    { path: '/drive', name: 'drive', component: () => import('@/views/DriveView.vue') },
    { path: '/google-drive', name: 'google-drive', component: () => import('@/views/GoogleDriveView.vue') },
    { path: '/people', name: 'people', component: () => import('@/views/DriveView.vue') },
    { path: '/images', name: 'images', component: () => import('@/views/ImageHostView.vue') },
    { path: '/traffic', name: 'traffic', component: () => import('@/views/TrafficView.vue') },
    { path: '/video-import', name: 'video-import', component: () => import('@/views/VideoImportView.vue') },
    { path: '/sync', name: 'sync', component: () => import('@/views/ChannelSyncView.vue') },
    { path: '/clients', name: 'clients', component: () => import('@/views/ClientsView.vue') },
    { path: '/:pathMatch(.*)*', redirect: '/drive' },
  ],
})

export function loginDestination(redirect: unknown): string {
  if (typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//')) {
    const resolved = router.resolve(redirect)
    if (['drive', 'people', 'images', 'traffic', 'sync', 'video-import', 'clients', 'google-drive'].includes(String(resolved.name))) {
      return resolved.fullPath
    }
  }
  return '/drive'
}

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.ready) await auth.bootstrap()
  if (to.name === 'people' && !auth.features.people ||
    to.name === 'images' && !auth.features.images ||
    to.name === 'traffic' && !auth.features.traffic ||
    to.name === 'google-drive' && auth.authenticated && !auth.features.google_drive) {
    return { name: 'drive', replace: true }
  }
  if (to.name === 'login') {
    if (auth.authenticated) return loginDestination(to.query.redirect)
  } else if (!auth.authenticated) {
    return { name: 'login', query: { redirect: to.fullPath }, replace: true }
  }
})
