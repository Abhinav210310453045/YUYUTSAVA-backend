import React, { useEffect, useState, useCallback } from 'react'
import { listSessions, listVisuals, visualUrl } from '../../api/client'
import { kindAccent, humanAge } from './kinds'
import Lightbox from './Lightbox'
import VisualActions from './VisualActions'

// A gallery of everything the agent has rendered (charts, diagrams, tables,
// code, math, timelines). Images are served from the daemon's /visuals/{id}
// route; clicking one opens it full size. The same visuals also render inline
// in the chat/voice bubbles (see components/chat/MessageImages.jsx).

function VisualCard({ v, onOpen, onDeleted }) {
  const accent = kindAccent(v.kind)
  return (
    <div
      className="hover-bulge"
      style={{
        display: 'flex', flexDirection: 'column', gap: 8, padding: 8,
        background: 'var(--bg-card)', border: `1px solid ${accent}55`,
        borderRadius: 10, textAlign: 'left', '--bulge-glow': `${accent}44`,
      }}
    >
      <img
        src={visualUrl(v.url)} alt={v.title || v.kind} loading="lazy"
        onClick={() => onOpen(v)} title={v.title || v.kind}
        style={{ width: '100%', height: 150, objectFit: 'contain', background: 'var(--bg-deep)', borderRadius: 6, cursor: 'zoom-in' }}
      />
      <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 9, textTransform: 'uppercase',
          letterSpacing: '0.06em', color: accent, border: `1px solid ${accent}55`,
          borderRadius: 6, padding: '1px 6px',
        }}>{v.kind}</span>
        <span style={{
          fontSize: 11, color: 'var(--text-primary)', overflow: 'hidden',
          whiteSpace: 'nowrap', textOverflow: 'ellipsis', flex: 1,
        }}>{v.title || '—'}</span>
        <span style={{ fontSize: 10, color: 'var(--text-dim)' }}>{humanAge(v.created_ts)}</span>
      </div>
      <VisualActions visual={v} onDeleted={onDeleted} />
    </div>
  )
}

export default function ArtifactsPanel() {
  const [sessions, setSessions] = useState([])
  const [sessionId, setSessionId] = useState(null)
  const [visuals, setVisuals] = useState([])
  const [loading, setLoading] = useState(false)
  const [zoomed, setZoomed] = useState(null)

  const onDeleted = useCallback((visualId) => {
    setVisuals((cur) => cur.filter((v) => v.visual_id !== visualId))
    setZoomed((z) => (z && z.visual_id === visualId ? null : z))
  }, [])

  useEffect(() => {
    listSessions(null, 100).then((rows) => {
      setSessions(rows)
      if (rows.length) setSessionId((cur) => cur || rows[0].id)
    }).catch(() => {})
  }, [])

  const refresh = useCallback((id) => {
    if (!id) return
    setLoading(true)
    listVisuals(id)
      .then(setVisuals)
      .catch(() => setVisuals([]))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => { refresh(sessionId) }, [sessionId, refresh])

  return (
    <div style={{ flex: 1, display: 'flex', flexDirection: 'column', overflow: 'hidden', position: 'relative' }}>
      <div style={{
        display: 'flex', alignItems: 'center', gap: 10,
        padding: '14px 24px', borderBottom: '1px solid var(--border-subtle)',
      }}>
        <span style={{
          fontFamily: 'var(--font-mono)', fontSize: 11, letterSpacing: '0.1em',
          textTransform: 'uppercase', color: 'var(--text-primary)', fontWeight: 600,
        }}>Artifacts — visuals</span>
        <select
          value={sessionId || ''}
          onChange={(e) => setSessionId(e.target.value)}
          style={{
            marginLeft: 'auto', background: 'var(--bg-card)', color: 'var(--text-primary)',
            border: '1px solid var(--border-card)', borderRadius: 6, padding: '5px 8px',
            fontFamily: 'var(--font-mono)', fontSize: 11, maxWidth: 320,
          }}
        >
          {sessions.length === 0 && <option value="">no sessions</option>}
          {sessions.map((s) => (
            <option key={s.id} value={s.id}>
              {(s.workspace.split('/').filter(Boolean).pop() || s.id)} · {s.id.slice(0, 8)} · {s.origin}
            </option>
          ))}
        </select>
        <button
          onClick={() => refresh(sessionId)}
          title="Refresh"
          style={{
            fontFamily: 'var(--font-mono)', fontSize: 11, padding: '5px 10px',
            background: 'rgba(0,255,136,0.08)', color: 'var(--neon-green)',
            border: '1px solid rgba(0,255,136,0.25)', borderRadius: 6, cursor: 'pointer',
          }}
        >↻</button>
      </div>

      <div style={{ flex: 1, overflowY: 'auto', padding: '20px 24px' }}>
        {loading && (
          <div style={{ color: 'var(--text-muted)', fontFamily: 'var(--font-mono)', fontSize: 12 }}>loading…</div>
        )}
        {!loading && visuals.length === 0 && (
          <div style={{
            height: '100%', display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', gap: 8, color: 'var(--text-muted)',
            fontFamily: 'var(--font-mono)', fontSize: 12,
          }}>
            <div style={{ fontSize: 28, opacity: 0.3 }}>◱</div>
            <div>{'> no visuals yet — ask the agent to chart or diagram something'}</div>
          </div>
        )}
        {!loading && visuals.length > 0 && (
          <div style={{
            display: 'grid', gap: 14,
            gridTemplateColumns: 'repeat(auto-fill, minmax(220px, 1fr))',
          }}>
            {visuals.map((v) => <VisualCard key={v.visual_id} v={v} onOpen={setZoomed} onDeleted={onDeleted} />)}
          </div>
        )}
      </div>

      <Lightbox v={zoomed} onClose={() => setZoomed(null)} onDeleted={onDeleted} />
    </div>
  )
}
