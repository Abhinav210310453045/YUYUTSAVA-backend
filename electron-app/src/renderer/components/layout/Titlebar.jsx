import React from 'react'
import { navIcons, NAV_ITEMS, PanelToggleIcon, ThemeIcon } from './navIcons.jsx'
import { useTheme } from '../../hooks/useTheme'

const LEVELS = ['DEBUG', 'INFO', 'WARNING']

export default function Titlebar({
  connected,
  logsEnabled,
  onToggleLogs,
  logLevel,
  onChangeLogLevel,
  activePanel,
  onNav,
  pendingCount,
  activityOpen,
  onToggleActivity,
}) {
  const { theme, toggle: toggleTheme } = useTheme()
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

      {/* right-aligned cluster: nav · panel toggle · log level · logs toggle */}
      <div style={{
        marginLeft: 'auto',
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        WebkitAppRegion: 'no-drag',
      }}>
        {/* Nav icons (moved here from the old left rail). */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 2 }}>
          {NAV_ITEMS.map((item) => {
            const isActive = activePanel === item.id
            return (
              <button
                key={item.id}
                onClick={() => onNav?.(item.id)}
                title={item.label}
                style={{
                  position: 'relative',
                  width: 28,
                  height: 28,
                  borderRadius: 6,
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  color: isActive ? 'var(--neon-green)' : 'var(--text-muted)',
                  background: isActive ? 'rgba(0,255,136,0.08)' : 'transparent',
                  boxShadow: isActive ? 'var(--glow-green)' : 'none',
                  border: `1px solid ${isActive ? 'rgba(0,255,136,0.2)' : 'transparent'}`,
                  transition: 'all 0.2s',
                }}
              >
                {navIcons[item.id]}
                {item.id === 'proposals' && pendingCount > 0 && (
                  <span style={{
                    position: 'absolute',
                    top: 1,
                    right: 1,
                    minWidth: 12,
                    height: 12,
                    borderRadius: 6,
                    background: 'var(--neon-red)',
                    color: '#fff',
                    fontSize: 8,
                    fontWeight: 700,
                    fontFamily: 'var(--font-mono)',
                    display: 'flex',
                    alignItems: 'center',
                    justifyContent: 'center',
                    padding: '0 2px',
                    boxShadow: 'var(--glow-red)',
                    lineHeight: 1,
                  }}>
                    {pendingCount > 99 ? '99+' : pendingCount}
                  </span>
                )}
              </button>
            )
          })}
        </div>

        <span style={{ width: 1, height: 18, background: 'var(--border-subtle)' }} />

        {/* Light / dark theme toggle. */}
        <button
          onClick={toggleTheme}
          title={theme === 'light' ? 'Switch to dark' : 'Switch to light'}
          style={{
            width: 28, height: 28, borderRadius: 6,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--text-muted)', background: 'transparent',
            border: '1px solid transparent', transition: 'all 0.2s', cursor: 'pointer',
          }}
        >
          <ThemeIcon theme={theme} />
        </button>

        {/* VS Code-style toggle for the right Activity panel. */}
        <button
          onClick={() => onToggleActivity?.(!activityOpen)}
          title={activityOpen ? 'Hide activity panel' : 'Show activity panel'}
          style={{
            width: 28,
            height: 28,
            borderRadius: 6,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            color: activityOpen ? 'var(--neon-green)' : 'var(--text-muted)',
            background: activityOpen ? 'rgba(0,255,136,0.08)' : 'transparent',
            border: `1px solid ${activityOpen ? 'rgba(0,255,136,0.2)' : 'transparent'}`,
            transition: 'all 0.2s',
          }}
        >
          <PanelToggleIcon open={activityOpen} />
        </button>

        <span style={{ width: 1, height: 18, background: 'var(--border-subtle)' }} />

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
