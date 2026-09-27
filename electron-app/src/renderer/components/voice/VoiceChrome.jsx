import React from 'react'
import NewSessionButton from '../common/NewSessionButton'
import { ContextMeter } from '../chat/ContextAside'

// The voice screen's fixed chrome: its header, the two notices above the
// thread, and what an empty thread shows.

const BLUE = 'var(--text-info)'

// Borderless, floats over the mesh.
export function VoiceHeader({ connected, usage, ctxOpen, onToggleContext, onNewSession }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        padding: '14px 24px',
        position: 'relative',
        zIndex: 2,
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: connected ? BLUE : 'var(--neon-red)',
          boxShadow: connected ? `0 0 6px ${BLUE}` : 'none',
        }}
      />
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          fontWeight: 700,
          flex: 1,
          background: 'linear-gradient(90deg, #cdd9ff, var(--text-info) 60%, #8b5cf6)',
          WebkitBackgroundClip: 'text',
          backgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
        }}
      >
        Voice — talk to YUYUTSAVA
      </span>
      <ContextMeter usage={usage} open={ctxOpen} onClick={onToggleContext} />
      <NewSessionButton onClick={onNewSession} label="New" color={BLUE} />
    </div>
  )
}

// Voice mode is off — say so once, plainly, instead of letting the panel look
// broken when nothing reacts to the wake word. Not dismissible: it disappears
// the moment voice mode is turned back on.
export function VoiceModeNotice({ wakeOn, voiceModeOff }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        position: 'relative',
        zIndex: 2,
        margin: '4px 24px 0',
        padding: '8px 12px',
        background: 'rgba(140,140,150,0.08)',
        border: '1px solid var(--border-subtle)',
        borderRadius: 12,
        fontSize: 12,
        color: 'var(--text-secondary)',
      }}
    >
      <span style={{ color: 'var(--text-muted)' }}>◎</span>
      <span style={{ flex: 1 }}>
        {voiceModeOff
          ? 'Voice mode is off — nothing is listening for you and replies stay text. Tap the mic to talk anyway.'
          : !wakeOn
            ? 'Wake word is off — nothing is listening for you. Tap the mic to talk.'
            : 'Spoken replies are off — answers arrive as text.'}{' '}
        Turn it back on from the title bar.
      </span>
    </div>
  )
}

// Dismissible pointer to where the wake keywords live.
export function WakeWordNote({ onOpenSettings, onDismiss }) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 10,
        position: 'relative',
        zIndex: 2,
        margin: '4px 24px 0',
        padding: '8px 12px',
        background: 'rgba(120,160,255,0.06)',
        border: '1px solid rgba(120,160,255,0.22)',
        borderRadius: 12,
        fontSize: 12,
        color: 'var(--text-secondary)',
      }}
    >
      <span style={{ color: BLUE }}>✦</span>
      <span style={{ flex: 1 }}>
        You can add or remove wake keywords in{' '}
        <button
          onClick={onOpenSettings}
          style={{
            background: 'none',
            border: 'none',
            color: BLUE,
            cursor: 'pointer',
            padding: 0,
            font: 'inherit',
            textDecoration: 'underline',
          }}
        >
          Settings
        </button>
        .
      </span>
      <button
        onClick={onDismiss}
        title="dismiss"
        style={{
          background: 'none',
          border: 'none',
          color: 'var(--text-muted)',
          cursor: 'pointer',
          fontSize: 16,
          lineHeight: 1,
          padding: 0,
        }}
      >
        ×
      </button>
    </div>
  )
}

export function EmptyVoiceThread() {
  return (
    <div
      style={{
        flex: 1,
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        justifyContent: 'center',
        gap: 10,
        color: 'var(--text-muted)',
        fontFamily: 'var(--font-mono)',
        fontSize: 12,
        textAlign: 'center',
      }}
    >
      <div
        className="grad-animated"
        style={{
          fontSize: 34,
          fontWeight: 700,
          background: 'var(--grad-accent)',
          WebkitBackgroundClip: 'text',
          backgroundClip: 'text',
          WebkitTextFillColor: 'transparent',
        }}
      >
        ◍
      </div>
      <div style={{ maxWidth: 340, lineHeight: 1.6 }}>
        say something — your words and YUYUTSAVA&apos;s replies land here
      </div>
    </div>
  )
}
