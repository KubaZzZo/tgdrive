import { registerHooks } from 'node:module'
import { existsSync } from 'node:fs'
import { fileURLToPath, pathToFileURL } from 'node:url'

registerHooks({
  resolve(specifier, context, nextResolve) {
    let candidate
    if (specifier.startsWith('@/')) candidate = new URL('../src/' + specifier.slice(2), import.meta.url)
    else if (specifier.startsWith('.') && context.parentURL) candidate = new URL(specifier, context.parentURL)
    if (candidate && !candidate.pathname.endsWith('.ts')) {
      const ts = fileURLToPath(candidate) + '.ts'
      if (existsSync(ts)) return nextResolve(pathToFileURL(ts).href, context)
    }
    return nextResolve(specifier, context)
  },
})
