import assert from 'node:assert/strict'
import test from 'node:test'

import { splitVideoImportLinks } from '../src/utils/video-import-links.ts'

const sample = 'ed2k://|file|SNOS-141.mp4|6800928646|A549B3E090C89D60D45FECD957EBCA27|/'

test('splits adjacent eD2k links for duplicate detection by the preview API', () => {
  assert.deepEqual(splitVideoImportLinks(sample + sample), [sample, sample])
})

test('recognizes an encoded or escaped eD2k boundary and removes a trailing HTML space', () => {
  const encoded = sample.replaceAll('|', '%7C')
  const escaped = sample.replaceAll('|', '\\|') + '\\'
  assert.deepEqual(
    splitVideoImportLinks(`  ${encoded}  ${escaped}${sample} &#x20;  \r\n`),
    [encoded, escaped, sample],
  )
})

test('keeps non-eD2k lines and eD2k extensions intact for server validation', () => {
  const direct = `https://example.com/video.mp4?caption=${sample + sample}`
  const magnet = 'magnet:?xt=urn:btih:0123456789abcdef0123456789abcdef01234567'
  const extended = sample + '|sources,example.com:1234|/'
  assert.deepEqual(
    splitVideoImportLinks(`${direct}\r\n${magnet}\n\n${extended}`),
    [direct, magnet, extended],
  )
})
