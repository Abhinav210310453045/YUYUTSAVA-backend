import React from 'react'
import { navIcons, NAV_ITEMS, PanelToggleIcon } from './navIcons.jsx'
import { useTheme } from '../../hooks/useTheme'
import BackButton from './BackButton'
import PlaybackButton from './PlaybackButton'

const LEVELS = ['DEBUG', 'INFO', 'WARNING']

// Mic glyph, struck through when voice mode is off.
function MicIcon({ off }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
         stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden>
      <rect x="9" y="2" width="6" height="11" rx="3" />
      <path d="M5 11a7 7 0 0 0 14 0M12 18v3" />
      {off && <path d="M3 3l18 18" />}
    </svg>
  )
}

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
  voiceOn,
  voiceSaving,
  onToggleVoice,
}) {
  const { theme, setTheme, themes } = useTheme()
  return (
    <div style={{
      height: 'var(--titlebar-h)',
      display: 'flex',
      alignItems: 'center',
      paddingLeft: 80,
      paddingRight: 16,
      WebkitAppRegion: 'drag',
      borderBottom: '1px solid var(--border-subtle)',
      background: 'var(--bg-titlebar)',
      backdropFilter: 'blur(20px)',
      flexShrink: 0,
      gap: 12,
    }}>
      {/* Global back arrow, sat where a macOS back chevron belongs — just
          right of the traffic lights, ahead of everything else. Pops the
          active tab's stack; dimmed at that tab's home view. */}
      <span style={{ WebkitAppRegion: 'no-drag', display: 'flex', marginLeft: -4 }}>
        <BackButton />
      </span>

      <span style={{
        fontFamily: 'var(--font-mono)',
        fontSize: 11,
        fontWeight: 'var(--fw-semibold)',
        letterSpacing: '0.15em',
        color: 'var(--neon-green)',
        textShadow: 'var(--title-glow)',
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
                  background: isActive ? 'rgba(var(--accent-rgb),0.08)' : 'transparent',
                  boxShadow: isActive ? 'var(--glow-green)' : 'none',
                  border: `1px solid ${isActive ? 'rgba(var(--accent-rgb),0.2)' : 'transparent'}`,
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

        {/* Transport for a spoken reply still playing on a view you've left.
            Renders nothing while silent, or while you're on that view. */}
        <PlaybackButton />

        {/* Voice mode. Off = the daemon stops listening for the wake word (it
            releases the mic) and stops speaking replies; the mic button in the
            Voice panel still works. Daemon-owned, so the CLI and the overlay
            see the same state. */}
        <button
          onClick={() => onToggleVoice?.(!voiceOn)}
          disabled={voiceSaving}
          title={voiceOn
            ? 'Voice mode is ON — wake word is listening and replies are spoken. Click to turn off.'
            : 'Voice mode is OFF — nothing is listening for you and replies stay text. The mic button still works.'}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 5,
            height: 28,
            padding: '0 8px',
            borderRadius: 6,
            color: voiceOn ? 'var(--neon-green)' : 'var(--text-muted)',
            background: voiceOn ? 'rgba(var(--accent-rgb),0.08)' : 'transparent',
            border: `1px solid ${voiceOn ? 'rgba(var(--accent-rgb),0.2)' : 'var(--border-subtle)'}`,
            fontFamily: 'var(--font-mono)',
            fontSize: 10,
            letterSpacing: '0.1em',
            textTransform: 'uppercase',
            cursor: voiceSaving ? 'progress' : 'pointer',
            opacity: voiceSaving ? 0.6 : 1,
            transition: 'all 0.2s',
          }}
        >
          <MicIcon off={!voiceOn} />
          {voiceOn ? 'Voice' : 'Muted'}
        </button>

        <span style={{ width: 1, height: 18, background: 'var(--border-subtle)' }} />

        {/* Color theme picker — themes live in styles/theme.css + useTheme. */}
        <select
          value={theme}
          onChange={(e) => setTheme(e.target.value)}
          title="Color theme"
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
          {themes.map((t) => <option key={t.id} value={t.id}>{t.label}</option>)}
        </select>

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
            background: activityOpen ? 'rgba(var(--accent-rgb),0.08)' : 'transparent',
            border: `1px solid ${activityOpen ? 'rgba(var(--accent-rgb),0.2)' : 'transparent'}`,
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
            background: logsEnabled ? 'rgba(var(--accent-rgb),0.12)' : 'transparent',
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
