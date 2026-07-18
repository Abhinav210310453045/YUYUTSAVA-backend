import React from 'react'
import { useNotifications } from '../../hooks/useNotifications.jsx'

// Stacked in-window toasts — shown when the window is focused so we don't
// send a redundant OS banner. Stays subtle: monochrome, brief, never modal.
export default function InWindowToast() {
  const { toasts, dismissToast } = useNotifications() || { toasts: [] }
  if (!toasts || toasts.length === 0) return null

  return (
    <div style={{
      position: 'fixed',
      right: 16,
      bottom: 16,
      display: 'flex',
      flexDirection: 'column',
      gap: 8,
      zIndex: 1000,
      pointerEvents: 'none',
    }}>
      {toasts.map((t) => (
        <div
          key={t.id}
          onClick={() => dismissToast(t.id)}
          style={{
            pointerEvents: 'auto',
            cursor: 'pointer',
            maxWidth: 360,
            background: 'rgba(20,22,28,0.95)',
            border: '1px solid rgba(var(--accent-rgb),0.25)',
            borderRadius: 6,
            padding: '10px 12px',
            fontFamily: 'var(--font-mono)',
            color: 'var(--text-primary)',
            boxShadow: '0 6px 24px rgba(0,0,0,0.45)',
          }}
        >
          <div style={{
            fontSize: 10,
            color: t.kind === 'ask' ? 'var(--neon-orange, #ffa040)' : 'var(--neon-green)',
            textTransform: 'uppercase',
            letterSpacing: '0.08em',
            marginBottom: 4,
          }}>
            {t.kind}
          </div>
          <div style={{ fontSize: 12, fontWeight: 'var(--fw-semibold)' }}>{t.title}</div>
          {t.body && (
            <div style={{
              fontSize: 11,
              color: 'var(--text-muted)',
              marginTop: 4,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
            }}>
              {t.body}
            </div>
          )}
        </div>
      ))}
    </div>
  )
}
