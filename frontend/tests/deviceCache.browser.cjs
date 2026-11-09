async page => {
  const bytes = Buffer.alloc(6 * 1024 * 1024 + 123)
  for (let i = 0; i < bytes.length; i++) bytes[i] = i % 251
  let requests = 0
  let changeVersion = false
  await page.route('**/__cache_fixture', async route => {
    requests++
    const range = /bytes=(\d+)-(\d+)/.exec(route.request().headers()['range'])
    const start = Number(range[1]), end = Number(range[2])
    if (start > 0 && route.request().headers()['if-range'] !== '"fixture-v1"') throw new Error('Range was not version pinned')
    await route.fulfill({
      status:206,
      headers:{'Content-Range':`bytes ${start}-${end}/${bytes.length}`, ETag: changeVersion && start > 0 ? '"fixture-v2"' : '"fixture-v1"'},
      body:bytes.subarray(start,end+1),
    })
  })
  const result = await page.evaluate(async size => {
    const cache = await import('/src/utils/deviceCache.ts')
    if (!cache.deviceCacheAvailable()) throw new Error('OPFS unavailable')
    await cache.clearVideoCache()
    let progress = 0
    const file = await cache.cacheVideo('/__cache_fixture','test-video',size,new AbortController().signal, n=>progress=n)
    const data = new Uint8Array(await file.arrayBuffer())
    if (data.length !== size || data.some((n,i)=>n!==i%251) || progress!==size) throw new Error('Cached bytes mismatch')
    const hit = await cache.cachedVideo('test-video',size)
    if (!hit || hit.etag !== '"fixture-v1"') throw new Error('Completed cache not reused')
    let pinned = false
    try { await cache.clearVideoCache() } catch { pinned = true }
    if (!pinned) throw new Error('Active file was removed')
    hit.release()
    await navigator.locks.request('tgdrive-video-cache-v1', async () => {
      if (await cache.cachedVideo('test-video',size)) throw new Error('Cache read raced an exclusive writer')
    })
    if (await cache.cachedVideo('another-channel',size)) throw new Error('Cache crossed workspace')
    const controller = new AbortController()
    let aborted = false
    try { await cache.cacheVideo('/__cache_fixture','cancelled',size,controller.signal,()=>controller.abort()) } catch { aborted=true }
    if (!aborted || await cache.cachedVideo('cancelled',size)) throw new Error('Cancelled cache marked complete')
    await cache.cacheVideoPrefix('prefix-video', new Uint8Array(65536).fill(7), '"fixture-v1"')
    const prefix = await cache.cachedVideo('prefix-video', size, true)
    if (prefix?.file.size !== 65536 || new Uint8Array(await prefix.file.arrayBuffer())[0] !== 7)
      throw new Error('Cached startup prefix unavailable')
    prefix.release()
    return {bytes:size,correct:true,reused:true,workspaceIsolated:true,writerIsolated:true,cancellationClean:true,pinned:true}
  }, bytes.length)
  changeVersion = true
  await page.evaluate(async size => {
    const cache = await import('/src/utils/deviceCache.ts')
    let rejected = false
    try {
      await cache.cacheVideo('/__cache_fixture', 'changed', size, new AbortController().signal, () => {})
    } catch (error) { rejected = String(error).includes('视频版本已变化') }
    if (!rejected || await cache.cachedVideo('changed', size)) throw new Error('Mixed-version file was committed')
    const old = await cache.cachedVideo('test-video', size)
    if (!old) throw new Error('Failed cache write removed a valid entry')
    old.release()
    await cache.clearVideoCache()
  }, bytes.length)
  return {...result,requests,etagMismatchRejected:true}
}
