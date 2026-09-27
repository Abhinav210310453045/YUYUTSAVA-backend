import React from 'react'

// The daemon's status and lifecycle controls, and the prompt that offers a
// restart after saving settings that need one.

export function DaemonBtn({ label, color, borderColor, bg, disabled, onClick }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        padding: '5px 14px',
        borderRadius: 'var(--radius-btn)',
        fontSize: 10,
        fontFamily: 'var(--font-mono)',
        fontWeight: 700,
        letterSpacing: '0.08em',
        textTransform: 'uppercase',
        border: `1px solid ${borderColor}`,
        background: bg,
        color,
        cursor: disabled ? 'not-allowed' : 'pointer',
        opacity: disabled ? 0.5 : 1,
        transition: 'all 0.15s',
        flexShrink: 0,
      }}
    >
      {disabled ? '...' : label}
    </button>
  )
}

// Changed vars need a daemon restart to take effect.
export function ReloadPrompt({ reloadPrompt, labelOf, daemonBusy, onApply, onLater }) {
  return (
    <div
      style={{
        background: 'rgba(251,191,36,0.07)',
        border: '1px solid rgba(251,191,36,0.3)',
        borderRadius: 'var(--radius-card)',
        padding: '12px 14px',
        fontSize: 11,
        color: 'var(--neon-amber)',
        fontFamily: 'var(--font-mono)',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div>
        Restart the daemon to apply changes to{' '}
        <strong>{reloadPrompt.keys.map(labelOf).join(', ')}</strong>.
      </div>
      <div style={{ color: reloadPrompt.noResume ? 'var(--neon-red)' : 'var(--text-muted)' }}>
        {reloadPrompt.noResume
          ? '⚠ A running task will restart from the beginning (storage/port change).'
          : 'A running task will resume from its last checkpoint.'}
      </div>
      <div style={{ display: 'flex', gap: 8 }}>
        <DaemonBtn
          label="Restart now"
          color="var(--neon-amber)"
          borderColor="rgba(251,191,36,0.3)"
          bg="rgba(251,191,36,0.06)"
          disabled={daemonBusy}
          onClick={onApply}
        />
        <DaemonBtn
          label="Later"
          color="var(--text-muted)"
          borderColor="var(--border-card)"
          bg="var(--bg-elevated)"
          disabled={daemonBusy}
          onClick={onLater}
        />
      </div>
    </div>
  )
}

// Running/stopped, port, managed — and Start, or Restart and Stop.
export function DaemonStatusBar({ daemonStatus, daemonBusy, onStart, onStop, onRestart }) {
  return (
    <div
      style={{
        background: 'var(--bg-elevated)',
        border: `1px solid ${daemonStatus?.running ? 'rgba(var(--accent-rgb),0.15)' : 'rgba(255,51,102,0.15)'}`,
        borderRadius: 'var(--radius-card)',
        padding: '10px 14px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
      }}
    >
      {/* Status indicator */}
      <span
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          flex: 1,
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: '50%',
            flexShrink: 0,
            background: daemonStatus?.running ? 'var(--neon-green)' : 'var(--neon-red)',
            boxShadow: daemonStatus?.running ? 'var(--glow-green)' : 'var(--glow-red)',
            animation: daemonStatus?.running ? 'neon-pulse 2s ease-in-out infinite' : 'none',
          }}
        />
        <span
          style={{
            color: daemonStatus?.running ? 'var(--neon-green)' : 'var(--neon-red)',
            fontWeight: 'var(--fw-semibold)',
          }}
        >
          {daemonStatus?.running ? 'Running' : 'Stopped'}
        </span>
        <span style={{ color: 'var(--text-dim)' }}>·</span>
        <span style={{ color: 'var(--text-muted)' }}>port {daemonStatus?.port ?? '7654'}</span>
        {daemonStatus?.managed && (
          <>
            <span style={{ color: 'var(--text-dim)' }}>·</span>
            <span style={{ color: 'var(--text-muted)' }}>managed</span>
          </>
        )}
      </span>

      {/* Action buttons */}
      {daemonStatus?.running ? (
        <>
          <DaemonBtn
            label="Restart"
            color="var(--neon-amber)"
            borderColor="rgba(251,191,36,0.3)"
            bg="rgba(251,191,36,0.06)"
            disabled={daemonBusy}
            onClick={onRestart}
          />
          <DaemonBtn
            label="Stop"
            color="var(--neon-red)"
            borderColor="rgba(255,51,102,0.3)"
            bg="rgba(255,51,102,0.06)"
            disabled={daemonBusy}
            onClick={onStop}
          />
        </>
      ) : (
        <DaemonBtn
          label="Start"
          color="var(--neon-green)"
          borderColor="rgba(var(--accent-rgb),0.3)"
          bg="rgba(var(--accent-rgb),0.06)"
          disabled={daemonBusy}
          onClick={onStart}
        />
      )}
    </div>
  )
}
