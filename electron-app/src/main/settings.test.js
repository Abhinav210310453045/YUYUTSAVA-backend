import fs from 'fs'
import os from 'os'
import path from 'path'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createRequire } from 'module'

const require = createRequire(import.meta.url)
const { readSettings, writeSettings, isModelKey } = require('./settings.js')

let home
beforeEach(() => {
  home = fs.mkdtempSync(path.join(os.tmpdir(), 'yy-settings-'))
  vi.stubEnv('YUYUTSAVA_HOME', home)
  vi.spyOn(console, 'warn').mockImplementation(() => {})
})
afterEach(() => {
  vi.unstubAllEnvs()
  vi.restoreAllMocks()
  fs.rmSync(home, { recursive: true, force: true })
})

describe('the .env the Settings panel writes', () => {
  it('takes ordinary settings', () => {
    writeSettings({ YUYUTSAVA_DAEMON_PORT: '7700' })
    expect(readSettings().YUYUTSAVA_DAEMON_PORT).toBe('7700')
  })

  it('refuses model settings — they live in models.toml', () => {
    writeSettings({
      LLM_PROVIDER: 'groq',
      GROQ_API_KEY: 'gsk-1',
      TIER_LIGHT_LLM_PROVIDER: 'ollama',
      YUYUTSAVA_HEARTBEAT_SEC: '10',
    })
    const written = readSettings()
    expect(written.LLM_PROVIDER).toBeUndefined()
    expect(written.GROQ_API_KEY).toBeUndefined()
    expect(written.TIER_LIGHT_LLM_PROVIDER).toBeUndefined()
    expect(written.YUYUTSAVA_HEARTBEAT_SEC).toBe('10')
    expect(console.warn).toHaveBeenCalled()
  })

  it('leaves the embedder its own provider', () => {
    expect(isModelKey('EMBED_LLM_PROVIDER')).toBe(false)
  })
})
