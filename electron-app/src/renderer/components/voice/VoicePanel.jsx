import React, { useEffect, useRef, useState } from 'react'
import { useConverse } from '../../hooks/useConverse'
import { useSettings } from '../../hooks/useSettings'
import WakeWordOnboarding from './WakeWordOnboarding'

// Persisted flag (in the daemon .env via main/settings.js) for the dismissible
// "wake keywords live in Settings" note. Stored as a UI-only env key.
const NOTE_KEY = 'UI_VOICE_NOTE_DISMISSED'

const BLUE = '#9bb8ff'

// The big round mic. Heart-beats + radiates a bluish aura while listening; a
// soft idle shimmer otherwise. Clicking toggles the mic.
function MicButton({ listening, speaking, onToggle }) {
  const animation = listening
    ? 'voice-beat 1.6s ease-in-out infinite, voice-aura 1.6s ease-out infinite'
    : 'voice-idle 3.2s ease-in-out infinite'
  return (
    <button
      onClick={onToggle}
      title={listening ? 'stop microphone' : 'talk to YUYUTSAVA'}
      style={{
        width: 132, height: 132, borderRadius: '50%',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        cursor: 'pointer',
        background: listening
          ? 'radial-gradient(circle at 50% 40%, rgba(120,160,255,0.30), rgba(120,160,255,0.08))'
          : 'radial-gradient(circle at 50% 40%, rgba(120,160,255,0.14), rgba(120,160,255,0.03))',
        border: `1.5px solid rgba(120,160,255,${listening ? 0.6 : 0.3})`,
        color: BLUE,
        animation,
        transition: 'background 0.3s, border-color 0.3s',
      }}
    >
      <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor"
        strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
        <rect x="9" y="2" width="6" height="11" rx="3" />
        <path d="M5 10v1a7 7 0 0 0 14 0v-1" />
        <line x1="12" y1="18" x2="12" y2="22" />
        <line x1="8" y1="22" x2="16" y2="22" />
      </svg>
    </button>
  )
}

// Animated equaliser shown while the agent is speaking.
function SpeakingBars() {
  return (
    <div style={{ display: 'flex', alignItems: 'flex-end', gap: 3, height: 14 }}>
      {[0, 1, 2, 3, 4].map((i) => (
        <span key={i} style={{
          width: 3, height: 14, background: BLUE, borderRadius: 2,
          transformOrigin: 'bottom',
          animation: `voice-bar 0.9s ease-in-out ${i * 0.12}s infinite`,
        }} />
      ))}
    </div>
  )
}

function VoiceBubble({ m, onReplay }) {
  const isUser = m.role === 'user'
  // In-session turns carry raw PCM chunks; resumed turns carry a persisted
  // audio_url (Phase 6b) — both are replayable.
  const hasAudio = !isUser && ((m.audioChunks && m.audioChunks.length > 0) || !!m.audioUrl)
  return (
    <div style={{ display: 'flex', justifyContent: isUser ? 'flex-end' : 'flex-start' }}>
      <div style={{
        maxWidth: '80%',
        background: isUser ? 'rgba(120,160,255,0.08)' : 'var(--bg-card)',
        border: `1px solid ${m.error ? 'rgba(255,51,102,0.4)' : isUser ? 'rgba(120,160,255,0.22)' : 'var(--border-card)'}`,
        borderRadius: 'var(--radius-card)',
        padding: '10px 14px',
        color: m.error ? 'var(--neon-red)' : 'var(--text-primary)',
        fontSize: 13, lineHeight: 1.6,
        whiteSpace: 'pre-wrap', wordBreak: 'break-word',
      }}>
        {hasAudio && (
          <button
            onClick={() => onReplay(m)}
            title="replay spoken reply"
            style={{
              float: 'right', marginLeft: 8, cursor: 'pointer',
              width: 24, height: 24, borderRadius: '50%',
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
              background: 'rgba(120,160,255,0.12)',
              border: '1px solid rgba(120,160,255,0.35)', color: BLUE, fontSize: 11,
            }}
          >▶</button>
        )}
        {m.text}{m.streaming ? <span style={{ color: BLUE }}>▋</span> : null}
      </div>
    </div>
  )
}

