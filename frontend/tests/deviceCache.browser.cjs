async page => {
  const bytes = Buffer.alloc(6 * 1024 * 1024 + 123)
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251
  let requests = 0
  await page.route('**/__cache_fixture', async route => {
    requests++
    const range = /bytes=(\d+)-(\d+)/.exec(route.request().headers()['range'])
    const start = Number(range[1]), end = Number(range[2])
    await route.fulfill({status:206, headers:{'Content-Range':`bytes ${start}-${end}/${bytes.length}`}, body:bytes.subarray(start,end+1)})
  })
  const result = await page.evaluate(async size => {
    const cache = await import('/src/utils/deviceCache.ts')
    if (!cache.deviceCacheAvailable()) throw new Error('OPFS unavailable')
    await cache.clearVideoCache()
    let progress = 0
    const file = await cache.cacheVideo('/__cache_fixture','test-video',size,new AbortController().signal, n=>progress=n)
    const data = new Uint8Array(await file.arrayBuffer())
    if (data.length !== size || data.some((n,i)=>n!==i%251) || progress!==size) throw new Error('Cached bytes mismatch')
    if (!(await cache.cachedVideo('test-video',size))) throw new Error('Completed cache not reused')
    await navigator.locks.request('tgdrive-video-cache-v1', async () => {
      if (await cache.cachedVideo('test-video',size)) throw new Error('Cache read raced an exclusive writer')
    })
    if (await cache.cachedVideo('another-channel',size)) throw new Error('Cache crossed workspace')
    const controller = new AbortController()
    let aborted = false
    try { await cache.cacheVideo('/__cache_fixture','cancelled',size,controller.signal,()=>controller.abort()) } catch { aborted=true }
    if (!aborted || await cache.cachedVideo('cancelled',size)) throw new Error('Cancelled cache marked complete')
    await cache.clearVideoCache()
    return {bytes:size,correct:true,reused:true,workspaceIsolated:true,writerIsolated:true,cancellationClean:true}
  }, bytes.length)
  return {...result,requests}
}
