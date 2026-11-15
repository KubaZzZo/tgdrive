const nextEd2kLink = /((?:\||%7c)\/\\?)\s*(?=ed2k:\/\/)/gi
const trailingHtmlSpace = /(?:&nbsp;|&#32;|&#x20;)$/i

export function splitVideoImportLinks(text: string): string[] {
  return text.split(/\r\n?|\n/).flatMap(value => {
    const line = value.trim()
    if (!line) return []
    if (!/^ed2k:\/\//i.test(line)) return [line]
    return line.replace(nextEd2kLink, '$1\n').split('\n').map(
      link => link.replace(trailingHtmlSpace, '').trim(),
    )
  })
}
