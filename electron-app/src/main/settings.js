const fs = require('fs')
const os = require('os')
const path = require('path')

function getEnvFilePath() {
  const home = process.env.YUYUTSAVA_HOME || path.join(os.homedir(), '.yuyutsava')
  return path.join(home, '.env')
}

function ensureEnvFileExists(filePath) {
  const dir = path.dirname(filePath)
  if (!fs.existsSync(dir)) fs.mkdirSync(dir, { recursive: true })
  if (!fs.existsSync(filePath)) fs.writeFileSync(filePath, '# YUYUTSAVA configuration\n', 'utf8')
}

function readSettings() {
  const filePath = getEnvFilePath()
  ensureEnvFileExists(filePath)
  const lines = fs.readFileSync(filePath, 'utf8').split('\n')
  const result = {}
  for (const line of lines) {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) continue
    const eq = trimmed.indexOf('=')
    if (eq === -1) continue
    const key = trimmed.slice(0, eq).trim()
    const val = trimmed.slice(eq + 1).trim().replace(/^["']|["']$/g, '')
    result[key] = val
  }
  return result
}

function writeSettings(updates) {
  const filePath = getEnvFilePath()
  ensureEnvFileExists(filePath)

  const existing = fs.readFileSync(filePath, 'utf8').split('\n')
  const handled = new Set()
  const newLines = existing.map(line => {
    const trimmed = line.trim()
    if (!trimmed || trimmed.startsWith('#')) return line
    const eq = trimmed.indexOf('=')
    if (eq === -1) return line
    const key = trimmed.slice(0, eq).trim()
    if (key in updates) {
      handled.add(key)
      return `${key}=${updates[key]}`
    }
    return line
  })

  // Append any new keys not already present
  for (const [key, val] of Object.entries(updates)) {
    if (!handled.has(key)) newLines.push(`${key}=${val}`)
  }

  const tmp = filePath + '.tmp'
  fs.writeFileSync(tmp, newLines.join('\n'), 'utf8')
  fs.renameSync(tmp, filePath)
}

module.exports = { readSettings, writeSettings, getEnvFilePath }
