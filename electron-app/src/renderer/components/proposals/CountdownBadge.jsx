import React, { useEffect, useState } from 'react'

export default function CountdownBadge({ expiresTs }) {
  const [secs, setSecs] = useState(() => Math.max(0, Math.round(expiresTs - Date.now() / 1000)))

  useEffect(() => {
    const id = setInterval(() => {
      setSecs(Math.max(0, Math.round(expiresTs - Date.now() / 1000)))
    }, 1000)
    return () => clearInterval(id)
  }, [expiresTs])

  const mm = String(Math.floor(secs / 60)).padStart(2, '0')
  const ss = String(secs % 60).padStart(2, '0')

  const isWarn = secs < 60
  const isCrit = secs < 15

  return (
    <span style={{
      fontFamily: 'var(--font-mono)',
      fontSize: 11,
      color: isCrit ? 'var(--neon-red)' : isWarn ? 'var(--neon-amber)' : 'var(--text-muted)',
      animation: isCrit ? 'countdown-crit 0.5s infinite' : isWarn ? 'countdown-warn 1s infinite' : 'none',
    }}>
      {mm}:{ss}
    </span>
  )
}
