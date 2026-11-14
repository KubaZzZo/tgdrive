import assert from 'node:assert/strict'
import test from 'node:test'

import { readDriveLocation, writeDriveLocation } from '../src/utils/driveLocation.ts'
import { groupFolderFiles } from '../src/utils/folderUpload.ts'

test('drive location round-trips a directory, search filters, sorting, layout and page', () => {
  const hash = '/drive?folder=folder-id&view=video&q=%E6%97%85%E8%A1%8C&sort=size&page=3&layout=grid&scope=all&min_size=0&created_from=2024-02-29&extension=.mp4'
  const location = readDriveLocation(hash)
  assert.equal(location.folder, 'folder-id')
  assert.equal(location.query, '旅行')
  assert.equal(location.page, 3)
  assert.equal(location.filters.min_size, 0)
  assert.deepEqual(readDriveLocation(writeDriveLocation(location)), location)
})

test('drive location ignores malformed and contradictory filter parameters', () => {
  const location = readDriveLocation('/drive?folder=../abc&page=-1&view=bad&scope=recent&min_size=10&max_size=9&created_from=2025-02-29&extension=%25')
  assert.equal(location.folder, null)
  assert.equal(location.view, 'all')
  assert.equal(location.scope, '')
  assert.equal(location.page, 1)
  assert.equal(location.filters.created_from, undefined)
  assert.equal(location.filters.max_size, undefined)
  assert.equal(location.filters.extension, undefined)
})

function file(name: string, path: string): File {
  const item = new File(['content'], name)
  Object.defineProperty(item, 'webkitRelativePath', { value: path })
  return item
}

test('folder selection groups files by full relative directory and keeps repeated names separate', () => {
  const root = file('one.txt', 'trip/one.txt')
  const first = file('one.txt', 'trip/day 1/one.txt')
  const second = file('two.txt', 'trip/day 1/two.txt')
  assert.deepEqual(groupFolderFiles([root, first, second]), [
    { path: ['trip'], files: [root] },
    { path: ['trip', 'day 1'], files: [first, second] },
  ])
  assert.throws(() => groupFolderFiles([file('one.txt', 'trip/../one.txt')]), /无效/)
  assert.throws(() => groupFolderFiles([file('one.txt', 'one.txt')]), /无效/)
})
