import React, { useState } from 'react'
import { enableVoiceSource } from '../../api/client'

// One-time wake-word setup, shown the first time the user opens the Voice panel
// before WAKE_WORDS has been chosen. Picks a wake word from the openwakeword
// pretrained models (no download config needed), seeds WAKE_WORDS in the
// daemon .env, and turns on the "voice" events source so the wake word is
// actually listened for. Dismissal (either path) persists via UI_WAKE_ONBOARDED
// so this never nags again — wake words can still be edited later in Settings.

const BLUE = '#9bb8ff'
const ONBOARDED_KEY = 'UI_WAKE_ONBOARDED'

// openwakeword ships these pretrained models — no extra download/config.
const CHOICES = [
  { value: 'hey_jarvis', label: 'Hey Jarvis', hint: 'recommended' },
  { value: 'alexa', label: 'Alexa', hint: '' },
  { value: 'hey_mycroft', label: 'Hey Mycroft', hint: '' },
]

export default function WakeWordOnboarding({ save, onDone }) {
  const [choice, setChoice] = useState('hey_jarvis')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState(null)

  async function enable() {
    setBusy(true)
    setError(null)
    try {
      // Seed the env (persists + picked up by CLI / full restart) and mark
      // onboarding done, then turn on the wake-word source (hot-applies).
      await save({ WAKE_WORDS: choice, [ONBOARDED_KEY]: '1' })
      await enableVoiceSource(choice)
      onDone?.(true)
    } catch (e) {
      setError('Could not enable the wake word. Is the daemon running?')
      setBusy(false)
    }
  }

  async function skip() {
    setBusy(true)
    try { await save({ [ONBOARDED_KEY]: '1' }) } catch { /* ignore */ }
    onDone?.(false)
  }

  return (
    <div style={{
      position: 'absolute', inset: 0, zIndex: 20,
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'rgba(8,10,18,0.72)', backdropFilter: 'blur(2px)',
      animation: 'fade-in 0.2s ease',
    }}>
      <div style={{
        width: 380, maxWidth: '88%',
        background: 'var(--bg-card)',
        border: '1px solid rgba(120,160,255,0.28)',
        borderRadius: 'var(--radius-card)',
        padding: '22px 22px 18px',
        boxShadow: '0 12px 40px rgba(0,0,0,0.5)',
        display: 'flex', flexDirection: 'column', gap: 14,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
          <span style={{ fontSize: 22, color: BLUE }}>✦</span>
          <div style={{
            fontFamily: 'var(--font-mono)', fontSize: 13, fontWeight: 700,
            letterSpacing: '0.06em', textTransform: 'uppercase', color: 'var(--text-primary)',
          }}>Set a wake word</div>
        </div>

        <div style={{ fontSize: 13, lineHeight: 1.6, color: 'var(--text-secondary)' }}>
          Pick a phrase to summon YUYUTSAVA hands-free. Saying it pops the mic
          overlay even when the app is in the background. You can change or add
          wake words later in Settings.
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {CHOICES.map((c) => {
            const active = c.value === choice
            return (
              <button
                key={c.value}
                onClick={() => setChoice(c.value)}
                disabled={busy}
                style={{
                  display: 'flex', alignItems: 'center', gap: 10,
                  padding: '10px 12px', borderRadius: 8, cursor: busy ? 'default' : 'pointer',
                  textAlign: 'left',
                  background: active ? 'rgba(120,160,255,0.12)' : 'var(--bg-elevated)',
                  border: `1px solid ${active ? 'rgba(120,160,255,0.5)' : 'var(--border-card)'}`,
                  color: 'var(--text-primary)', fontSize: 13,
                  transition: 'all 0.15s',
                }}
              >
                <span style={{
                  width: 14, height: 14, borderRadius: '50%', flexShrink: 0,
                  border: `1.5px solid ${active ? BLUE : 'var(--text-muted)'}`,
                  background: active ? BLUE : 'transparent',
                  boxShadow: active ? `0 0 6px ${BLUE}` : 'none',
                }} />
                <span style={{ flex: 1 }}>“{c.label}”</span>
                {c.hint && (
                  <span style={{
                    fontFamily: 'var(--font-mono)', fontSize: 10, color: BLUE,
                    textTransform: 'uppercase', letterSpacing: '0.06em',
                  }}>{c.hint}</span>
                )}
              </button>
            )
          })}
        </div>

        {error && (
          <div style={{ fontSize: 12, color: 'var(--neon-red)', fontFamily: 'var(--font-mono)' }}>
            {error}
          </div>
        )}

        <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 2 }}>
          <button onClick={skip} disabled={busy} style={btn(false, busy)}>Not now</button>
          <button onClick={enable} disabled={busy} style={btn(true, busy)}>
            {busy ? 'Enabling…' : 'Enable wake word'}
          </button>
        </div>
      </div>
    </div>
  )
}

function btn(primary, busy) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 11, fontWeight: 600,
    letterSpacing: '0.05em', textTransform: 'uppercase',
    padding: '7px 14px', borderRadius: 'var(--radius-btn)',
    cursor: busy ? 'not-allowed' : 'pointer', opacity: busy ? 0.6 : 1,
    border: `1px solid ${primary ? 'rgba(120,160,255,0.5)' : 'var(--border-card)'}`,
    background: primary ? 'rgba(120,160,255,0.12)' : 'var(--bg-elevated)',
    color: primary ? BLUE : 'var(--text-muted)',
    transition: 'all 0.15s',
  }
}
