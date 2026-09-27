import React from 'react'
import NewSessionButton from '../common/NewSessionButton'
import { ContextMeter } from './ContextAside'

// The chat's title bar: connection dot, title, and on the right the context
// meter, the host's own actions and New chat.
export default function ChatHeader({
  connected,
  title,
  headerActions,
  showNewSession,
  showContext,
  usage,
  ctxOpen,
  onToggleContext,
  onNewSession,
}) {
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'center',
        gap: 8,
        padding: '14px 24px',
        borderBottom: '1px solid var(--border-subtle)',
        background: 'var(--bg-bar)',
      }}
    >
      <span
        style={{
          width: 8,
          height: 8,
          borderRadius: '50%',
          background: connected ? 'var(--neon-green)' : 'var(--neon-red)',
          boxShadow: connected ? '0 0 6px var(--neon-green)' : 'none',
        }}
      />
      <span
        style={{
          fontFamily: 'var(--font-mono)',
          fontSize: 11,
          letterSpacing: '0.1em',
          textTransform: 'uppercase',
          color: 'var(--text-primary)',
          fontWeight: 'var(--fw-semibold)',
        }}
      >
        {title}
      </span>
      {(headerActions || showNewSession || showContext) && (
        <span style={{ marginLeft: 'auto', display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          {showContext && <ContextMeter usage={usage} open={ctxOpen} onClick={onToggleContext} />}
          {headerActions}
          {showNewSession && <NewSessionButton onClick={onNewSession} label="New chat" />}
        </span>
      )}
    </div>
  )
}
