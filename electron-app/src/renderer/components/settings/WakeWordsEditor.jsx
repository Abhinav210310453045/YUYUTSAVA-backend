import React, { useState } from 'react'
import { enableVoiceSource } from '../../api/client'

// openWakeWord ships these pretrained models — offer them as one-tap presets;
// custom words still work but need a matching model file.
const PRESETS = ['hey_jarvis', 'alexa', 'hey_mycroft', 'hey_rhasspy']

function parse(csv) {
  return String(csv || '')
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
}

// List editor for WAKE_WORDS (mirrors WatchedDirsEditor). Controlled over the
// settings' comma-separated string via onChange, and additionally pushes the
// new list to the daemon's voice events-source params so it HOT-APPLIES with no
// restart (the infra added in Phase 5c). Save still persists WAKE_WORDS to .env
// so the choice survives a restart.
export default function WakeWordsEditor({ value, threshold, onChange }) {
  const words = parse(value)
  const [draft, setDraft] = useState('')

  function commit(next) {
    const csv = next.join(', ')
    onChange?.('WAKE_WORDS', csv)
    // Hot-apply now; best-effort (a stopped daemon just picks it up on next start
    // from the saved .env).
    enableVoiceSource(next, threshold || null).catch(() => { /* daemon down — fine */ })
  }

  function add(word) {
    const w = (word || '').trim()
    if (!w || words.includes(w)) return
    commit([...words, w])
    setDraft('')
  }

  function remove(word) {
    commit(words.filter((w) => w !== word))
  }

  const chipBtn = (extra) => ({
    padding: '3px 10px',
    borderRadius: 'var(--radius-btn)',
    fontSize: 10,
    fontFamily: 'var(--font-mono)',
    fontWeight: 'var(--fw-semibold)',
    letterSpacing: '0.06em',
    cursor: 'pointer',
    ...extra,
  })

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      {words.length === 0 && (
        <div style={{ color: 'var(--text-muted)', fontSize: 11, fontFamily: 'var(--font-mono)' }}>
          No wake words. Add one below — say it to summon the voice agent.
        </div>
      )}

      {words.map((w) => (
        <div key={w} style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 10px',
          background: 'var(--bg-elevated)',
          border: '1px solid var(--border-card)',
          borderRadius: 'var(--radius-card)',
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
        }}>
          <span style={{ flex: 1, color: 'var(--text-primary)' }}>{w}</span>
          <button
            onClick={() => remove(w)}
            style={chipBtn({
              textTransform: 'uppercase',
              border: '1px solid rgba(255,51,102,0.3)',
              background: 'rgba(255,51,102,0.06)',
              color: 'var(--neon-red)',
            })}
          >
            Remove
          </button>
        </div>
      ))}

      {/* Custom add */}
      <div style={{ display: 'flex', gap: 8 }}>
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); add(draft) } }}
          placeholder="custom wake word"
          style={{
            flex: 1,
            padding: '6px 10px',
            borderRadius: 'var(--radius-card)',
            border: '1px solid var(--border-card)',
            background: 'var(--bg-elevated)',
            color: 'var(--text-primary)',
            fontFamily: 'var(--font-mono)',
            fontSize: 11,
          }}
        />
        <button
          onClick={() => add(draft)}
          style={chipBtn({
            textTransform: 'uppercase',
            border: '1px solid rgba(var(--accent-rgb),0.3)',
            background: 'rgba(var(--accent-rgb),0.06)',
            color: 'var(--neon-green)',
            padding: '6px 14px',
          })}
        >
          + Add
        </button>
      </div>

      {/* Preset quick-add (not already selected) */}
      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {PRESETS.filter((p) => !words.includes(p)).map((p) => (
          <button
            key={p}
            onClick={() => add(p)}
            style={chipBtn({
              border: '1px solid rgba(120,160,255,0.3)',
              background: 'rgba(120,160,255,0.08)',
              color: 'var(--text-info)',
            })}
          >
            + {p}
          </button>
        ))}
      </div>
    </div>
  )
}