export default function VoicePanel({ onOpenSettings, autoStartSignal = 0 }) {
  const {
    messages, connected, busy, pendingAsk, listening, speaking,
    answerAsk, startVoice, stopVoice, interrupt, replay,
  } = useConverse({ origin: 'voice' })
  const { settings, loading: settingsLoading, save } = useSettings()
  const [askDraft, setAskDraft] = useState('')
  const scrollRef = useRef(null)

  const noteDismissed = settings[NOTE_KEY] === '1'
  // First-run wake-word setup: no wake word chosen yet and not yet onboarded.
  const needsOnboarding = !settingsLoading
    && !settings.WAKE_WORDS
    && settings.UI_WAKE_ONBOARDED !== '1'

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTop = el.scrollHeight
  }, [messages, pendingAsk])

  // Hotkey/wake while the window is focused bumps autoStartSignal → start the
  // mic. Skip the initial mount (signal 0) so opening the panel by hand is quiet.
  useEffect(() => {
    if (autoStartSignal > 0 && !listening) startVoice()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [autoStartSignal])

  const dismissNote = () => { save({ [NOTE_KEY]: '1' }) }

  const status = listening
    ? '● listening — speak, then pause'
    : speaking ? '▸ speaking…'
    : busy ? 'working…'
    : 'tap the mic to talk'

  const askBody = pendingAsk?.payload?.body || pendingAsk?.payload?.question
    || pendingAsk?.payload?.reason || pendingAsk?.payload?.text || 'The agent is asking for input.'

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
      {needsOnboarding && (
        <WakeWordOnboarding save={save} onDone={() => {}} />
      )}

      {/* header */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 8,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)',
      }}>
        <span style={{
          width: 8, height: 8, borderRadius: '50%',
          background: connected ? BLUE : 'var(--neon-red)',
          boxShadow: connected ? `0 0 6px ${BLUE}` : 'none',
        }} />
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
          textTransform: 'uppercase', color: 'var(--text-primary)', fontWeight: 600,
        }}>Voice — talk to YUYUTSAVA</span>
      </div>

      {/* dismissible wake-word note */}
      {!noteDismissed && (
        <div style={{
          display: 'flex', alignItems: 'center', gap: 10,
          margin: '12px 24px 0', padding: '8px 12px',
          background: 'rgba(120,160,255,0.06)',
          border: '1px solid rgba(120,160,255,0.22)', borderRadius: 8,
          fontSize: 12, color: 'var(--text-secondary)',
        }}>
          <span style={{ color: BLUE }}>✦</span>
          <span style={{ flex: 1 }}>
            You can add or remove wake keywords in{' '}
            <button onClick={onOpenSettings} style={{
              background: 'none', border: 'none', color: BLUE, cursor: 'pointer',
              padding: 0, font: 'inherit', textDecoration: 'underline',
            }}>Settings</button>.
          </span>
          <button onClick={dismissNote} title="dismiss" style={{
            background: 'none', border: 'none', color: 'var(--text-muted)',
            cursor: 'pointer', fontSize: 16, lineHeight: 1, padding: 0,
          }}>×</button>
        </div>
      )}

      {/* mic + status */}
      <div style={{
        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 12,
        padding: '26px 24px 18px',
      }}>
        <MicButton listening={listening} speaking={speaking}
          onToggle={() => (listening ? stopVoice() : startVoice())} />
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8, minHeight: 16,
          fontFamily: 'var(--font-mono)', fontSize: 11,
          color: listening ? BLUE : speaking ? BLUE : 'var(--text-muted)',
        }}>
          {speaking && <SpeakingBars />}
          <span>{status}</span>
        </div>
        {busy && (
          <button onClick={interrupt} style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, cursor: 'pointer',
            padding: '4px 12px', borderRadius: 8,
            background: 'rgba(255,51,102,0.08)', border: '1px solid rgba(255,51,102,0.3)',
            color: 'var(--neon-red)',
          }}>stop</button>
        )}
      </div>

      {/* transcript / conversation thread */}
      <div ref={scrollRef} style={{
        flex: 1, overflowY: 'auto', padding: '8px 24px 20px',
        display: 'flex', flexDirection: 'column', gap: 12,
        borderTop: '1px solid var(--border-subtle)',
      }}>
        {messages.length === 0 && (
          <div style={{
            flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 8, color: 'var(--text-muted)',
            fontFamily: 'var(--font-mono)', fontSize: 12, textAlign: 'center',
          }}>
            <div style={{ fontSize: 26, opacity: 0.3, color: BLUE }}>◌</div>
            <div>{'> say something — your words and YUYUTSAVA\'s replies appear here'}</div>
          </div>
        )}
        {messages.map((m) => <VoiceBubble key={m.id} m={m} onReplay={replay} />)}

        {pendingAsk && (
          <div style={{
            border: '1px solid var(--neon-amber)', borderRadius: 'var(--radius-card)',
            padding: '12px 14px', background: 'rgba(255,176,0,0.06)',
          }}>
            <div style={{ fontFamily: 'var(--font-mono)', fontSize: 11, color: 'var(--neon-amber)', marginBottom: 6 }}>
              ▣ Question
            </div>
            <div style={{ fontSize: 13, color: 'var(--text-primary)', marginBottom: 8 }}>{askBody}</div>
            <div style={{ display: 'flex', gap: 8 }}>
              <input
                value={askDraft}
                onChange={(e) => setAskDraft(e.target.value)}
                onKeyDown={(e) => { if (e.key === 'Enter' && askDraft.trim()) { answerAsk(askDraft.trim()); setAskDraft('') } }}
                placeholder="type a reply, or use the buttons"
                style={{
                  flex: 1, background: 'var(--bg-deep)', color: 'var(--text-primary)',
                  border: '1px solid var(--border-card)', borderRadius: 6, padding: '6px 10px', fontSize: 12,
                }}
              />
              <button onClick={() => answerAsk('yes')} style={askBtn(true)}>approve</button>
              <button onClick={() => answerAsk('no')} style={askBtn(false)}>reject</button>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function askBtn(primary) {
  return {
    fontFamily: 'var(--font-mono)', fontSize: 12, cursor: 'pointer',
    padding: '6px 12px', borderRadius: 8,
    background: primary ? 'rgba(0,255,136,0.1)' : 'rgba(255,51,102,0.08)',
    border: `1px solid ${primary ? 'rgba(0,255,136,0.3)' : 'rgba(255,51,102,0.3)'}`,
    color: primary ? 'var(--neon-green)' : 'var(--neon-red)',
  }
}
