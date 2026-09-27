// What the board's pieces share: the mono face, the drag types that tell its
// three drag species apart, and the small outlined button.

export const mono = { fontFamily: 'var(--font-mono)' }

export const OBJECTIVE_DND = 'application/x-yy-objective'
export const NOTE_DND = 'application/x-yy-note'

export const btnStyle = (color, borderColor) => ({
  ...mono,
  fontSize: 10,
  padding: '3px 9px',
  background: 'transparent',
  color,
  border: `1px solid ${borderColor}`,
  borderRadius: 6,
  cursor: 'pointer',
})

export const hasFiles = (e) => Array.from(e.dataTransfer?.types || []).includes('Files')
export const hasType = (e, t) => Array.from(e.dataTransfer?.types || []).includes(t)
