// Per-visual-kind accent colors, shared by the Artifacts gallery and the inline
// chat/voice image cards so a chart is always green, a diagram always blue, etc.
export const KIND_ACCENT = {
  chart: '#00ff88',
  diagram: '#78a0ff',
  table: '#ffb000',
  code: '#b47bff',
  math: '#2ee6d6',
  timeline: '#ff3366',
}

export const kindAccent = (kind) => KIND_ACCENT[kind] || 'var(--neon-green)'

// Relative "…ago" label from a unix-seconds timestamp.
export function humanAge(unixSec) {
  const d = Math.max(0, Date.now() / 1000 - unixSec)
  if (d < 60) return `${Math.floor(d)}s ago`
  if (d < 3600) return `${Math.floor(d / 60)}m ago`
  if (d < 86400) return `${Math.floor(d / 3600)}h ago`
  return `${Math.floor(d / 86400)}d ago`
}
