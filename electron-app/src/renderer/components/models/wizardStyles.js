// Shared looks of the setup wizard's steps.

export const overlay = {
  position: 'fixed',
  inset: 0,
  zIndex: 1000,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  background: 'rgba(0,0,0,0.55)',
  backdropFilter: 'blur(4px)',
}
export const sheet = {
  width: 'min(640px, 92vw)',
  maxHeight: '86vh',
  overflowY: 'auto',
  padding: 22,
  borderRadius: 'var(--radius-card)',
  border: '1px solid var(--border-card)',
  background: 'var(--bg-elevated)',
  fontFamily: 'var(--font-ui)',
}
export const heading = { fontSize: 13, color: 'var(--text-secondary)', marginBottom: 10 }
export const actions = { display: 'flex', gap: 8, justifyContent: 'flex-end', marginTop: 14 }
export const card = {
  padding: '10px 12px',
  borderRadius: 'var(--radius-card)',
  border: '1px solid var(--border-card)',
  background: 'var(--bg-card)',
  cursor: 'pointer',
}
export const optionRow = {
  display: 'flex',
  alignItems: 'center',
  gap: 8,
  padding: '4px 0',
  cursor: 'pointer',
}
