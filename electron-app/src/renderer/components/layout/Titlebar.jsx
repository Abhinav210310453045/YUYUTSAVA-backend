import React from 'react'

const LEVELS = ['DEBUG', 'INFO', 'WARNING']

export default function Titlebar({ connected, logsEnabled, onToggleLogs, logLevel, onChangeLogLevel }) {
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

      {/* right-aligned cluster: log level + logs toggle */}
      <div style={{
        marginLeft: 'auto',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        WebkitAppRegion: 'no-drag',
      }}>
        <select
          value={logLevel || 'INFO'}
          onChange={(e) => onChangeLogLevel?.(e.target.value)}
          title="Daemon log level"
          style={{
            background: 'var(--bg-panel)',
            color: 'var(--text-secondary)',
            border: '1px solid var(--border-subtle)',
            borderRadius: 4,
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            padding: '3px 6px',
            cursor: 'pointer',
            letterSpacing: '0.08em',
          }}
        >
          {LEVELS.map((l) => <option key={l} value={l}>{l}</option>)}
        </select>

        <button
          onClick={() => onToggleLogs?.(!logsEnabled)}
          title="Stream HTTP request logs into the Activity panel"
          style={{
            background: logsEnabled ? 'rgba(0,255,136,0.12)' : 'transparent',
            color: logsEnabled ? 'var(--neon-green)' : 'var(--text-muted)',
            border: `1px solid ${logsEnabled ? 'var(--neon-green)' : 'var(--border-subtle)'}`,
            borderRadius: 4,
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            padding: '3px 8px',
            cursor: 'pointer',
            letterSpacing: '0.1em',
            textTransform: 'uppercase',
            transition: 'all 0.2s',
          }}
        >
          Logs {logsEnabled ? 'ON' : 'OFF'}
        </button>
      </div>
    </div>
  )
}
