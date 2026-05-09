import React from 'react'

export default function Titlebar({ connected }) {
  const minimize = () => window.electronAPI?.minimizeWindow()
  const maximize = () => window.electronAPI?.maximizeWindow()
  const close = () => window.electronAPI?.closeWindow()

  return (
    <div style={{
      height: 'var(--titlebar-h)',
      display: 'flex',
      alignItems: 'center',
      paddingLeft: 80,
      paddingRight: 16,
      WebkitAppRegion: 'drag',
      borderBottom: '1px solid var(--border-subtle)',
      background: 'rgba(10, 10, 15, 0.8)',
      backdropFilter: 'blur(20px)',
      flexShrink: 0,
      gap: 12,
    }}>
      <span style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        fontWeight: 600,
        letterSpacing: '0.15em',
        color: 'var(--neon-green)',
        textShadow: '0 0 12px rgba(0,255,136,0.5)',
        textTransform: 'uppercase',
      }}>
        YUYUTSAVA Terminal
      </span>

      <span style={{
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        fontSize: 10,
        color: connected ? 'var(--neon-green)' : 'var(--text-muted)',
        fontFamily: 'var(--font-mono)',
        WebkitAppRegion: 'no-drag',
        transition: 'color 0.3s',
      }}>
        <span style={{
          width: 5,
          height: 5,
          borderRadius: '50%',
          background: connected ? 'var(--neon-green)' : 'var(--text-muted)',
          boxShadow: connected ? '0 0 6px var(--neon-green)' : 'none',
          flexShrink: 0,
          transition: 'all 0.3s',
        }} />
        {connected ? 'daemon connected' : 'disconnected'}
      </span>
    </div>
  )
}
