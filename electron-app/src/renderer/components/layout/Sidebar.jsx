import React from 'react'

const icons = {
  proposals: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/>
    </svg>
  ),
  sessions: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10"/>
      <polyline points="12 6 12 12 16 14"/>
    </svg>
  ),
  settings: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
  ),
  chat: (
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      <line x1="12" y1="10" x2="12" y2="10" strokeWidth="3"/>
    </svg>
  ),
}

export default function Sidebar({ active, onNav, pendingCount, width }) {
  const items = [
    { id: 'proposals', label: 'Proposals', badge: pendingCount },
    { id: 'sessions', label: 'Sessions' },
    { id: 'settings', label: 'Settings' },
    { id: 'chat', label: 'Chat' },
  ]

  return (
    <div style={{
      width: width ?? 'var(--sidebar-w)',
      background: 'var(--bg-panel)',
      borderRight: '1px solid var(--border-subtle)',
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      paddingTop: 12,
      gap: 4,
      flexShrink: 0,
    }}>
      {items.map(item => {
        const isActive = active === item.id
        return (
          <button
            key={item.id}
            onClick={() => onNav(item.id)}
            title={item.label}
            style={{
              position: 'relative',
              width: 44,
              height: 44,
              borderRadius: 8,
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              color: isActive ? 'var(--neon-green)' : item.locked ? 'var(--text-dim)' : 'var(--text-muted)',
              background: isActive ? 'rgba(0,255,136,0.08)' : 'transparent',
              boxShadow: isActive ? 'var(--glow-green)' : 'none',
              border: `1px solid ${isActive ? 'rgba(0,255,136,0.2)' : 'transparent'}`,
              transition: 'all 0.2s',
              cursor: item.locked ? 'default' : 'pointer',
              opacity: item.locked ? 0.4 : 1,
            }}
          >
            {icons[item.id]}
            {item.badge > 0 && (
              <span style={{
                position: 'absolute',
                top: 6,
                right: 6,
                minWidth: 14,
                height: 14,
                borderRadius: 7,
                background: 'var(--neon-red)',
                color: '#fff',
                fontSize: 9,
                fontWeight: 700,
                fontFamily: 'var(--font-mono)',
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                padding: '0 3px',
                boxShadow: 'var(--glow-red)',
                lineHeight: 1,
              }}>
                {item.badge > 99 ? '99+' : item.badge}
              </span>
            )}
          </button>
        )
      })}
    </div>
  )
}
