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
    const val = trimmed
      .slice(eq + 1)
      .trim()
      .replace(/^["']|["']$/g, '')
    result[key] = val
  }
  return result
}

// Model settings live in models.toml now, changed through the daemon's /models
// API (Settings → Models). A model key written here would be read only by a
// daemon without models.toml, and silently ignored by every other — so they are
// refused rather than written.
const MODEL_KEY =
  /^(?:TIER_[A-Z]+_)?(?:LLM_PROVIDER|GROQ_API_KEY|GROQ_MODEL|OPENROUTER_API_KEY|OPENROUTER_MODEL|ANTHROPIC_API_KEY|ANTHROPIC_MODEL|OLLAMA_MODEL|OLLAMA_HOST)$/

function isModelKey(key) {
  return MODEL_KEY.test(key)
}

function writeSettings(requested) {
  const refused = Object.keys(requested).filter(isModelKey)
  if (refused.length) {
    console.warn(
      `[settings] not writing ${refused.join(', ')} — models are set up in Settings → Models`,
    )
  }
  const updates = Object.fromEntries(Object.entries(requested).filter(([key]) => !isModelKey(key)))
  const filePath = getEnvFilePath()
  ensureEnvFileExists(filePath)

  const existing = fs.readFileSync(filePath, 'utf8').split('\n')
  const handled = new Set()
  const newLines = existing.map((line) => {
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

module.exports = { readSettings, writeSettings, getEnvFilePath, isModelKey }
