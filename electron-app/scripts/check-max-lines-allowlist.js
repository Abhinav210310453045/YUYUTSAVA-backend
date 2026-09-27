// The max-lines allowlist only shrinks. ESLint turns the file-size limit off
// for the files listed in max-lines-allowlist.json; this fails when a listed
// file no longer needs the exemption (it was split, or deleted), so the entry
// is removed in the same change and cannot be reused by something new.
const fs = require('fs')
const path = require('path')

const root = path.join(__dirname, '..')
const { max, files } = JSON.parse(
  fs.readFileSync(path.join(root, 'max-lines-allowlist.json'), 'utf8'),
)

// Counted as ESLint's max-lines counts them: a final newline adds no line.
const lineCount = (text) => {
  const lines = text.split(/\r\n|\r|\n/)
  if (lines.length > 1 && lines[lines.length - 1] === '') lines.pop()
  return lines.length
}

const stale = []
for (const file of files) {
  const full = path.join(root, file)
  if (!fs.existsSync(full)) stale.push(`${file}: no longer exists`)
  else if (lineCount(fs.readFileSync(full, 'utf8')) <= max) {
    stale.push(`${file}: now within ${max} lines`)
  }
}

if (stale.length) {
  console.error('Remove these from max-lines-allowlist.json:\n  ' + stale.join('\n  '))
  process.exit(1)
}
