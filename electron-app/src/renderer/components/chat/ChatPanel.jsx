import React from 'react'

export default function ChatPanel() {
  const lines = [
    '> YUYUTSAVA Terminal v0.1',
    '> Initializing chat module...',
    '',
    '  ERROR: module not found',
    '  Module: chat.interactive',
    '  Status: COMING SOON',
    '',
    '> Future: direct conversation with',
    '  the YUYUTSAVA orchestrator.',
    '',
    '> Press any key to continue..._',
  ]

  return (
    <div style={{
      flex: 1,
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      justifyContent: 'center',
      padding: 40,
    }}>
      <div style={{
        maxWidth: 480,
        width: '100%',
        background: 'var(--bg-card)',
        border: '1px solid var(--border-card)',
        borderRadius: 'var(--radius-card)',
        padding: '24px 28px',
        boxShadow: 'var(--shadow-card)',
      }}>
        <div style={{
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          marginBottom: 16,
          paddingBottom: 12,
          borderBottom: '1px solid var(--border-subtle)',
        }}>
          <span style={{
            width: 12, height: 12, borderRadius: '50%',
            background: 'var(--neon-red)', boxShadow: 'var(--glow-red)',
          }} />
          <span style={{
            width: 12, height: 12, borderRadius: '50%',
            background: 'var(--neon-amber)', boxShadow: 'var(--glow-amber)',
          }} />
          <span style={{
            width: 12, height: 12, borderRadius: '50%',
            background: 'var(--neon-green)', boxShadow: 'var(--glow-green)',
          }} />
          <span style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            color: 'var(--text-muted)',
            marginLeft: 8,
          }}>
            chat — yuyutsava terminal
          </span>
        </div>

        {lines.map((line, i) => (
          <div key={i} style={{
            fontFamily: 'var(--font-mono)',
            fontSize: 12,
            lineHeight: 1.7,
            color: line.startsWith('>') ? 'var(--neon-green)' :
                   line.startsWith('  ERROR') || line.startsWith('  Module') || line.startsWith('  Status') ? 'var(--neon-red)' :
                   line.startsWith('  ') ? 'var(--text-secondary)' :
                   'var(--text-muted)',
          }}>
            {line || ' '}
          </div>
        ))}
      </div>
    </div>
  )
}
